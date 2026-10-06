"""권한을 유지하면서 일반 인트라넷 메뉴 데이터를 통합 검색한다.

통합관리 영역의 테이블(회원/인사, 메뉴권한, 설정, 테마, 사용통계,
로그인 기록 등)은 의도적으로 이 모듈에 등록하지 않는다.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from urllib.parse import quote, urlencode

from flask import Blueprint, jsonify, request, session

from .database import get_db
from .menu_access import build_menu_access
from .school_bp import SCHOOL_CATEGORY_ALIASES, can_access_post, can_access_school
from .security import is_admin_session


unified_search_bp = Blueprint("unified_search", __name__)

PER_SOURCE_LIMIT = 6
MAX_RESULTS = 60
MAX_QUERY_LENGTH = 100

BOARD_MENU_KEYS = {
    "noti": "board_noti",
    "archive": "board_archive",
    "manual": "board_manual",
}

# school_bp의 카테고리는 내부적으로 영문 id(예: weekly_report)로 저장되므로
# 검색 결과에 노출할 때는 화면에 쓰이는 한글 명칭으로 바꿔서 보여준다.
SCHOOL_CATEGORY_LABELS = {value: key for key, value in SCHOOL_CATEGORY_ALIASES.items()}
SCHOOL_CATEGORY_LABELS["team_review"] = "팀장전용"


def _school_category_label(category):
    text = str(category or "").strip()
    return SCHOOL_CATEGORY_LABELS.get(text, text)


SOURCE_ORDER = {
    "board": 10,
    "school": 20,
    "gallery": 30,
    "approval": 40,
    "certificate": 50,
    "expense": 60,
    "manual": 70,
    "leaflet": 80,
    "contacts": 130,
    "smart_document": 140,
    "ai_mail": 150,
    "payroll": 160,
    "contract": 180,
}


def _table_exists(conn, table_name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(conn, table_name):
    if not _table_exists(conn, table_name):
        return set()
    quoted = '"' + str(table_name).replace('"', '""') + '"'
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")}


def _like_value(query):
    escaped = str(query).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _plain_text(value, limit=220):
    text = str(value or "")
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _date(value):
    return str(value or "")[:16]


def _result(source, source_label, icon, title, snippet, meta, url, *, date="", thumbnail=""):
    return {
        "source": source,
        "source_label": source_label,
        "icon": icon,
        "title": _plain_text(title, 140) or "제목 없음",
        "snippet": _plain_text(snippet),
        "meta": _plain_text(meta, 140),
        "url": str(url or ""),
        "date": _date(date),
        "thumbnail": str(thumbnail or ""),
    }


def _safe_source(results, source_name, callback):
    try:
        results.extend(callback())
    except sqlite3.Error:
        # 일부 기능은 최초 방문 시 테이블을 만드는 구조다. 아직 초기화되지
        # 않은 선택 기능 하나가 전체 검색을 중단시키지 않게 한다.
        return
    except (KeyError, TypeError, ValueError):
        return


def _search_boards(conn, like, access):
    if not _table_exists(conn, "board_posts") or not _table_exists(conn, "board_config"):
        return []
    try:
        user_level = int(session.get("user_level", 99))
    except (TypeError, ValueError):
        user_level = 99
    rows = conn.execute(
        """
        SELECT p.id, p.board_en, p.title, p.content, p.author, p.created_at,
               c.name_kr, c.lvl_access, c.lvl_read,
               (SELECT GROUP_CONCAT(f.original_name, ' ') FROM board_files f WHERE f.post_id=p.id) AS filenames,
               (SELECT GROUP_CONCAT(cm.content, ' ') FROM board_comments cm WHERE cm.post_id=p.id) AS comments
        FROM board_posts p
        JOIN board_config c ON c.name_en=p.board_en
        WHERE p.title LIKE ? ESCAPE '\\'
           OR p.content LIKE ? ESCAPE '\\'
           OR p.author LIKE ? ESCAPE '\\'
           OR EXISTS (SELECT 1 FROM board_files f WHERE f.post_id=p.id AND f.original_name LIKE ? ESCAPE '\\')
           OR EXISTS (SELECT 1 FROM board_comments cm WHERE cm.post_id=p.id AND cm.content LIKE ? ESCAPE '\\')
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT 80
        """,
        (like, like, like, like, like),
    ).fetchall()
    output = []
    for row in rows:
        menu_key = BOARD_MENU_KEYS.get(str(row["board_en"] or ""))
        if menu_key and not access.get(menu_key, False):
            continue
        if user_level > int(row["lvl_access"] or 0):
            continue
        if user_level > int(row["lvl_read"] or 0):
            continue
        detail = row["content"] or row["comments"] or row["filenames"]
        output.append(_result(
            "board", row["name_kr"] or "게시판", "fa-clipboard-list",
            row["title"], detail, f"{row['author']} · 게시판", f"/board/{quote(str(row['board_en']))}/read/{row['id']}",
            date=row["created_at"],
        ))
        if len(output) >= PER_SOURCE_LIMIT:
            break
    return output


def _approval_snippet(raw):
    """doc_data는 JSON 문자열이라 그대로 보여주면 이스케이프된 \\n 등이 그대로 노출된다.
    실제 본문 값만 꺼내서 사람이 읽는 텍스트로 보여준다."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return raw
    if not isinstance(data, dict):
        return raw
    body = data.get("본문")
    if body:
        return body
    return " ".join(str(v) for v in data.values() if v not in (None, ""))


def _approval_visible(row, user_name):
    # 임시저장은 아직 상신하지 않은 개인 문서라 본인만 검색된다.
    if str(row["status"] or "").strip() == "임시저장":
        return bool(user_name and user_name == str(row["drafter"] or "").strip())
    if is_admin_session():
        return True
    direct = {
        str(row["drafter"] or "").strip(),
        str(row["approver_1"] or "").strip(),
        str(row["approver_2"] or "").strip(),
    }
    direct.update(item.strip() for item in str(row["receivers"] or "").split(",") if item.strip())
    direct.update(item.strip() for item in str(row["cc_receivers"] or "").split(",") if item.strip())
    return bool(user_name and user_name in direct)


def _search_approvals(conn, like, access):
    if not access.get("approval_main", False) or not _table_exists(conn, "approvals"):
        return []
    rows = conn.execute(
        """
        SELECT * FROM approvals
        WHERE title LIKE ? ESCAPE '\\' OR doc_type LIKE ? ESCAPE '\\'
           OR drafter LIKE ? ESCAPE '\\' OR status LIKE ? ESCAPE '\\'
           OR filename LIKE ? ESCAPE '\\'
        ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 80
        """,
        (like, like, like, like, like),
    ).fetchall()
    user_name = str(session.get("user_name") or "").strip()
    output = []
    for row in rows:
        if not _approval_visible(row, user_name):
            continue
        output.append(_result(
            "approval", "사내결재", "fa-file-signature", row["title"], _approval_snippet(row["doc_data"]),
            f"{row['doc_type']} · {row['drafter']} · {row['status']}", f"/approval?open_doc={row['id']}",
            date=row["updated_at"] or row["created_at"],
        ))
        if len(output) >= PER_SOURCE_LIMIT:
            break
    return output


# 증명서 발급관리는 강사 / 임직원 / 우수강사 세 메뉴·테이블로 분리돼 있다.
CERTIFICATE_SEARCH_SOURCES = (
    ("instructor", "document_instructor", "instructor_certificate_requests", "강사증명"),
    ("employee", "document_employee", "employee_certificate_requests", "임직원증명"),
    ("excellent", "document_excellent", "excellent_certificate_requests", "우수강사인증서"),
)


def _search_certificates(conn, like, access, query):
    output = []
    for kind, menu_key, table, label in CERTIFICATE_SEARCH_SOURCES:
        if not access.get(menu_key, False) or not _table_exists(conn, table):
            continue
        rows = conn.execute(
            f"""
            SELECT id, certificate_type, applicant_name, workplace, subject_or_duty,
                   purpose, position, status, applied_date, company_name, workgroup_name
            FROM {table}
            WHERE applicant_name LIKE ? ESCAPE '\\' OR certificate_type LIKE ? ESCAPE '\\'
               OR workplace LIKE ? ESCAPE '\\' OR subject_or_duty LIKE ? ESCAPE '\\'
               OR purpose LIKE ? ESCAPE '\\' OR position LIKE ? ESCAPE '\\'
               OR company_name LIKE ? ESCAPE '\\' OR workgroup_name LIKE ? ESCAPE '\\'
            ORDER BY id DESC LIMIT ?
            """,
            (*([like] * 8), PER_SOURCE_LIMIT),
        ).fetchall()
        url = f"/document/{kind}/admin?" + urlencode({"search": query})
        output.extend(
            _result(
                "certificate", f"{label} 발급관리", "fa-file-invoice",
                f"{row['applicant_name']} · {row['certificate_type']}",
                " · ".join(filter(None, [row["workplace"], row["subject_or_duty"], row["purpose"]])),
                f"{row['company_name'] or row['workgroup_name'] or '증명발급'} · {row['status']}", url,
                date=row["applied_date"],
            )
            for row in rows
        )
    return output[:PER_SOURCE_LIMIT]


def _search_expenses(conn, like, access):
    if not access.get("expense_main", False) or not _table_exists(conn, "expense_reports"):
        return []
    rows = conn.execute(
        """
        SELECT r.*,
               (SELECT GROUP_CONCAT(COALESCE(i.vendor,'') || ' ' || COALESCE(i.description,'') || ' ' || COALESCE(i.note,''), ' ')
                FROM expense_items i WHERE i.report_id=r.id) AS item_text
        FROM expense_reports r
        WHERE r.title LIKE ? ESCAPE '\\' OR r.drafter LIKE ? ESCAPE '\\'
           OR r.expense_school_name LIKE ? ESCAPE '\\' OR r.expense_manager LIKE ? ESCAPE '\\'
           OR r.expense_kind LIKE ? ESCAPE '\\' OR r.memo LIKE ? ESCAPE '\\'
           OR EXISTS (SELECT 1 FROM expense_items i WHERE i.report_id=r.id AND
               (i.vendor LIKE ? ESCAPE '\\' OR i.description LIKE ? ESCAPE '\\' OR i.note LIKE ? ESCAPE '\\'))
        ORDER BY COALESCE(r.submitted_at, r.created_at) DESC, r.id DESC LIMIT 80
        """,
        (like, like, like, like, like, like, like, like, like),
    ).fetchall()
    # 현재 지출결의 화면은 메뉴 접근 가능 회원에게 전체 내역을 제공한다.
    return [
        _result(
            "expense", "지출결의", "fa-receipt", row["title"], row["item_text"] or row["memo"],
            f"{row['expense_school_name'] or row['expense_org_type'] or '본사'} · {row['expense_manager'] or row['drafter']} · {row['payment_status']}",
            f"/expense?open_report={row['id']}", date=row["submitted_at"] or row["created_at"],
        )
        for row in rows[:PER_SOURCE_LIMIT]
    ]


def _preferred_school_key(conn):
    rows = conn.execute(
        "SELECT id, access_key FROM schools WHERE COALESCE(is_active,1)=1 ORDER BY year DESC, id DESC"
    ).fetchall()
    for row in rows:
        if can_access_school(conn, row["id"]):
            return str(row["access_key"] or "")
    return ""


def _search_school(conn, like, access):
    if not access.get("school_workspace", False) or not _table_exists(conn, "school_posts"):
        return []
    school_key = _preferred_school_key(conn)
    if not school_key:
        return []
    rows = conn.execute(
        """
        SELECT p.*, s.school_name,
               (SELECT GROUP_CONCAT(c.content, ' ') FROM school_post_comments c WHERE c.post_id=p.id) AS comments
        FROM school_posts p LEFT JOIN schools s ON s.id=p.school_id
        WHERE p.title LIKE ? ESCAPE '\\' OR p.content LIKE ? ESCAPE '\\'
           OR p.author LIKE ? ESCAPE '\\' OR p.filename LIKE ? ESCAPE '\\'
           OR EXISTS (SELECT 1 FROM school_post_comments c WHERE c.post_id=p.id AND c.content LIKE ? ESCAPE '\\')
        ORDER BY p.created_at DESC, p.id DESC LIMIT 100
        """,
        (like, like, like, like, like),
    ).fetchall()
    output = []
    for row in rows:
        if not can_access_post(conn, row["school_id"], row["category"], post=row):
            continue
        url = f"/school/{quote(school_key)}?" + urlencode({"category": row["category"], "open_post": row["id"]})
        output.append(_result(
            "school", "학교업무공간", "fa-school", row["title"], row["content"] or row["comments"],
            f"{row['school_name'] or '본부 공유'} · {_school_category_label(row['category'])} · {row['author']}", url, date=row["created_at"],
        ))
        if len(output) >= PER_SOURCE_LIMIT:
            break
    if access.get("school_tasks", False) and _table_exists(conn, "school_tasks") and len(output) < PER_SOURCE_LIMIT:
        tasks = conn.execute(
            """
            SELECT t.*,
                   COALESCE(NULLIF(t.custom_school_name, ''), s.school_name, '미지정') AS school_name,
                   s.access_key
            FROM school_tasks t
            LEFT JOIN schools s ON s.id=t.school_id
            WHERE t.title LIKE ? ESCAPE '\\'
               OR t.note LIKE ? ESCAPE '\\'
               OR t.owner LIKE ? ESCAPE '\\'
               OR t.custom_school_name LIKE ? ESCAPE '\\'
            ORDER BY t.start_date DESC, t.id DESC LIMIT 40
            """,
            (like, like, like, like),
        ).fetchall()
        for row in tasks:
            if not can_access_school(conn, row["school_id"]):
                continue
            output.append(_result(
                "school", "학교업무 일정", "fa-list-check", row["title"], row["note"],
                f"{row['school_name']} · {row['owner']}", "/school/tasks", date=row["start_date"],
            ))
            if len(output) >= PER_SOURCE_LIMIT:
                break
    return output


def _search_gallery(conn, like, access):
    if not _table_exists(conn, "gall2_posts"):
        return []
    columns = _table_columns(conn, "gall2_posts")
    has_scope = "school_id" in columns
    rows = conn.execute(
        """
        SELECT p.*,
               (SELECT thumb_name FROM gall2 g WHERE g.post_id=p.id ORDER BY g.id LIMIT 1) AS thumb_name,
               (SELECT GROUP_CONCAT(g.title, ' ') FROM gall2 g WHERE g.post_id=p.id) AS image_titles,
               (SELECT GROUP_CONCAT(c.content, ' ') FROM gall2_comments c WHERE c.post_id=p.id) AS comments
        FROM gall2_posts p
        WHERE p.title LIKE ? ESCAPE '\\' OR p.content LIKE ? ESCAPE '\\'
           OR p.author LIKE ? ESCAPE '\\'
           OR EXISTS (SELECT 1 FROM gall2 g WHERE g.post_id=p.id AND g.title LIKE ? ESCAPE '\\')
           OR EXISTS (SELECT 1 FROM gall2_comments c WHERE c.post_id=p.id AND c.content LIKE ? ESCAPE '\\')
        ORDER BY p.created_at DESC, p.id DESC LIMIT 80
        """,
        (like, like, like, like, like),
    ).fetchall()
    school_key = _preferred_school_key(conn) if access.get("school_workspace", False) else ""
    output = []
    for row in rows:
        school_id = row["school_id"] if has_scope else None
        if school_id is None:
            if not access.get("gallery_main", False):
                continue
            url = f"/gall2?post_id={row['id']}"
            thumb = f"/gall2/thumb/{quote(str(row['thumb_name']))}" if row["thumb_name"] else ""
            label = "사내 갤러리"
        else:
            if not school_key:
                continue
            gallery_school_key = school_key
            if int(school_id or 0) > 0:
                if not can_access_school(conn, school_id):
                    continue
                school_row = conn.execute(
                    "SELECT access_key FROM schools WHERE id=?",
                    (school_id,),
                ).fetchone()
                if not school_row or not school_row["access_key"]:
                    continue
                gallery_school_key = str(school_row["access_key"])
            url = f"/school/{quote(gallery_school_key)}/gallery?post_id={row['id']}"
            thumb = f"/school/{quote(gallery_school_key)}/gallery/thumb/{quote(str(row['thumb_name']))}" if row["thumb_name"] else ""
            label = "학교 갤러리"
        output.append(_result(
            "gallery", label, "fa-images", row["title"], row["content"] or row["comments"] or row["image_titles"],
            f"{row['author']} · 사진", url, date=row["created_at"], thumbnail=thumb,
        ))
        if len(output) >= PER_SOURCE_LIMIT:
            break
    return output


def _search_manuals(conn, like, access):
    if not access.get("board_manual", False) or not _table_exists(conn, "manuals"):
        return []
    rows = conn.execute(
        """
        SELECT m.id, m.title, m.description, m.updated_at,
               GROUP_CONCAT(COALESCE(s.title,'') || ' ' || COALESCE(s.description,'') || ' ' || COALESCE(s.content_html,''), ' ') AS sections
        FROM manuals m LEFT JOIN manual_sections s ON s.manual_id=m.id
        WHERE m.status='published' AND (m.title LIKE ? ESCAPE '\\' OR m.description LIKE ? ESCAPE '\\'
              OR EXISTS (SELECT 1 FROM manual_sections x WHERE x.manual_id=m.id AND
                  (x.title LIKE ? ESCAPE '\\' OR x.description LIKE ? ESCAPE '\\' OR x.content_html LIKE ? ESCAPE '\\')))
        GROUP BY m.id ORDER BY m.updated_at DESC LIMIT ?
        """,
        (like, like, like, like, like, PER_SOURCE_LIMIT),
    ).fetchall()
    return [_result("manual", "업무메뉴얼", "fa-book", r["title"], r["description"] or r["sections"], "게시된 메뉴얼", f"/manual/{r['id']}", date=r["updated_at"]) for r in rows]


def _search_leaflets(conn, like, access):
    if not access.get("ebook_library", False) or not _table_exists(conn, "ebooks"):
        return []
    # 통합관리의 텍스트 eBook(kind=text)은 제외하고 일반 메뉴의 e리플렛만 검색한다.
    rows = conn.execute(
        """
        SELECT id, title, author, description, updated_at FROM ebooks
        WHERE kind='leaflet' AND (title LIKE ? ESCAPE '\\' OR author LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
        ORDER BY updated_at DESC, id DESC LIMIT ?
        """,
        (like, like, like, PER_SOURCE_LIMIT),
    ).fetchall()
    return [_result("leaflet", "e리플렛", "fa-book-open-reader", r["title"], r["description"], r["author"], f"/ebook/{r['id']}", date=r["updated_at"]) for r in rows]


def _search_contacts(conn, like, access):
    if not access.get("contacts_main", False):
        return []
    output = []
    if _table_exists(conn, "office_contact_entries"):
        rows = conn.execute(
            """
            SELECT * FROM office_contact_entries WHERE organization_name LIKE ? ESCAPE '\\'
               OR person_name LIKE ? ESCAPE '\\' OR role_title LIKE ? ESCAPE '\\'
               OR phone LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\' OR memo LIKE ? ESCAPE '\\'
            ORDER BY sort_order, id LIMIT ?
            """,
            (*([like] * 6), PER_SOURCE_LIMIT),
        ).fetchall()
        output.extend(_result("contacts", "본사연락망", "fa-address-book", r["organization_name"] or r["person_name"], " · ".join(filter(None, [r["person_name"], r["role_title"], r["memo"]])), " · ".join(filter(None, [r["phone"], r["email"]])), "/contacts") for r in rows)
    if len(output) < PER_SOURCE_LIMIT and _table_exists(conn, "office_contact_defaults"):
        rows = conn.execute(
            """
            SELECT * FROM office_contact_defaults WHERE unit_name LIKE ? ESCAPE '\\'
               OR person_name LIKE ? ESCAPE '\\' OR role_title LIKE ? ESCAPE '\\'
               OR phone LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\'
            ORDER BY sort_order, id LIMIT ?
            """,
            (*([like] * 5), PER_SOURCE_LIMIT - len(output)),
        ).fetchall()
        output.extend(_result("contacts", "본사연락망", "fa-address-book", r["unit_name"] or r["person_name"], " · ".join(filter(None, [r["person_name"], r["role_title"]])), " · ".join(filter(None, [r["phone"], r["email"]])), "/contacts") for r in rows)
    return output[:PER_SOURCE_LIMIT]


def _search_owner_work(conn, like, access):
    owner = str(session.get("emp_no") or "").strip()
    output = []
    configs = (
        ("smart_document_main", "smart_document_history", "smart_document", "스마트 공문", "fa-file-circle-check", "/smart-document", "title", "subject", "recipient", "status", "updated_at"),
        ("ai_mail_main", "ai_mail_campaigns", "ai_mail", "스마트 메일", "fa-wand-magic-sparkles", "/ai-mail", "name", "subject", "group_name", "status", "updated_at"),
        ("payroll_main", "payroll_campaigns", "payroll", "스마트 명세서", "fa-envelope-open-text", "/payroll", "subject", "source_filename", "group_name", "status", "updated_at"),
    )
    for menu_key, table, source, label, icon, url, title_col, snippet_col, meta_col, status_col, date_col in configs:
        if not access.get(menu_key, False) or not _table_exists(conn, table):
            continue
        columns = _table_columns(conn, table)
        wanted = {"owner_emp_no", title_col, snippet_col, meta_col, status_col, date_col}
        if not wanted.issubset(columns):
            continue
        rows = conn.execute(
            f"""
            SELECT * FROM {table} WHERE owner_emp_no=? AND
              ({title_col} LIKE ? ESCAPE '\\' OR {snippet_col} LIKE ? ESCAPE '\\' OR {meta_col} LIKE ? ESCAPE '\\' OR {status_col} LIKE ? ESCAPE '\\')
            ORDER BY {date_col} DESC, id DESC LIMIT ?
            """,
            (owner, like, like, like, like, PER_SOURCE_LIMIT),
        ).fetchall()
        output.extend(_result(source, label, icon, r[title_col], r[snippet_col], f"{r[meta_col] or ''} · {r[status_col] or ''}".strip(" ·"), url, date=r[date_col]) for r in rows)
    return output


def _search_contracts(conn, like, access, query):
    output = []
    # 강사전자계약 / 임직원전자계약 메뉴 권한이 있는 쪽의 계약구분만 검색한다.
    from .menu_access import VERIFIED_CONTRACT_KIND_MENUS
    from .verified_contract_repository import CONTRACT_TABLES

    allowed_kinds = [kind for kind, menu in VERIFIED_CONTRACT_KIND_MENUS.items() if access.get(menu, False)]
    for kind in allowed_kinds:
        if not _table_exists(conn, CONTRACT_TABLES[kind]):
            continue
        rows = conn.execute(
            f"""
            SELECT id, contract_type, school_name, department, signer_name, status, title_snapshot, updated_at
            FROM {CONTRACT_TABLES[kind]} WHERE (
              contract_type LIKE ? ESCAPE '\\' OR school_name LIKE ? ESCAPE '\\'
              OR department LIKE ? ESCAPE '\\' OR signer_name LIKE ? ESCAPE '\\' OR title_snapshot LIKE ? ESCAPE '\\')
            ORDER BY updated_at DESC, id DESC LIMIT ?
            """,
            (*([like] * 5), PER_SOURCE_LIMIT),
        ).fetchall()
        output.extend(_result("contract", "전자계약", "fa-file-signature", r["title_snapshot"] or f"{r['signer_name']} 계약", f"{r['school_name']} · {r['department']}", f"{r['contract_type']} · {r['status']}", f"/verified-contract/{kind}/admin?" + urlencode({"q": query}), date=r["updated_at"]) for r in rows)
    # 기존 계약 테이블은 오래된 인코딩 컬럼이 섞일 수 있어 개인식별번호 등
    # 원본 행을 일반 검색에 노출하지 않는다. 인증전자계약의 안전한 표시 필드만 사용한다.
    return output[:PER_SOURCE_LIMIT]


@unified_search_bp.get("/api/unified-search")
def unified_search():
    if not session.get("emp_no"):
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    query = re.sub(r"\s+", " ", str(request.args.get("q") or "")).strip()
    if len(query) < 2:
        return jsonify({
            "status": "success", "query": query, "results": [], "total": 0,
            "message": "검색어를 2글자 이상 입력해주세요.", "excluded_scope": "통합관리",
        })
    if len(query) > MAX_QUERY_LENGTH:
        return jsonify({"status": "error", "message": f"검색어는 {MAX_QUERY_LENGTH}자 이하로 입력해주세요."}), 400

    like = _like_value(query)
    access = build_menu_access(session.get("user_level", 99))
    results = []
    conn = get_db()
    try:
        sources = (
            ("board", lambda: _search_boards(conn, like, access)),
            ("school", lambda: _search_school(conn, like, access)),
            ("gallery", lambda: _search_gallery(conn, like, access)),
            ("approval", lambda: _search_approvals(conn, like, access)),
            ("certificate", lambda: _search_certificates(conn, like, access, query)),
            ("expense", lambda: _search_expenses(conn, like, access)),
            ("manual", lambda: _search_manuals(conn, like, access)),
            ("leaflet", lambda: _search_leaflets(conn, like, access)),
            ("contacts", lambda: _search_contacts(conn, like, access)),
            ("owner_work", lambda: _search_owner_work(conn, like, access)),
            ("contract", lambda: _search_contracts(conn, like, access, query)),
        )
        for source_name, callback in sources:
            _safe_source(results, source_name, callback)
    finally:
        conn.close()

    # 각 소스 쿼리의 최신순을 유지하면서 메뉴 영역 순서만 정리한다.
    results.sort(key=lambda item: SOURCE_ORDER.get(item["source"], 999))
    results = results[:MAX_RESULTS]
    return jsonify({
        "status": "success",
        "query": query,
        "results": results,
        "total": len(results),
        "source_count": len({(item["source"], item["source_label"]) for item in results}),
        "excluded_scope": "통합관리",
    })
