from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import quote

from flask import (
    Blueprint, Response, abort, current_app, jsonify, redirect, render_template,
    request, send_from_directory, url_for
)
from .storage import MANUAL_UPLOADS
from werkzeug.utils import secure_filename

from routes.database import get_db
from routes.manual_import import (
    IMPORT_ACCEPT_ATTR,
    SAMPLE_MARKDOWN,
    SUPPORTED_IMPORT_EXTENSIONS,
    ManualImportError,
    file_extension,
    manual_to_markdown,
    parse_manual_file,
    parse_markdown,
)

manual_bp = Blueprint("manual", __name__)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "jpe", "jfif", "webp", "gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TXT_BYTES = 2 * 1024 * 1024
# 외부파일(TXT/MD/HTML/DOCX)로 메뉴얼을 등록할 때의 파일 용량 제한.
MAX_IMPORT_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------------
# Storage / DB helpers
# ---------------------------------------------------------------------
def _manual_root() -> Path:
    path = Path(current_app.config.get("MANUAL_UPLOAD_ROOT", str(MANUAL_UPLOADS)))
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connect():
    """새담인트라넷의 routes.database.get_db()를 그대로 사용합니다."""
    conn = get_db()
    try:
        # 기존 DB 설정에 foreign_keys가 꺼져 있어도 이 연결에서는 활성화합니다.
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn, table_name: str):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(conn, table_name: str, column_name: str, ddl: str):
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


def init_manual_schema() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS manuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '새 메뉴얼',
                description TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS manual_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manual_id INTEGER NOT NULL,
                section_no INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                content_html TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (manual_id) REFERENCES manuals(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_manual_sections_manual
            ON manual_sections(manual_id, sort_order);

            CREATE TABLE IF NOT EXISTS manual_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manual_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (manual_id) REFERENCES manuals(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_manual_images_manual
            ON manual_images(manual_id);
            """
        )

        # v5: 썸네일은 본문 첫 이미지와 분리하여 별도 업로드만 사용합니다.
        # 기존 DB에는 아래 컬럼이 없으므로 안전하게 자동 추가합니다.
        _ensure_column(conn, "manuals", "thumbnail_source", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "manuals", "thumbnail_filename", "TEXT NOT NULL DEFAULT ''")


# 이전 패키지명과의 호환용 별칭
init_manual_tables = init_manual_schema

@manual_bp.before_request
def _ensure_tables():
    init_manual_schema()


# ---------------------------------------------------------------------
# Optional write permission hook
# ---------------------------------------------------------------------
def manual_write_required(view):
    """
    Optional integration point.

    In app config:
        app.config["MANUAL_WRITE_GUARD"] = callable

    The callable may:
      - return True / None -> allow
      - return False       -> 403
      - return a Flask Response -> returned immediately

    If no guard is configured, access is allowed so this module can be
    dropped into an existing intranet first. Connect your existing login/
    permission decorator before production use.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        guard = current_app.config.get("MANUAL_WRITE_GUARD")
        if callable(guard):
            result = guard()
            if result is False:
                abort(403)
            if result not in (None, True):
                return result
        return view(*args, **kwargs)
    return wrapped


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rowdict(row):
    return dict(row) if row is not None else None


def _get_manual_or_404(manual_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()
    if not row:
        abort(404)
    return _rowdict(row)


def _get_sections(manual_id: int):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM manual_sections
            WHERE manual_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (manual_id,),
        ).fetchall()
    return [_rowdict(r) for r in rows]


# ---------------------------------------------------------------------
# HTML sanitation
# ---------------------------------------------------------------------
ALLOWED_CSS_PROPERTIES = [
    "color", "font-size",
    "width", "height", "min-width", "max-width",
    "text-align", "vertical-align",
    "margin-left", "margin-right",
    "table-layout",
]

ALLOWED_TAGS = [
    "p", "br", "strong", "b", "em", "i", "u", "s",
    "h2", "h3", "h4", "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "div", "span", "a", "figure", "figcaption", "img",
    "blockquote", "code", "pre", "hr",
]

ALLOWED_ATTRIBUTES = {
    "*": ["class", "style"],
    "a": ["href", "title", "target", "rel", "class", "style"],
    "img": ["src", "alt", "title", "data-filename", "class", "style"],
    "figure": ["data-filename", "class", "style"],
    "td": ["colspan", "rowspan", "class", "style"],
    "th": ["colspan", "rowspan", "class", "style"],
    "table": ["class", "style"],
}

# 내용까지 통째로 버리는 태그. 나머지 비허용 태그는 글자만 남기고 태그를 벗겨냅니다.
DROP_WITH_CONTENT_TAGS = {
    "script", "style", "noscript", "iframe", "object", "embed",
    "form", "input", "button", "select", "textarea", "svg",
    "link", "meta", "head", "title",
}


def _clean_style_value(raw: str) -> str:
    cleaned = []
    for item in str(raw or "").split(";"):
        if ":" not in item:
            continue
        prop, val = item.split(":", 1)
        prop = prop.strip().lower()
        val = val.strip()
        if prop not in ALLOWED_CSS_PROPERTIES:
            continue
        if any(bad in val.lower() for bad in ("url(", "expression", "javascript:", "behavior:", "<", ">")):
            continue
        cleaned.append(f"{prop}:{val}")
    return ";".join(cleaned)


def _is_safe_url(value: str) -> bool:
    url = str(value or "").strip()
    if not url:
        return False
    if url.startswith(("#", "/")):
        return True
    return bool(re.match(r"^(https?:)?//", url, re.I))


def _sanitize_with_bs4(value: str) -> str:
    """bleach가 없는 환경에서 BeautifulSoup으로 태그/속성을 화이트리스트 처리합니다."""
    from bs4 import BeautifulSoup  # type: ignore

    soup = BeautifulSoup(value, "html.parser")

    for tag in soup.find_all(list(DROP_WITH_CONTENT_TAGS)):
        tag.decompose()

    for tag in soup.find_all(True):
        name = (tag.name or "").lower()
        if name not in ALLOWED_TAGS:
            tag.unwrap()
            continue

        allowed = set(ALLOWED_ATTRIBUTES.get("*", [])) | set(ALLOWED_ATTRIBUTES.get(name, []))
        for attr in list(tag.attrs):
            key = attr.lower()
            if key not in allowed:
                del tag[attr]
                continue
            if key == "style":
                style = _clean_style_value(tag[attr])
                if style:
                    tag[attr] = style
                else:
                    del tag[attr]
            elif key in ("href", "src"):
                if not _is_safe_url(tag[attr]):
                    del tag[attr]

    return str(soup)


def _sanitize_with_regex(value: str) -> str:
    value = re.sub(
        r"<\s*(script|iframe|object|embed|style)[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r'\son\w+\s*=\s*([\'\"]).*?\1', "", value, flags=re.I | re.S)
    value = re.sub(r"\son\w+\s*=\s*[^\s>]+", "", value, flags=re.I)
    value = re.sub(r"javascript\s*:", "", value, flags=re.I)

    def clean_style(match):
        quote = match.group(1)
        cleaned = _clean_style_value(match.group(2))
        if not cleaned:
            return ""
        return f' style={quote}{cleaned}{quote}'

    return re.sub(
        r'\sstyle\s*=\s*([\'\"])(.*?)\1',
        clean_style,
        value,
        flags=re.I | re.S,
    )


def _sanitize_html(value: str) -> str:
    """
    메뉴얼 본문에 필요한 제한된 HTML/CSS만 허용합니다.
    글자색/글자크기, 표 셀 폭·높이를 저장하기 위해 style 속성을 허용하되
    CSS 속성은 화이트리스트로 제한합니다.

    bleach → BeautifulSoup → 정규식 순서로 사용 가능한 방법을 선택합니다.
    """
    value = value or ""

    try:
        import bleach  # type: ignore
        from bleach.css_sanitizer import CSSSanitizer  # type: ignore

        css_sanitizer = CSSSanitizer(allowed_css_properties=ALLOWED_CSS_PROPERTIES)
        return bleach.clean(
            value,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols=["http", "https"],
            css_sanitizer=css_sanitizer,
            strip=True,
        )
    except ImportError:
        pass
    except Exception:
        pass

    try:
        return _sanitize_with_bs4(value)
    except Exception:
        return _sanitize_with_regex(value)


def _first_image_url(sections) -> str:
    for sec in sections:
        content = sec.get("content_html", "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content, flags=re.I)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------
# 외부파일 임포트 helpers
# ---------------------------------------------------------------------
def _make_image_saver(manual_id: int):
    """
    임포트 파서가 문서 안의 이미지(데이터 URI, DOCX 내장 이미지)를
    메뉴얼 폴더에 저장할 때 사용하는 콜백을 만듭니다.
    저장된 파일 목록은 saver.saved 에 쌓이며 임포트 성공 후 DB에 등록합니다.
    """
    folder = _manual_root() / str(manual_id)
    saved = []

    def saver(raw: bytes, ext: str, original_name: str = "") -> str:
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            return ""
        clean_ext = str(ext or "").lower().lstrip(".")
        if clean_ext in {"jpeg", "jpe", "jfif"}:
            clean_ext = "jpg"
        if clean_ext not in ALLOWED_IMAGE_EXTENSIONS:
            return ""

        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.{clean_ext}"
        (folder / filename).write_bytes(raw)
        saved.append((filename, str(original_name or filename)[:255]))
        return url_for("manual.media", manual_id=manual_id, filename=filename)

    saver.saved = saved
    return saver


def _register_imported_images(manual_id: int, saved) -> None:
    if not saved:
        return
    now = _now()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO manual_images(manual_id, filename, original_name, created_at)
            VALUES(?, ?, ?, ?)
            """,
            [(manual_id, filename, original, now) for filename, original in saved],
        )


def _discard_imported_images(manual_id: int, saved) -> None:
    folder = _manual_root() / str(manual_id)
    for filename, _original in saved:
        path = folder / secure_filename(filename)
        if path.exists():
            path.unlink()


def _read_import_file(file):
    """업로드된 임포트 파일을 검증하고 (원본파일명, bytes)를 돌려줍니다."""
    if not file or not file.filename:
        return None, None, "등록할 파일을 선택해 주세요."

    original = str(file.filename).replace("\\", "/").rsplit("/", 1)[-1].strip()
    ext = file_extension(original)
    if ext not in SUPPORTED_IMPORT_EXTENSIONS:
        return None, None, "TXT, MD, HTML, DOCX 파일만 메뉴얼로 등록할 수 있습니다."

    data = file.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        return None, None, "메뉴얼 파일은 20MB 이하만 등록할 수 있습니다."
    if not data:
        return None, None, "파일 내용이 비어 있습니다."

    return original, data, ""


def _normalize_imported_sections(raw_sections):
    """파서 결과를 저장 가능한 형태(제목 보정 + 살균)로 다듬습니다."""
    sections = []
    for idx, sec in enumerate(raw_sections or []):
        title = str(sec.get("title") or "").strip()[:200] or f"{idx + 1}. 제목 없음"
        sections.append(
            {
                "section_no": idx + 1,
                "title": title,
                "description": str(sec.get("description") or "").strip()[:1000],
                "content_html": _sanitize_html(str(sec.get("content_html") or "")),
                "sort_order": idx,
            }
        )
    if not sections:
        sections = [
            {
                "section_no": 1,
                "title": "1. 내용",
                "description": "",
                "content_html": "",
                "sort_order": 0,
            }
        ]
    return sections


def _replace_sections(conn, manual_id: int, sections) -> None:
    conn.execute("DELETE FROM manual_sections WHERE manual_id = ?", (manual_id,))
    conn.executemany(
        """
        INSERT INTO manual_sections(
            manual_id, section_no, title, description, content_html, sort_order
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            (
                manual_id,
                sec["section_no"],
                sec["title"],
                sec["description"],
                sec["content_html"],
                sec["sort_order"],
            )
            for sec in sections
        ],
    )


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------
@manual_bp.get("/")
def list_manuals():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.*,
                COUNT(s.id) AS section_count
            FROM manuals m
            LEFT JOIN manual_sections s ON s.manual_id = m.id
            GROUP BY m.id
            ORDER BY
                CASE WHEN m.status = 'published' THEN 0 ELSE 1 END,
                COALESCE(m.published_at, m.updated_at) DESC,
                m.id DESC
            """
        ).fetchall()
    manuals = [_rowdict(r) for r in rows]
    return render_template(
        "manual/manual_list.html",
        manuals=manuals,
        import_accept=IMPORT_ACCEPT_ATTR,
    )


@manual_bp.get("/new")
@manual_write_required
def new_manual():
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO manuals(title, description, status, created_at, updated_at)
            VALUES(?, ?, 'draft', ?, ?)
            """,
            ("새 메뉴얼", "", now, now),
        )
        manual_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO manual_sections(
                manual_id, section_no, title, description, content_html, sort_order
            ) VALUES(?, 1, ?, '', '', 0)
            """,
            (manual_id, "1. 새 목차"),
        )
    return redirect(url_for("manual.edit_manual", manual_id=manual_id))


@manual_bp.get("/<int:manual_id>/edit")
@manual_write_required
def edit_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_editor.html",
        manual=manual,
        sections=sections,
        manual_payload={"manual": manual, "sections": sections},
        import_accept=IMPORT_ACCEPT_ATTR,
    )


@manual_bp.get("/<int:manual_id>/preview")
@manual_write_required
def preview_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_preview.html",
        manual=manual,
        sections=sections,
        is_preview=True,
    )


@manual_bp.get("/<int:manual_id>")
def view_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    if manual["status"] != "published":
        # Drafts are intentionally not publicly viewable.
        abort(404)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_view.html",
        manual=manual,
        sections=sections,
        is_preview=False,
    )


# ---------------------------------------------------------------------
# Save / publish / delete
# ---------------------------------------------------------------------
@manual_bp.post("/<int:manual_id>/save")
@manual_write_required
def save_manual(manual_id):
    _get_manual_or_404(manual_id)
    payload = request.get_json(silent=True) or {}

    title = (payload.get("title") or "").strip()[:200]
    description = (payload.get("description") or "").strip()[:1000]
    raw_sections = payload.get("sections") or []

    if not title:
        return jsonify(ok=False, message="메뉴얼 제목을 입력해 주세요."), 400
    if not isinstance(raw_sections, list) or not raw_sections:
        return jsonify(ok=False, message="목차를 한 개 이상 만들어 주세요."), 400

    sections = []
    for idx, sec in enumerate(raw_sections):
        sec_title = str(sec.get("title") or "").strip()[:200]
        if not sec_title:
            sec_title = f"{idx + 1}. 제목 없음"
        sections.append(
            {
                "section_no": idx + 1,
                "title": sec_title,
                "description": str(sec.get("description") or "").strip()[:1000],
                "content_html": _sanitize_html(str(sec.get("content_html") or "")),
                "sort_order": idx,
            }
        )

    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE manuals
            SET title = ?, description = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, description, now, manual_id),
        )
        _replace_sections(conn, manual_id, sections)

    return jsonify(ok=True, message="임시저장되었습니다.", updated_at=now)


@manual_bp.post("/<int:manual_id>/publish")
@manual_write_required
def publish_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)

    if not manual["title"].strip():
        return jsonify(ok=False, message="제목을 입력해 주세요."), 400
    if not sections:
        return jsonify(ok=False, message="목차가 없습니다."), 400

    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE manuals
            SET status = 'published',
                published_at = COALESCE(published_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, manual_id),
        )

    return jsonify(
        ok=True,
        message="작성완료되었습니다.",
        view_url=url_for("manual.view_manual", manual_id=manual_id),
    )


@manual_bp.post("/<int:manual_id>/unpublish")
@manual_write_required
def unpublish_manual(manual_id):
    _get_manual_or_404(manual_id)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE manuals SET status='draft', updated_at=? WHERE id=?",
            (now, manual_id),
        )
    return jsonify(ok=True, message="작성중 상태로 변경되었습니다.")


@manual_bp.post("/<int:manual_id>/delete")
@manual_write_required
def delete_manual(manual_id):
    _get_manual_or_404(manual_id)
    _delete_manual_record(manual_id)
    return jsonify(ok=True, message="메뉴얼이 삭제되었습니다.")


def _delete_manual_record(manual_id: int) -> None:
    """메뉴얼 행과 업로드 폴더를 함께 삭제합니다."""
    with _connect() as conn:
        conn.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))

    folder = _manual_root() / str(manual_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------------
# Image upload / delete / media
# ---------------------------------------------------------------------
@manual_bp.post("/api/upload-image")
@manual_write_required
def upload_image():
    try:
        manual_id = int(request.form.get("manual_id", "0"))
    except ValueError:
        manual_id = 0

    _get_manual_or_404(manual_id)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="이미지 파일을 선택해 주세요."), 400

    # 중요:
    # 한글 파일명에 secure_filename()을 먼저 적용하면
    # 예: "캡처이미지.jpg" -> 확장자 판별이 깨지는 환경이 있을 수 있습니다.
    # 따라서 원본 파일명에서 먼저 확장자를 읽고, 실제 서버 저장명만 UUID로 만듭니다.
    original = str(file.filename).replace("\\\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(original).suffix.lower().lstrip(".")

    # 브라우저/캡처 프로그램이 확장자를 누락하거나 특이하게 전달하는 경우
    # MIME 타입으로 한 번 더 JPEG/PNG/WEBP/GIF 여부를 확인합니다.
    mime_ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/pjpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = mime_ext_map.get((file.mimetype or "").lower(), "")

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify(
            ok=False,
            message="PNG, JPG, JPEG, JFIF, WEBP, GIF 이미지만 업로드할 수 있습니다.",
        ), 400

    # JPEG 계열은 서버 저장 확장자를 jpg로 통일합니다.
    if ext in {"jpeg", "jpe", "jfif"}:
        ext = "jpg"

    # DB에는 사용자가 올린 원래 한글 파일명을 보존합니다.
    original = original[:255]

    # Read once to enforce module-level size cap.
    data = file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify(ok=False, message="이미지는 8MB 이하만 업로드할 수 있습니다."), 400

    filename = f"{uuid.uuid4().hex}.{ext}"
    folder = _manual_root() / str(manual_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_images(manual_id, filename, original_name, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (manual_id, filename, original, _now()),
        )

    return jsonify(
        ok=True,
        filename=filename,
        url=url_for("manual.media", manual_id=manual_id, filename=filename),
    )


@manual_bp.post("/api/delete-image")
@manual_write_required
def delete_image():
    payload = request.get_json(silent=True) or {}
    try:
        manual_id = int(payload.get("manual_id", 0))
    except (TypeError, ValueError):
        manual_id = 0
    filename = secure_filename(str(payload.get("filename") or ""))

    _get_manual_or_404(manual_id)
    if not filename:
        return jsonify(ok=False, message="삭제할 이미지가 없습니다."), 400

    with _connect() as conn:
        exists = conn.execute(
            "SELECT id FROM manual_images WHERE manual_id=? AND filename=?",
            (manual_id, filename),
        ).fetchone()
        if not exists:
            return jsonify(ok=False, message="등록된 이미지를 찾을 수 없습니다."), 404
        conn.execute(
            "DELETE FROM manual_images WHERE manual_id=? AND filename=?",
            (manual_id, filename),
        )

    path = _manual_root() / str(manual_id) / filename
    if path.exists():
        path.unlink()

    return jsonify(ok=True)



@manual_bp.post("/api/upload-thumbnail")
@manual_write_required
def upload_thumbnail():
    try:
        manual_id = int(request.form.get("manual_id", "0"))
    except ValueError:
        manual_id = 0

    _get_manual_or_404(manual_id)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="썸네일 이미지 파일을 선택해 주세요."), 400

    original = str(file.filename).replace("\\\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(original).suffix.lower().lstrip(".")

    mime_ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/pjpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = mime_ext_map.get((file.mimetype or "").lower(), "")

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify(
            ok=False,
            message="PNG, JPG, JPEG, JFIF, WEBP, GIF 이미지만 업로드할 수 있습니다.",
        ), 400

    if ext in {"jpeg", "jpe", "jfif"}:
        ext = "jpg"

    data = file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify(ok=False, message="썸네일 이미지는 8MB 이하만 업로드할 수 있습니다."), 400

    filename = f"thumb_{uuid.uuid4().hex}.{ext}"
    folder = _manual_root() / str(manual_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)

    old_filename = ""
    url = url_for("manual.media", manual_id=manual_id, filename=filename)
    with _connect() as conn:
        row = conn.execute(
            "SELECT thumbnail_filename FROM manuals WHERE id=?",
            (manual_id,),
        ).fetchone()
        if row:
            old_filename = str(row["thumbnail_filename"] or "")
        conn.execute(
            """
            UPDATE manuals
            SET thumbnail=?, thumbnail_source='uploaded',
                thumbnail_filename=?, updated_at=?
            WHERE id=?
            """,
            (url, filename, _now(), manual_id),
        )

    if old_filename and old_filename != filename:
        old_path = folder / secure_filename(old_filename)
        if old_path.exists():
            old_path.unlink()

    return jsonify(ok=True, url=url, filename=filename)


@manual_bp.post("/api/delete-thumbnail")
@manual_write_required
def delete_thumbnail():
    payload = request.get_json(silent=True) or {}
    try:
        manual_id = int(payload.get("manual_id", 0))
    except (TypeError, ValueError):
        manual_id = 0

    _get_manual_or_404(manual_id)

    old_filename = ""
    with _connect() as conn:
        row = conn.execute(
            "SELECT thumbnail_filename FROM manuals WHERE id=?",
            (manual_id,),
        ).fetchone()
        if row:
            old_filename = str(row["thumbnail_filename"] or "")
        conn.execute(
            """
            UPDATE manuals
            SET thumbnail='', thumbnail_source='', thumbnail_filename='', updated_at=?
            WHERE id=?
            """,
            (_now(), manual_id),
        )

    if old_filename:
        path = _manual_root() / str(manual_id) / secure_filename(old_filename)
        if path.exists():
            path.unlink()

    return jsonify(ok=True, message="썸네일을 삭제했습니다.")


@manual_bp.get("/media/<int:manual_id>/<path:filename>")
def media(manual_id, filename):
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    folder = _manual_root() / str(manual_id)
    return send_from_directory(folder, safe)


# ---------------------------------------------------------------------
# 외부파일(TXT/MD/HTML/DOCX)로 메뉴얼 등록
# ---------------------------------------------------------------------
@manual_bp.post("/api/import-file")
@manual_write_required
def import_file():
    """
    외부파일을 읽어 메뉴얼로 등록합니다.

    manual_id 를 함께 보내면 해당 메뉴얼 편집 화면에 채워 넣을 목차를 돌려주고,
    보내지 않으면 새 메뉴얼을 만들어 저장한 뒤 편집 화면 주소를 돌려줍니다.
    """
    original, data, error = _read_import_file(request.files.get("file"))
    if error:
        return jsonify(ok=False, message=error), 400

    try:
        target_id = int(request.form.get("manual_id") or 0)
    except (TypeError, ValueError):
        target_id = 0

    created_new = False
    now = _now()

    if target_id > 0:
        _get_manual_or_404(target_id)
        manual_id = target_id
    else:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO manuals(title, description, status, created_at, updated_at)
                VALUES(?, '', 'draft', ?, ?)
                """,
                ("새 메뉴얼", now, now),
            )
            manual_id = cur.lastrowid
        created_new = True

    saver = _make_image_saver(manual_id)

    try:
        document = parse_manual_file(original, data, saver)
    except ManualImportError as exc:
        _discard_imported_images(manual_id, saver.saved)
        if created_new:
            _delete_manual_record(manual_id)
        return jsonify(ok=False, message=str(exc)), 400
    except Exception:
        current_app.logger.exception("메뉴얼 파일 임포트 실패: %s", original)
        _discard_imported_images(manual_id, saver.saved)
        if created_new:
            _delete_manual_record(manual_id)
        return jsonify(ok=False, message="파일을 읽는 중 오류가 발생했습니다. 파일 형식을 확인해 주세요."), 400

    sections = _normalize_imported_sections(document.get("sections"))
    title = str(document.get("title") or "").strip()[:200] or "새 메뉴얼"
    description = str(document.get("description") or "").strip()[:1000]

    _register_imported_images(manual_id, saver.saved)

    if not created_new:
        # 편집 화면에서 불러오기: 저장은 사용자가 [임시저장]을 누를 때 진행합니다.
        return jsonify(
            ok=True,
            manual_id=manual_id,
            title=title,
            description=description,
            image_count=len(saver.saved),
            sections=[
                {
                    "title": sec["title"],
                    "description": sec["description"],
                    "content_html": sec["content_html"],
                }
                for sec in sections
            ],
        )

    with _connect() as conn:
        conn.execute(
            "UPDATE manuals SET title = ?, description = ?, updated_at = ? WHERE id = ?",
            (title, description, _now(), manual_id),
        )
        _replace_sections(conn, manual_id, sections)

    return jsonify(
        ok=True,
        manual_id=manual_id,
        title=title,
        description=description,
        section_count=len(sections),
        image_count=len(saver.saved),
        edit_url=url_for("manual.edit_manual", manual_id=manual_id),
        message=f"'{title}' 메뉴얼을 등록했습니다.",
    )


@manual_bp.post("/api/upload-txt")
@manual_write_required
def upload_txt():
    """이전 버전 호환용 엔드포인트. TXT 내용을 목차 구조로만 변환해 돌려줍니다."""
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="TXT 파일을 선택해 주세요."), 400

    if file_extension(file.filename) not in ("txt", "md", "markdown"):
        return jsonify(ok=False, message=".txt 파일만 불러올 수 있습니다."), 400

    data = file.read(MAX_TXT_BYTES + 1)
    if len(data) > MAX_TXT_BYTES:
        return jsonify(ok=False, message="TXT 파일은 2MB 이하만 불러올 수 있습니다."), 400

    try:
        from routes.manual_import import decode_text

        parsed = parse_markdown(decode_text(data))
    except ManualImportError as exc:
        return jsonify(ok=False, message=str(exc)), 400

    parsed["sections"] = [
        {
            "title": sec["title"],
            "description": sec["description"],
            "content_html": _sanitize_html(sec["content_html"]),
        }
        for sec in parsed["sections"]
    ]
    return jsonify(ok=True, **parsed)


@manual_bp.get("/<int:manual_id>/export.md")
def export_markdown(manual_id):
    """메뉴얼을 다시 등록할 수 있는 .md 파일로 내려받습니다."""
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)

    text = manual_to_markdown(manual, sections)

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", str(manual.get("title") or "메뉴얼")).strip() or "메뉴얼"
    filename = f"{safe_title[:80]}.md"
    quoted = quote(filename)

    return Response(
        text.encode("utf-8-sig"),
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        },
    )


@manual_bp.get("/sample-format")
def sample_format():
    """메뉴얼 파일 작성 양식(.md) 내려받기."""
    filename = "새담메뉴얼_작성양식.md"
    quoted = quote(filename)
    return Response(
        SAMPLE_MARKDOWN.encode("utf-8-sig"),
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
        },
    )
