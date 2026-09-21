"""AI에이전트가 호출할 수 있는 권한 제한형 읽기 전용 조회 도구.

이 모듈은 모델이 만든 SQL을 실행하지 않는다. 모든 SQL과 반환 컬럼은 서버에
고정되어 있으며, 비밀번호·주민번호·계좌번호 같은 민감정보는 조회하지 않는다.
"""

from __future__ import annotations

import calendar
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib.parse import quote

from routes.database import get_db
from routes.menu_access import BOARD_TOP_MENU_LABELS, SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS


MAX_RESULT_LIMIT = 20
WEEKLY_CATEGORIES = ("weekly_report", "weekly", "report", "주간업무보고", "주간업무")
COMPLETED_CONTRACT_STATUSES = {"completed", "signed", "완료", "서명완료"}
SCHOOL_TASK_CATEGORY_LABELS = {
    "community": "커뮤니티", "notice": "공지", "weekly_report": "주간업무", "open_class": "공개수업",
    "expense": "지출결의", "item_request": "비품신청", "work_schedule": "근무표", "billing": "청구",
    "survey": "설문", "reference": "자료실", "director_resources": "센터장자료", "team_review": "팀리뷰",
}
VERIFIED_CONTRACT_STATUSES = {"draft", "pending", "completed", "revoked", "expired"}
VERIFIED_CONTRACT_STATUS_LABELS = {
    "draft": "등록대기", "pending": "서명대기", "completed": "계약완료",
    "revoked": "취소", "expired": "기간만료",
}
SMART_DOCUMENT_STATUS_LABELS = {"draft": "임시저장", "sent": "발송완료", "failed": "발송실패"}


@dataclass
class ToolExecution:
    """모델용 최소 데이터와 브라우저용 안전한 표시 데이터를 분리한다."""

    model_data: dict[str, Any]
    display: dict[str, Any] | None = None


class ToolPermissionError(PermissionError):
    pass


def _clean(value: Any, maximum: int = 120) -> str:
    text = str(value or "").strip()
    return text[:maximum]


_SENSITIVE_PATTERNS = (
    # 주민등록번호
    (re.compile(r"\b\d{6}\s*-\s*\d{7}\b"), "******-*******"),
    # 계좌·카드번호처럼 길게 이어진 숫자열
    (re.compile(r"\b\d{12,19}\b"), "***"),
)


def _mask_sensitive(text: str) -> str:
    """게시물 본문을 모델에 넘기기 전에 주민번호·계좌번호 형태를 가린다."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _plain_text(value: Any, maximum: int = 500) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return _mask_sensitive(re.sub(r"\s+", " ", text).strip())[:maximum]


def _attachment_names(value: Any, maximum: int = 5) -> list[str]:
    """school_posts.filename에 쉼표로 이어 붙인 첨부파일명을 목록으로 돌려준다."""
    names = [name.strip() for name in str(value or "").split(",") if name.strip()]
    return names[:maximum]


def _limit(value: Any, default: int = 10) -> int:
    try:
        return max(1, min(int(value), MAX_RESULT_LIMIT))
    except (TypeError, ValueError):
        return default


def _iso_date(value: Any, fallback: date) -> date:
    text = _clean(value, 10)
    if not text:
        return fallback
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _date_range(arguments: dict[str, Any], *, default: str = "month") -> tuple[date, date]:
    today = date.today()
    if default == "week":
        fallback_start = today - timedelta(days=today.weekday())
        fallback_end = fallback_start + timedelta(days=6)
    elif default == "year":
        fallback_start, fallback_end = date(today.year, 1, 1), date(today.year, 12, 31)
    else:
        fallback_start = date(today.year, today.month, 1)
        fallback_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    start = _iso_date(arguments.get("start_date"), fallback_start)
    end = _iso_date(arguments.get("end_date"), fallback_end)
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    if (end - start).days > 730:
        raise ValueError("한 번에 조회할 수 있는 기간은 최대 2년입니다.")
    return start, end


def _optional_date_range(arguments: dict[str, Any]) -> tuple[date | None, date | None]:
    """특정인 조회처럼 '전체 내역'이 자연스러운 도구용. 날짜를 안 주면 전체 기간으로 본다."""
    start_text = _clean(arguments.get("start_date"), 10)
    end_text = _clean(arguments.get("end_date"), 10)
    if not start_text and not end_text:
        return None, None
    today = date.today()
    start = _iso_date(arguments.get("start_date"), date(2000, 1, 1))
    end = _iso_date(arguments.get("end_date"), today)
    if start > end:
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    if (end - start).days > 730:
        raise ValueError("한 번에 조회할 수 있는 기간은 최대 2년입니다.")
    return start, end


def _is_master(context: dict[str, Any]) -> bool:
    return str(context.get("emp_no") or "").lower() == "admin"


def _level(context: dict[str, Any]) -> int:
    try:
        return int(context.get("user_level", 99))
    except (TypeError, ValueError):
        return 99


def _allowed(context: dict[str, Any], menu_key: str) -> bool:
    return _is_master(context) or bool((context.get("menu_access") or {}).get(menu_key, False))


def _require(context: dict[str, Any], menu_key: str, label: str) -> None:
    if not _allowed(context, menu_key):
        raise ToolPermissionError(f"{label} 조회 권한이 없습니다.")


def _assigned_schools(conn, context: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, school_name, access_key
        FROM schools
        WHERE ? IN (center_director_id, center_director_id_2)
          AND COALESCE(is_active, 1)=1
        ORDER BY year DESC, school_name
        """,
        (_clean(context.get("emp_no"), 30),),
    ).fetchall()
    return [dict(row) for row in rows]


def _school_scope(conn, context: dict[str, Any]) -> tuple[str, list[int]]:
    """기존 학교업무공간 정책(school_bp/school_task: 레벨 1~7은 전체 조회)에 맞춘 전체/담당학교/차단 범위를 반환한다."""
    if _is_master(context) or 1 <= _level(context) <= 7:
        return "all", []
    assigned = _assigned_schools(conn, context)
    if assigned:
        return "assigned", [int(row["id"]) for row in assigned]
    return "none", []


def _period_label(start: date, end: date) -> str:
    return f"{start:%Y.%m.%d} ~ {end:%Y.%m.%d}"


def _table_exists(conn, name: str) -> bool:
    """일부 기능은 최초 사용 시점에야 스키마를 생성하므로 조회 전에 존재를 확인한다."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _table(title: str, message: str, columns: list[tuple[str, str] | tuple[str, str, str]],
           rows: list[dict[str, Any]], actions=None):
    def _column(spec):
        key, label = spec[0], spec[1]
        style = spec[2] if len(spec) > 2 else None
        column = {"key": key, "label": label}
        if style == "wrap":
            # 본문 미리보기처럼 긴 문장은 한 줄로 늘리지 않고 셀 안에서 줄바꿈한다.
            column["wrap"] = True
        elif style:
            column["align"] = style
        return column

    return {
        "type": "table",
        "title": title,
        "message": message,
        "columns": [_column(spec) for spec in columns],
        "rows": rows,
        "actions": actions or [],
    }


def get_attendance_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "attendance_main", "근태관리")
    start, end = _date_range(arguments)
    employee_name = _clean(arguments.get("employee_name"), 50)
    conn = get_db()
    try:
        where = ["a.date BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if employee_name:
            where.append("u.name LIKE ?")
            params.append(f"%{employee_name}%")
        records = conn.execute(
            f"""
            SELECT u.name, COALESCE(u.position, a.position, '') AS position,
                   a.date, a.clock_in_time, a.clock_out_time, a.status
            FROM daily_attendance a
            JOIN users u ON u.emp_no=a.emp_no
            WHERE {' AND '.join(where)}
            ORDER BY u.name, a.date
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    people: dict[str, dict[str, Any]] = {}
    today = date.today().isoformat()
    for record in records:
        name = str(record["name"] or "알 수 없음")
        item = people.setdefault(name, {
            "name": name, "position": record["position"] or "미지정", "registered_days": 0,
            "normal": 0, "late": 0, "early": 0, "absent": 0, "missing_checkout": 0,
        })
        status = str(record["status"] or "")
        item["registered_days"] += 1
        item["late"] += int("지각" in status)
        item["early"] += int("조퇴" in status)
        item["absent"] += int("결근" in status)
        missing = not record["clock_out_time"] and str(record["date"]) < today
        item["missing_checkout"] += int(missing)
        if not any(("지각" in status, "조퇴" in status, "결근" in status, missing)):
            item["normal"] += 1
    rows = list(people.values())
    display_rows = [{
        "name": row["name"], "position": row["position"], "registered_days": row["registered_days"],
        "normal": row["normal"], "late": row["late"], "early": row["early"],
        "absent": row["absent"], "missing_checkout": row["missing_checkout"],
    } for row in rows]
    message = f"{_period_label(start, end)} 등록 근태 {len(records)}건을 집계했습니다."
    if not rows:
        message = f"{_period_label(start, end)}에 조회 가능한 근태 기록이 없습니다."
    display = _table(
        "근태 요약", message,
        [("name", "이름"), ("position", "직급"), ("registered_days", "등록일"),
         ("normal", "정상"), ("late", "지각"), ("early", "조퇴"),
         ("absent", "결근"), ("missing_checkout", "퇴근누락")],
        display_rows,
        [{"label": "근태관리로 이동", "url": "/attendance", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "records": rows,
                          "caveat": "등록된 근태만 집계하며 근무예정표가 없어 무기록 결근은 판정하지 않음"}, display)


def get_best_attendance(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    summary = get_attendance_summary(arguments, context)
    position = _clean(arguments.get("position"), 40)
    limit = _limit(arguments.get("limit"), 5)
    records = [row for row in (summary.model_data.get("records") or []) if str(row.get("name", "")).lower() != "admin"]
    if position:
        records = [row for row in records if position.lower() in str(row.get("position", "")).lower()]
    records.sort(key=lambda row: (
        row["late"] * 3 + row["early"] * 2 + row["absent"] * 5 + row["missing_checkout"] * 2,
        -row["normal"], -row["registered_days"], row["name"],
    ))
    ranked = []
    for index, row in enumerate(records[:limit], 1):
        ranked.append({"rank": index, **row})
    period = str(summary.model_data.get("period") or "")
    message = f"{period} 근태 기록을 기준으로 상위 {len(ranked)}명을 정렬했습니다."
    if not ranked:
        message = f"{period}에 순위를 계산할 근태 기록이 없습니다."
    display = _table(
        "근태 우수자", message,
        [("rank", "순위"), ("name", "이름"), ("position", "직급"), ("normal", "정상"),
         ("late", "지각"), ("early", "조퇴"), ("absent", "결근"),
         ("missing_checkout", "퇴근누락")],
        ranked,
        [{"label": "근태관리로 이동", "url": "/attendance", "style": "primary"}],
    )
    return ToolExecution({"period": period, "ranking": ranked, "scoring": "지각·조퇴·결근·퇴근누락 감점 후 정상 기록 우선",
                          "caveat": summary.model_data.get("caveat")}, display)


def get_missing_weekly_reports(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "school_workspace", "학교업무공간")
    start, end = _date_range(arguments, default="week")
    conn = get_db()
    try:
        scope, school_ids = _school_scope(conn, context)
        if scope == "none":
            raise ToolPermissionError("조회할 수 있는 담당 학교가 없습니다.")
        where = ["COALESCE(s.is_active,1)=1", "(COALESCE(s.center_director_id,'')<>'' OR COALESCE(s.center_director_id_2,'')<>'')"]
        params: list[Any] = []
        if scope == "assigned":
            where.append(f"s.id IN ({','.join('?' for _ in school_ids)})")
            params.extend(school_ids)
        schools = conn.execute(
            f"""
            SELECT s.id, s.school_name, s.access_key,
                   u.name AS director_name, u2.name AS director_name_2
            FROM schools s
            LEFT JOIN users u ON u.emp_no=s.center_director_id
            LEFT JOIN users u2 ON u2.emp_no=s.center_director_id_2
            WHERE {' AND '.join(where)}
            ORDER BY s.school_name
            """, params,
        ).fetchall()
        posted = {int(row["school_id"]) for row in conn.execute(
            f"""
            SELECT DISTINCT school_id FROM school_posts
            WHERE category IN ({','.join('?' for _ in WEEKLY_CATEGORIES)})
              AND date(created_at) BETWEEN ? AND ?
            """, (*WEEKLY_CATEGORIES, start.isoformat(), end.isoformat()),
        ).fetchall()}
    finally:
        conn.close()
    missing = []
    for school in schools:
        if int(school["id"]) in posted:
            continue
        names = [name for name in (school["director_name"], school["director_name_2"]) if name]
        for name in names:
            missing.append({"name": name, "school": school["school_name"]})
    message = f"{_period_label(start, end)} 주간업무 미등록자는 {len(missing)}명입니다."
    display = _table(
        "주간업무 미등록자", message,
        [("name", "센터장"), ("school", "담당 학교")], missing,
        [{"label": "학교업무공간으로 이동", "url": "/school", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "missing_count": len(missing), "people": missing,
                          "basis": "활성 학교의 지정 센터장과 해당 학교 주간업무 게시물 비교"}, display)


EXPENSE_STATUS_LABELS = {"결재대기", "지급대기", "지급완료", "반려"}


def _combined_expense_status(doc_status: Any, payment_status: Any) -> str:
    """routes/expense.py의 _combined_expense_status와 동일한 화면 표시 규칙."""
    doc = str(doc_status or "").strip()
    payment = str(payment_status or "").strip()
    if doc == "반려":
        return "반려"
    if payment == "지급완료":
        return "지급완료"
    if doc == "완료":
        return "지급대기"
    return "결재대기"


def _expense_scope(conn, context: dict[str, Any]) -> tuple[list[str], str]:
    if bool(context.get("center_director_mode")):
        schools = _assigned_schools(conn, context)
        return [str(row["school_name"]) for row in schools], "assigned"
    return [], "all"


def _expense_period_sql(start: date, end: date) -> tuple[str, list[Any]]:
    return (
        "date(printf('%04d-%02d-01', CAST(report_year AS INTEGER), CAST(report_month AS INTEGER))) BETWEEN ? AND ?",
        [date(start.year, start.month, 1).isoformat(), date(end.year, end.month, 1).isoformat()],
    )


def get_expense_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "expense_main", "지출결의")
    start, end = _date_range(arguments, default="year")
    conn = get_db()
    try:
        period_sql, params = _expense_period_sql(start, end)
        where = [period_sql]
        school_names, scope = _expense_scope(conn, context)
        if scope == "assigned":
            if not school_names:
                raise ToolPermissionError("조회할 수 있는 담당 학교 지출결의가 없습니다.")
            where.append(f"(expense_school_name IN ({','.join('?' for _ in school_names)}) OR drafter=?)")
            params.extend(school_names)
            params.append(_clean(context.get("user_name"), 50))
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS report_count, COALESCE(SUM(total_amount),0) AS request_amount,
                   SUM(CASE WHEN doc_status='완료' THEN 1 ELSE 0 END) AS approved_count,
                   COALESCE(SUM(CASE WHEN doc_status='완료' THEN total_amount ELSE 0 END),0) AS approved_amount,
                   SUM(CASE WHEN payment_status='지급완료' THEN 1 ELSE 0 END) AS paid_count,
                   COALESCE(SUM(CASE WHEN payment_status='지급완료' THEN total_amount ELSE 0 END),0) AS paid_amount,
                   SUM(CASE WHEN doc_status='반려' THEN 1 ELSE 0 END) AS rejected_count,
                   COALESCE(SUM(CASE WHEN doc_status='반려' THEN total_amount ELSE 0 END),0) AS rejected_amount
            FROM expense_reports WHERE {' AND '.join(where)}
            """, params,
        ).fetchone()
    finally:
        conn.close()
    data = {
        "report_count": int(row["report_count"] or 0), "request_amount": int(row["request_amount"] or 0),
        "approved_count": int(row["approved_count"] or 0), "approved_amount": int(row["approved_amount"] or 0),
        "paid_count": int(row["paid_count"] or 0), "paid_amount": int(row["paid_amount"] or 0),
        "rejected_count": int(row["rejected_count"] or 0), "rejected_amount": int(row["rejected_amount"] or 0),
    }
    display_rows = [{
        "period": _period_label(start, end), "report_count": data["report_count"],
        "request_amount": f"{data['request_amount']:,}원",
        "approved_count": data["approved_count"], "approved_amount": f"{data['approved_amount']:,}원",
        "paid_count": data["paid_count"], "paid_amount": f"{data['paid_amount']:,}원",
    }]
    display = _table(
        "지출 현황",
        f"{_period_label(start, end)} 지출결의 현황입니다. 신청액은 반려 {data['rejected_count']}건을 포함한 전체 금액입니다.",
        [("period", "기간"), ("report_count", "결의서"), ("request_amount", "신청액", "right"),
         ("approved_count", "결재완료"), ("approved_amount", "결재액", "right"),
         ("paid_count", "지급완료"), ("paid_amount", "지급완료액", "right")],
        display_rows, [{"label": "지출결의로 이동", "url": "/expense", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), **data}, display)


def get_expense_ranking(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "expense_main", "지출결의")
    limit = _limit(arguments.get("limit"), 5)
    keyword = _clean(arguments.get("keyword"), 50)
    has_explicit_dates = bool(_clean(arguments.get("start_date"), 10) or _clean(arguments.get("end_date"), 10))
    if keyword and not has_explicit_dates:
        # 특정인을 지정했는데 기간을 안 줬다면 '총 내역/총액'으로 보고 전체 기간을 본다.
        start, end = None, None
    else:
        start, end = _date_range(arguments, default="year")
    conn = get_db()
    try:
        where: list[str] = []
        params: list[Any] = []
        if start and end:
            period_sql, period_params = _expense_period_sql(start, end)
            where.append(period_sql)
            params.extend(period_params)
        school_names, scope = _expense_scope(conn, context)
        if scope == "assigned":
            if not school_names:
                raise ToolPermissionError("조회할 수 있는 담당 학교 지출결의가 없습니다.")
            where.append(f"(expense_school_name IN ({','.join('?' for _ in school_names)}) OR drafter=?)")
            params.extend(school_names)
            params.append(_clean(context.get("user_name"), 50))
        having_sql = ""
        having_params: list[Any] = []
        if keyword:
            having_sql = "HAVING person LIKE ?"
            having_params = [f"%{keyword}%"]
        where_sql = " AND ".join(where) if where else "1=1"
        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(expense_manager),''), NULLIF(TRIM(drafter),''), '미지정') AS person,
                   COUNT(*) AS report_count,
                   COALESCE(SUM(total_amount),0) AS request_amount,
                   COALESCE(SUM(CASE WHEN doc_status='완료' THEN total_amount ELSE 0 END),0) AS approved_amount,
                   COALESCE(SUM(CASE WHEN payment_status='지급완료' THEN total_amount ELSE 0 END),0) AS paid_amount,
                   COALESCE(SUM(CASE WHEN doc_status='반려' THEN total_amount ELSE 0 END),0) AS rejected_amount,
                   COALESCE(SUM(CASE WHEN doc_status='반려' THEN 1 ELSE 0 END),0) AS rejected_count
            FROM expense_reports WHERE {where_sql}
            GROUP BY person {having_sql}
            ORDER BY request_amount DESC, report_count DESC, person LIMIT ?
            """, (*params, *having_params, limit),
        ).fetchall()
    finally:
        conn.close()
    ranking = [{"rank": index, "name": row["person"], "report_count": int(row["report_count"]),
                "request_amount": int(row["request_amount"]), "approved_amount": int(row["approved_amount"]),
                "paid_amount": int(row["paid_amount"]), "rejected_amount": int(row["rejected_amount"]),
                "rejected_count": int(row["rejected_count"])} for index, row in enumerate(rows, 1)]
    display_rows = [{**row, "request_amount": f"{row['request_amount']:,}원",
                      "approved_amount": f"{row['approved_amount']:,}원",
                      "paid_amount": f"{row['paid_amount']:,}원"} for row in ranking]
    period_label = _period_label(start, end) if start and end else "전체 기간"
    display = _table(
        "지출액 순위",
        f"{period_label} 작성자/지출담당자 기준 {len(ranking)}명입니다. 신청액은 반려 건도 포함한 전체 금액입니다.",
        [("rank", "순위"), ("name", "이름"), ("report_count", "결의서"), ("request_amount", "신청액", "right"),
         ("approved_amount", "결재액", "right"), ("paid_amount", "지급완료액", "right")],
        display_rows, [{"label": "지출결의로 이동", "url": "/expense", "style": "primary"}],
    )
    return ToolExecution({"period": period_label, "ranking": ranking,
                          "basis": "expense_manager 우선, 없으면 drafter 기준",
                          "definition": "신청액=전체 결의서 합계(반려 포함), 결재액=결재완료(doc_status=완료) 합계, "
                                        "지급완료액=지급완료 합계"}, display)


def search_expense_reports(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "expense_main", "지출결의")
    keyword = _clean(arguments.get("keyword"), 50)
    status = _clean(arguments.get("status"), 10)
    if status and status not in EXPENSE_STATUS_LABELS:
        raise ValueError("status는 결재대기, 지급대기, 지급완료, 반려 중 하나여야 합니다.")
    start, end = _optional_date_range(arguments)
    limit = _limit(arguments.get("limit"), 20)
    conn = get_db()
    try:
        where: list[str] = []
        params: list[Any] = []
        if keyword:
            where.append("(expense_manager LIKE ? OR drafter LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        if start and end:
            where.insert(0, "date(expense_date) BETWEEN ? AND ?")
            params[0:0] = [start.isoformat(), end.isoformat()]
        school_names, scope = _expense_scope(conn, context)
        if scope == "assigned":
            if not school_names:
                raise ToolPermissionError("조회할 수 있는 담당 학교 지출결의가 없습니다.")
            where.append(f"(expense_school_name IN ({','.join('?' for _ in school_names)}) OR drafter=?)")
            params.extend(school_names)
            params.append(_clean(context.get("user_name"), 50))
        where_sql = " AND ".join(where) if where else "1=1"
        rows = conn.execute(
            f"""
            SELECT title, expense_org_type, expense_school_name, expense_kind, expense_manager, drafter,
                   doc_status, payment_status, total_amount, expense_date
            FROM expense_reports WHERE {where_sql}
            ORDER BY expense_date DESC, id DESC
            """, params,
        ).fetchall()
    finally:
        conn.close()

    all_items = []
    for row in rows:
        combined_status = _combined_expense_status(row["doc_status"], row["payment_status"])
        if status and combined_status != status:
            continue
        person = str(row["expense_manager"] or "").strip() or str(row["drafter"] or "").strip() or "미지정"
        all_items.append({
            "person": person, "title": row["title"],
            "org": row["expense_school_name"] or row["expense_org_type"] or "본사",
            "category": row["expense_kind"] or "", "status": combined_status,
            "amount": int(row["total_amount"] or 0), "expense_date": str(row["expense_date"] or "")[:10],
        })
    status_counts: dict[str, int] = {}
    for item in all_items:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    total_count = len(all_items)
    total_amount = sum(item["amount"] for item in all_items)
    shown = all_items[:limit]
    summary = ", ".join(f"{key} {value}건" for key, value in status_counts.items()) or "해당 없음"
    period_label = _period_label(start, end) if start and end else "전체 기간"
    who = f"'{keyword}'" if keyword else "전체 인원"
    message = f"{period_label} {who} 지출결의 {total_count}건 (결재대기·반려·지급완료 등 모든 상태 포함, {summary})."
    if total_count > len(shown):
        message += f" 최근 {len(shown)}건만 표시합니다."
    display_rows = [{**item, "amount": f"{item['amount']:,}원"} for item in shown]
    display = _table(
        "지출결의 내역", message,
        [("person", "담당자"), ("title", "문서명"), ("org", "학교/본사"), ("category", "내역"), ("status", "상태"),
         ("amount", "금액", "right"), ("expense_date", "결의일")], display_rows,
        [{"label": "지출결의로 이동", "url": "/expense", "style": "primary"}],
    )
    return ToolExecution({"period": period_label, "keyword": keyword or "전체", "total_count": total_count,
                          "shown_count": len(shown), "total_amount": total_amount,
                          "status_counts": status_counts, "reports": shown}, display)


_KOREAN_DATE = re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_ISOISH_DATE = re.compile(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def _contract_end_date(period: Any) -> date | None:
    text = str(period or "")
    matches = list(_KOREAN_DATE.finditer(text)) or list(_ISOISH_DATE.finditer(text))
    if not matches:
        return None
    # '2026년 6월 29일부터 근무종료일까지'처럼 시작일만 숫자로 있는 계약을
    # 만료일로 오판하지 않는다. 숫자 날짜 뒤에 '까지'가 붙거나, 시작·종료
    # 날짜가 모두 숫자로 기록된 경우에만 마지막 날짜를 종료일로 사용한다.
    explicit_end = [match for match in matches if "까지" in text[match.end(): match.end() + 12]]
    if explicit_end:
        match = explicit_end[-1]
    elif len(matches) >= 2:
        match = matches[-1]
    else:
        return None
    year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def get_contract_expirations(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "verified_contract_admin", "인증전자계약")
    start, end = _date_range(arguments)
    limit = _limit(arguments.get("limit"), 20)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, contract_type, school_name, department, signer_name, contract_data_json "
            "FROM verified_contracts ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    matches, unparsed = [], 0
    for row in rows:
        try:
            data = json.loads(row["contract_data_json"] or "{}")
        except (TypeError, ValueError):
            data = {}
        period = data.get("계약기간") if isinstance(data, dict) else ""
        if not str(period or "").strip():
            continue
        expires = _contract_end_date(period)
        if not expires:
            unparsed += 1
            continue
        if start <= expires <= end:
            matches.append({"name": row["signer_name"], "school": row["school_name"], "department": row["department"],
                            "contract_type": row["contract_type"], "expires_on": expires.isoformat(), "period": str(period)})
    matches.sort(key=lambda item: (item["expires_on"], item["name"]))
    matches = matches[:limit]
    message = f"{_period_label(start, end)} 계약 만료 예정 {len(matches)}건입니다."
    if unparsed:
        message += f" 종료일을 판별할 수 없는 계약 {unparsed}건은 제외했습니다."
    display = _table(
        "계약 만료 예정자", message,
        [("name", "성명"), ("school", "학교"), ("department", "부서"),
         ("contract_type", "계약구분"), ("expires_on", "만료일")], matches,
        [{"label": "인증전자계약관리로 이동", "url": "/verified-contract/admin", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "contracts": matches, "unparsed_count": unparsed}, display)


def get_incomplete_contracts(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "verified_contract_admin", "인증전자계약")
    limit = _limit(arguments.get("limit"), 20)
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT signer_name, school_name, department, status, created_at
            FROM verified_contracts
            WHERE LOWER(COALESCE(status,'')) NOT IN ('completed','signed')
            ORDER BY id DESC LIMIT ?
            """, (limit,),
        ).fetchall()
    finally:
        conn.close()
    items = [{"name": row["signer_name"], "school": row["school_name"], "department": row["department"],
              "status": row["status"], "created_at": str(row["created_at"] or "")[:10]} for row in rows]
    display = _table(
        "미완료 인증전자계약", f"완료되지 않은 인증전자계약 {len(items)}건입니다.",
        [("name", "성명"), ("school", "학교"),
         ("department", "부서"), ("status", "상태"), ("created_at", "등록일")], items,
        [{"label": "인증전자계약관리로 이동", "url": "/verified-contract/admin", "style": "primary"}],
    )
    return ToolExecution({"count": len(items), "contracts": items}, display)


def search_employees(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "contacts_main", "본사연락망")
    keyword = _clean(arguments.get("keyword"), 80)
    position = _clean(arguments.get("position"), 40)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        where = ["u.emp_no<>'admin'", "COALESCE(u.status,'승인')='승인'"]
        params: list[Any] = []
        if keyword:
            where.append("(u.name LIKE ? OR u.department LIKE ? OR u.custom_department LIKE ? OR s.school_name LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        if position:
            where.append("u.position LIKE ?")
            params.append(f"%{position}%")
        rows = conn.execute(
            f"""
            SELECT u.name, u.position, COALESCE(NULLIF(u.custom_department,''),u.department,'') AS department,
                   COALESCE(u.custom_team,'') AS team, u.phone, u.email,
                   GROUP_CONCAT(DISTINCT s.school_name) AS schools
            FROM users u
            LEFT JOIN schools s ON u.emp_no IN (s.center_director_id,s.center_director_id_2)
                              AND COALESCE(s.is_active,1)=1
            WHERE {' AND '.join(where)}
            GROUP BY u.id ORDER BY u.level, u.name LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    people = [{"name": row["name"], "position": row["position"] or "미지정",
               "department": row["department"] or "미지정", "team": row["team"] or "",
               "phone": row["phone"] or "", "email": row["email"] or "",
               "school": row["schools"] or ""} for row in rows]
    display = _table(
        "직원 검색", f"조건에 맞는 직원 {len(people)}명입니다.",
        [("name", "이름"), ("position", "직급"), ("department", "소속"),
         ("team", "팀"), ("phone", "연락처"), ("email", "이메일"), ("school", "담당 학교")], people,
        [{"label": "본사연락망으로 이동", "url": "/contacts", "style": "primary"}],
    )
    return ToolExecution({"count": len(people), "employees": people,
                          "privacy": "연락처·이메일은 본사연락망에 공개된 정보로 제공됨. 주소·주민번호·계좌번호는 제공하지 않음"}, display)


def search_school_data(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "school_workspace", "학교업무공간")
    keyword = _clean(arguments.get("keyword"), 80)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        scope, school_ids = _school_scope(conn, context)
        if scope == "none":
            raise ToolPermissionError("조회할 수 있는 담당 학교가 없습니다.")
        where = ["COALESCE(s.is_active,1)=1"]
        params: list[Any] = []
        if scope == "assigned":
            where.append(f"s.id IN ({','.join('?' for _ in school_ids)})")
            params.extend(school_ids)
        if keyword:
            where.append("(s.school_name LIKE ? OR u.name LIKE ? OR u2.name LIKE ? OR s.contract_subject LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        rows = conn.execute(
            f"""
            SELECT s.school_name, s.year, s.contract_subject, s.office_location, s.access_key,
                   s.office_phone, s.school_address, s.school_phone, s.school_email,
                   u.name AS director_name, u2.name AS director_name_2
            FROM schools s
            LEFT JOIN users u ON u.emp_no=s.center_director_id
            LEFT JOIN users u2 ON u2.emp_no=s.center_director_id_2
            WHERE {' AND '.join(where)} ORDER BY s.year DESC,s.school_name LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    schools = [{"school": row["school_name"], "year": row["year"],
                "director": ", ".join(filter(None, [row["director_name"], row["director_name_2"]])) or "미지정",
                "subject": row["contract_subject"] or "", "office": row["office_location"] or "",
                "address": row["school_address"] or "", "phone": row["school_phone"] or "",
                "office_phone": row["office_phone"] or "", "email": row["school_email"] or "",
                "link": f"/school/{quote(str(row['access_key'] or ''))}" if row["access_key"] else "/school"}
               for row in rows]
    display = _table(
        "학교 정보", f"조건에 맞는 학교 {len(schools)}곳입니다.",
        [("school", "학교"), ("year", "연도"), ("director", "센터장"), ("subject", "계약과목"),
         ("office", "지원실"), ("address", "학교주소"), ("phone", "학교전화"),
         ("office_phone", "지원실전화"), ("email", "이메일")],
        schools,
        [{"label": "학교업무공간으로 이동", "url": "/school", "style": "primary"}],
    )
    model_schools = [{key: value for key, value in row.items() if key != "link"} for row in schools]
    return ToolExecution({"count": len(model_schools), "schools": model_schools}, display)


BOARD_MENU_KEYS = {"noti": "board_noti", "archive": "board_archive", "manual": "board_manual"}


def _board_is_visible(context: dict[str, Any], board_en: str, access: int, read: int) -> bool:
    if not (_is_master(context) or _level(context) <= int(access) and _level(context) <= int(read)):
        return False
    mapped = BOARD_MENU_KEYS.get(board_en)
    return not mapped or _allowed(context, mapped)


def _normalize_board_text(text: str) -> str:
    return str(text or "").strip().lower().replace(" ", "")


def _board_name_matches(needle: str, *candidates: str) -> bool:
    normalized_needle = _normalize_board_text(needle)
    if not normalized_needle:
        return False
    for candidate in candidates:
        normalized_candidate = _normalize_board_text(candidate)
        if normalized_candidate and (normalized_needle in normalized_candidate or normalized_candidate in normalized_needle):
            return True
    return False


def search_board_posts(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    keyword = _clean(arguments.get("keyword"), 100)
    board_query = _clean(arguments.get("board"), 40)
    start, end = _optional_date_range(arguments)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        configs = conn.execute("SELECT name_en,name_kr,lvl_access,lvl_read FROM board_config").fetchall()
        visible = [row for row in configs if _board_is_visible(context, row["name_en"], row["lvl_access"], row["lvl_read"])]
        if not board_query and keyword:
            # 모델이 "OO 게시판 보여줘" 같은 요청도 board가 아닌 keyword에 게시판 이름을 넣는 경우가 있어,
            # keyword가 게시판 이름과 일치하면 내용 검색 대신 그 게시판 열람으로 처리한다.
            board_matched = [row for row in visible
                              if _board_name_matches(keyword, row["name_kr"],
                                                      BOARD_TOP_MENU_LABELS.get(row["name_en"], ""), row["name_en"])]
            if board_matched:
                board_query, keyword = keyword, ""
        if board_query:
            visible = [row for row in visible
                       if _board_name_matches(board_query, row["name_kr"],
                                               BOARD_TOP_MENU_LABELS.get(row["name_en"], ""), row["name_en"])]
        board_names = [row["name_en"] for row in visible]
        labels = {row["name_en"]: BOARD_TOP_MENU_LABELS.get(row["name_en"], row["name_kr"]) for row in visible}
        if not board_names:
            if board_query:
                raise ValueError(f"‘{board_query}’ 게시판을 찾을 수 없거나 조회 권한이 없습니다.")
            raise ToolPermissionError("검색할 수 있는 사내 게시판이 없습니다.")
        if not keyword and not board_query:
            raise ValueError("게시판 검색어 또는 게시판 이름을 입력해 주세요.")
        where = [f"board_en IN ({','.join('?' for _ in board_names)})"]
        params: list[Any] = [*board_names]
        if start and end:
            where.append("date(created_at) BETWEEN ? AND ?")
            params.extend([start.isoformat(), end.isoformat()])
        if keyword:
            where.append("(title LIKE ? OR content LIKE ? OR author LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        rows = conn.execute(
            f"""
            SELECT id,board_en,title,content,author,created_at
            FROM board_posts
            WHERE {' AND '.join(where)}
            ORDER BY created_at DESC,id DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    posts = [{"board": labels.get(row["board_en"], row["board_en"]), "title": row["title"],
              "author": row["author"], "date": str(row["created_at"] or "")[:10],
              "excerpt": _plain_text(row["content"], 240),
              "link": f"/board/{quote(str(row['board_en']))}/read/{int(row['id'])}"} for row in rows]
    label = f"‘{keyword}’ 관련" if keyword else f"‘{board_query}’" if board_query else ""
    display = {"type": "list", "title": "게시판 검색 결과", "message": f"{label} 게시물 {len(posts)}건입니다.",
               "items": posts, "actions": []}
    model_posts = [{key: value for key, value in row.items() if key != "link"} for row in posts]
    return ToolExecution({"keyword": keyword, "board": board_query, "posts": model_posts}, display)


def search_manuals(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "board_manual", "업무메뉴얼")
    keyword = _clean(arguments.get("keyword"), 100)
    if keyword and _board_name_matches(keyword, "업무메뉴얼"):
        # "업무메뉴얼(에 등록된 게시물) 보여줘"처럼 메뉴 이름 자체가 keyword로 들어오면 전체 목록으로 본다.
        keyword = ""
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        if not _table_exists(conn, "manuals"):
            items: list[dict[str, Any]] = []
        else:
            where = ["1=1"]
            params: list[Any] = []
            if keyword:
                like = f"%{keyword}%"
                where.append(
                    "(m.title LIKE ? OR m.description LIKE ? OR "
                    "EXISTS (SELECT 1 FROM manual_sections s WHERE s.manual_id=m.id "
                    "AND (s.title LIKE ? OR s.content_html LIKE ?)))"
                )
                params.extend([like, like, like, like])
            rows = conn.execute(
                f"""
                SELECT m.id, m.title, m.description, m.status, m.created_by, m.updated_at, m.published_at
                FROM manuals m WHERE {' AND '.join(where)}
                ORDER BY CASE WHEN m.status='published' THEN 0 ELSE 1 END,
                         COALESCE(m.published_at, m.updated_at) DESC LIMIT ?
                """, (*params, limit),
            ).fetchall()
            items = [{"title": row["title"], "description": _plain_text(row["description"], 160),
                      "status": "게시됨" if row["status"] == "published" else "임시저장",
                      "author": row["created_by"] or "", "updated_at": str(row["updated_at"] or "")[:10],
                      "link": f"/manual/{int(row['id'])}"} for row in rows]
    finally:
        conn.close()
    label = f"‘{keyword}’ 관련" if keyword else ""
    display = {"type": "list", "title": "업무메뉴얼 검색 결과", "message": f"{label} 업무메뉴얼 {len(items)}건입니다.",
               "items": items, "actions": [{"label": "업무메뉴얼로 이동", "url": "/manual", "style": "primary"}]}
    model_items = [{key: value for key, value in item.items() if key != "link"} for item in items]
    return ToolExecution({"keyword": keyword, "manuals": model_items}, display)


def search_gallery(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    keyword = _clean(arguments.get("keyword"), 100)
    start, end = _date_range(arguments, default="year")
    limit = _limit(arguments.get("limit"), 12)
    conn = get_db()
    try:
        where, params = ["date(p.created_at) BETWEEN ? AND ?"], [start.isoformat(), end.isoformat()]
        gallery_allowed = _allowed(context, "gallery_main")
        assigned_schools = _assigned_schools(conn, context)
        school_gallery_key = ""
        if _is_master(context) or 1 <= _level(context) <= 6:
            school_row = conn.execute(
                "SELECT access_key FROM schools WHERE COALESCE(is_active,1)=1 "
                "AND COALESCE(access_key,'')<>'' ORDER BY year DESC,school_name LIMIT 1"
            ).fetchone()
            school_gallery_key = str(school_row["access_key"] or "") if school_row else ""
        elif assigned_schools:
            school_gallery_key = str(assigned_schools[0].get("access_key") or "")
        school_allowed = bool(school_gallery_key)
        scope_parts = []
        if gallery_allowed:
            scope_parts.append("p.school_id IS NULL")
        if school_allowed:
            # 기존 학교갤러리는 학교별 행이 아니라 school_id=0 공유 범위를 사용한다.
            scope_parts.append("p.school_id=0")
        if not scope_parts:
            raise ToolPermissionError("갤러리 조회 권한이 없습니다.")
        where.append(f"({' OR '.join(scope_parts)})")
        if keyword:
            like = f"%{keyword}%"
            where.append("(p.title LIKE ? OR p.content LIKE ? OR p.author LIKE ? OR s.school_name LIKE ?)")
            params.extend([like, like, like, like])
        rows = conn.execute(
            f"""
            SELECT p.id,p.title,p.author,p.created_at,p.school_id,s.school_name,s.access_key,
                   g.filename,g.thumb_name
            FROM gall2_posts p JOIN gall2 g ON g.post_id=p.id
            LEFT JOIN schools s ON s.id=p.school_id
            WHERE {' AND '.join(where)}
            ORDER BY p.created_at DESC,p.id DESC,g.id LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        if row["school_id"] is None:
            prefix, link = "/gall2", "/gall2"
        else:
            school_key = quote(school_gallery_key)
            prefix, link = f"/school/{school_key}/gallery", f"/school/{school_key}/gallery"
        items.append({"thumbnail": f"{prefix}/thumb/{quote(str(row['thumb_name']))}",
                      "image_url": f"{prefix}/raw/{quote(str(row['filename']))}",
                      "title": row["title"], "meta": f"{'학교갤러리' if row['school_id'] is not None else '사내 갤러리'} · {str(row['created_at'] or '')[:10]}",
                      "link": link})
    display = {"type": "gallery", "title": "갤러리 검색 결과",
               "message": f"{('‘' + keyword + '’ 관련 ') if keyword else '최근 '}사진 {len(items)}장을 찾았습니다.",
               "items": items, "actions": [{"label": "사내 갤러리로 이동", "url": "/gall2", "style": "primary"}]}
    model_items = [{"title": item["title"], "meta": item["meta"]} for item in items]
    return ToolExecution({"keyword": keyword, "count": len(items), "images": model_items}, display)


def search_documents(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    keyword = _clean(arguments.get("keyword"), 100)
    if not keyword:
        raise ValueError("문서 검색어를 입력해 주세요.")
    start, end = _optional_date_range(arguments)
    limit = _limit(arguments.get("limit"), 10)
    results: list[dict[str, Any]] = []
    conn = get_db()
    try:
        configs = conn.execute("SELECT name_en,name_kr,lvl_access,lvl_read FROM board_config").fetchall()
        visible = [row for row in configs if _board_is_visible(context, row["name_en"], row["lvl_access"], row["lvl_read"])]
        board_names = [row["name_en"] for row in visible]
        labels = {row["name_en"]: BOARD_TOP_MENU_LABELS.get(row["name_en"], row["name_kr"]) for row in visible}
        if board_names:
            like = f"%{keyword}%"
            where = ["p.board_en IN ({})".format(','.join('?' for _ in board_names)),
                     "(p.title LIKE ? OR p.content LIKE ? OR f.original_name LIKE ?)"]
            params: list[Any] = [*board_names, like, like, like]
            if start and end:
                where.append("date(p.created_at) BETWEEN ? AND ?")
                params.extend([start.isoformat(), end.isoformat()])
            rows = conn.execute(
                f"""
                SELECT p.id,p.board_en,p.title,p.created_at,f.original_name
                FROM board_files f JOIN board_posts p ON p.id=f.post_id
                WHERE {' AND '.join(where)}
                ORDER BY p.created_at DESC LIMIT ?
                """, (*params, limit),
            ).fetchall()
            results.extend({"name": row["original_name"], "source": labels.get(row["board_en"], row["board_en"]),
                            "title": row["title"], "date": str(row["created_at"] or "")[:10],
                            "link": f"/board/{quote(str(row['board_en']))}/read/{int(row['id'])}"} for row in rows)
        if len(results) < limit and _allowed(context, "school_workspace"):
            scope, school_ids = _school_scope(conn, context)
            if scope != "none":
                like = f"%{keyword}%"
                where = ["(p.title LIKE ? OR p.content LIKE ? OR p.filename LIKE ? OR s.school_name LIKE ? OR p.author LIKE ?)",
                         "COALESCE(p.filename,'')<>''"]
                params = [like, like, like, like, like]
                if start and end:
                    where.append("date(p.created_at) BETWEEN ? AND ?")
                    params.extend([start.isoformat(), end.isoformat()])
                if scope == "assigned":
                    where.append(f"p.school_id IN ({','.join('?' for _ in school_ids)})")
                    params.extend(school_ids)
                rows = conn.execute(
                    f"""
                    SELECT p.id,p.title,p.filename,p.created_at,s.school_name,s.access_key
                    FROM school_posts p JOIN schools s ON s.id=p.school_id
                    WHERE {' AND '.join(where)} ORDER BY p.created_at DESC LIMIT ?
                    """, (*params, limit - len(results)),
                ).fetchall()
                results.extend({"name": str(row["filename"] or "").split(",")[0], "source": row["school_name"],
                                "title": row["title"], "date": str(row["created_at"] or "")[:10],
                                "link": f"/school/{quote(str(row['access_key'] or ''))}"} for row in rows)
    finally:
        conn.close()
    results = results[:limit]
    display = {"type": "files", "title": "문서 검색 결과", "message": f"‘{keyword}’ 관련 파일 {len(results)}건입니다.",
               "items": results, "actions": []}
    model_results = [{key: value for key, value in row.items() if key != "link"} for row in results]
    return ToolExecution({"keyword": keyword, "files": model_results}, display)


def search_certificate_requests(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "document_admin", "증명서 발급관리")
    keyword = _clean(arguments.get("keyword"), 50)
    certificate_type = _clean(arguments.get("certificate_type"), 40)
    status = _clean(arguments.get("status"), 20)
    start, end = _date_range(arguments, default="year")
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        where = ["COALESCE(applied_date,'') BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if keyword:
            where.append("applicant_name LIKE ?")
            params.append(f"%{keyword}%")
        if certificate_type:
            where.append("certificate_type LIKE ?")
            params.append(f"%{certificate_type}%")
        if status:
            where.append("status=?")
            params.append(status)
        rows = conn.execute(
            f"""
            SELECT applicant_name, applicant_type, certificate_type, status,
                   applied_date, issued_date, workplace, purpose
            FROM certificate_requests
            WHERE {' AND '.join(where)}
            ORDER BY applied_date DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    items = [dict(row) for row in rows]
    display = _table(
        "증명서 발급 현황", f"조건에 맞는 증명서 신청 {len(items)}건입니다.",
        [("applicant_name", "성명"), ("applicant_type", "구분"), ("certificate_type", "증명서종류"),
         ("status", "상태"), ("applied_date", "신청일"), ("issued_date", "발급일"),
         ("workplace", "근무장소"), ("purpose", "용도")], items,
        [{"label": "증명서 발급관리로 이동", "url": "/document/admin", "style": "primary"}],
    )
    return ToolExecution({"count": len(items), "requests": items}, display)


def get_school_task_status(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    category = _clean(arguments.get("category"), 30)
    if category and category not in SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS:
        raise ValueError("category는 정해진 학교업무 분류값 중 하나여야 합니다.")
    if category:
        _require(context, SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS[category], "학교업무처리")
    elif not (_allowed(context, "school_tasks") or _allowed(context, "school_center_boards")):
        raise ToolPermissionError("학교업무처리 조회 권한이 없습니다.")
    status = _clean(arguments.get("status"), 20)
    keyword = _clean(arguments.get("keyword"), 80)
    start, end = _optional_date_range(arguments)
    limit = _limit(arguments.get("limit"), 15)
    conn = get_db()
    try:
        scope, school_ids = _school_scope(conn, context)
        if scope == "none":
            raise ToolPermissionError("조회할 수 있는 담당 학교가 없습니다.")
        where: list[str] = []
        params: list[Any] = []
        if start and end:
            where.append("date(p.created_at) BETWEEN ? AND ?")
            params.extend([start.isoformat(), end.isoformat()])
        if scope == "assigned":
            where.append(f"p.school_id IN ({','.join('?' for _ in school_ids)})")
            params.extend(school_ids)
        if category:
            where.append("p.category=?")
            params.append(category)
        if status:
            where.append("p.status=?")
            params.append(status)
        if keyword:
            where.append("s.school_name LIKE ?")
            params.append(f"%{keyword}%")
        where_sql = " AND ".join(where) if where else "1=1"
        rows = conn.execute(
            f"""
            SELECT s.school_name, p.category, p.title, p.content, p.author, p.filename,
                   p.status, p.processor, p.created_at
            FROM school_posts p JOIN schools s ON s.id=p.school_id
            WHERE {where_sql}
            ORDER BY p.created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    items: list[dict[str, Any]] = []
    model_items: list[dict[str, Any]] = []
    for row in rows:
        # 본문은 에디터가 저장한 HTML이므로 태그를 걷어낸 평문으로 모델에 넘긴다.
        # 요약을 만들려면 제목만으로는 부족해 본문 전문에 가까운 길이를 함께 전달한다.
        body = _plain_text(row["content"], 1200)
        attachments = _attachment_names(row["filename"])
        base = {"school": row["school_name"],
                "category": SCHOOL_TASK_CATEGORY_LABELS.get(row["category"], row["category"]),
                "title": row["title"], "status": row["status"] or "접수",
                "processor": row["processor"] or "", "created_at": str(row["created_at"] or "")[:10]}
        items.append({**base, "preview": body[:100] + ("…" if len(body) > 100 else "")})
        model_items.append({**base, "author": row["author"] or "",
                            "content": body or "(본문 없음)",
                            "attachments": attachments,
                            "attachment_count": len(attachments)})
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    summary = ", ".join(f"{key} {value}건" for key, value in counts.items()) or "해당 없음"
    period_label = _period_label(start, end) if start and end else "전체 기간"
    empty_bodies = sum(1 for item in model_items if item["content"] == "(본문 없음)")
    display = _table(
        "학교업무 처리 현황", f"{period_label} 조건에 맞는 업무 {len(items)}건 ({summary}).",
        [("school", "학교"), ("category", "분류"), ("title", "제목"), ("preview", "내용", "wrap"),
         ("status", "상태"), ("processor", "처리자"), ("created_at", "등록일")], items,
        [{"label": "학교업무처리로 이동", "url": "/school/tasks", "style": "primary"}],
    )
    return ToolExecution({"period": period_label, "count": len(items), "tasks": model_items,
                          "status_counts": counts,
                          "content_included": True,
                          "truncated": len(items) >= limit,
                          "empty_content_count": empty_bodies,
                          "note": "tasks[].content는 게시물 본문 평문(최대 1200자)이다. "
                                  "요약 요청이면 이 본문을 근거로 요약한다. "
                                  "truncated가 true면 조회 한도까지만 가져온 것이므로 최신 건 기준임을 밝힌다. "
                                  "본문이 '(본문 없음)'이면 첨부파일로만 제출된 건이므로 "
                                  "attachments 목록을 근거로 안내한다."}, display)


_WEEKLY_TASK_CATEGORY_COLUMNS = {
    "회의": ("cat_meeting_title", "cat_meeting_time"),
    "면접": ("cat_interview_title", "cat_interview_time"),
    "미팅": ("cat_miting_title", "cat_miting_time"),
    "외근": ("cat_out_title", "cat_out_time"),
    "기타": ("cat_etc_title", "cat_etc_time"),
}


def get_weekly_schedule_overview(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    """메인 화면의 '나의 주간업무 요약'(로그인 사용자, 오늘~+6일)과
    '주간 자원관리 현황'(전체 직원, 이번 주 월~일)을 동일한 기준으로 조회한다."""
    current_user = str(context.get("user_name") or "")
    today = date.today()
    anchor_text = _clean(arguments.get("start_date"), 10)
    anchor = _iso_date(arguments.get("start_date"), today) if anchor_text else today
    week_start = anchor - timedelta(days=anchor.weekday())
    week_end = week_start + timedelta(days=6)
    rolling_start = today
    rolling_end = today + timedelta(days=6)
    query_start = min(week_start, rolling_start)
    query_end = max(week_end, rolling_end)

    conn = get_db()
    try:
        task_rows = conn.execute(
            "SELECT * FROM tasks WHERE date BETWEEN ? AND ?",
            (query_start.isoformat(), query_end.isoformat()),
        ).fetchall()
        attendance_rows = conn.execute(
            "SELECT * FROM attendance WHERE status='승인' AND start_date <= ? AND end_date >= ?",
            (query_end.isoformat(), query_start.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for row in task_rows:
        try:
            day = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        for cat, (title_col, time_col) in _WEEKLY_TASK_CATEGORY_COLUMNS.items():
            title = row[title_col]
            if title:
                events.append({"owner": str(row["owner"] or ""), "category": cat, "title": str(title),
                               "time": str(row[time_col] or ""), "start": day, "end": day})

    for row in attendance_rows:
        try:
            start_day = datetime.strptime(str(row["start_date"])[:10], "%Y-%m-%d").date()
            end_day = datetime.strptime(str(row["end_date"])[:10], "%Y-%m-%d").date() - timedelta(days=1)
        except ValueError:
            continue
        if end_day < start_day:
            end_day = start_day
        events.append({"owner": str(row["owner"] or ""), "category": "근태/휴가",
                       "title": str(row["type"] or "근태/휴가"), "time": "", "start": start_day, "end": end_day})

    def _overlaps(event: dict[str, Any], window_start: date, window_end: date) -> bool:
        return event["start"] <= window_end and event["end"] >= window_start

    def _format(event: dict[str, Any]) -> dict[str, Any]:
        period = event["start"].isoformat() if event["start"] == event["end"] \
            else f'{event["start"].isoformat()}~{event["end"].isoformat()}'
        return {"owner": event["owner"], "category": event["category"], "title": event["title"],
                "time": event["time"], "period": period}

    my_items = [_format(e) for e in sorted(
        (e for e in events if e["owner"] == current_user and _overlaps(e, rolling_start, rolling_end)),
        key=lambda e: (e["start"], e["end"]))]
    all_items = [_format(e) for e in sorted(
        (e for e in events if _overlaps(e, week_start, week_end)),
        key=lambda e: (e["start"], e["end"], e["owner"]))]

    my_range = f"{rolling_start.isoformat()}~{rolling_end.isoformat()}"
    all_range = f"{week_start.isoformat()}~{week_end.isoformat()}"
    my_message = f"{current_user or '알 수 없음'}님의 나의 주간업무 요약({my_range}) {len(my_items)}건입니다."
    all_message = f"주간 자원관리 현황({all_range}) 전체 {len(all_items)}건입니다."

    display = {
        "type": "sections", "title": "주간일정", "message": f"{my_message} {all_message}",
        "sections": [
            _table("나의 주간업무 요약", my_message,
                   [("period", "일자"), ("category", "구분"), ("title", "제목"), ("time", "시간")], my_items),
            _table("주간 자원관리 현황", all_message,
                   [("period", "일자"), ("owner", "담당자"), ("category", "구분"), ("title", "제목"), ("time", "시간")],
                   all_items),
        ],
        "actions": [],
    }
    return ToolExecution(
        {"my_week": {"range": my_range, "count": len(my_items), "items": my_items},
         "all_week": {"range": all_range, "count": len(all_items), "items": all_items}},
        display,
    )


def get_payroll_campaign_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "payroll_main", "스마트 명세서 발송")
    start, end = _date_range(arguments)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        where = ["date(p.created_at) BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        summary_row = conn.execute(
            f"""
            SELECT COUNT(*) AS campaign_count, COALESCE(SUM(sent_count),0) AS sent_total,
                   COALESCE(SUM(failed_count),0) AS failed_total
            FROM payroll_campaigns p WHERE {' AND '.join(where)}
            """, params,
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT COALESCE(u.name, p.owner_emp_no) AS owner_name, p.subject, p.status,
                   p.total_count, p.sent_count, p.failed_count, p.created_at
            FROM payroll_campaigns p LEFT JOIN users u ON u.emp_no=p.owner_emp_no
            WHERE {' AND '.join(where)}
            ORDER BY p.created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    campaigns = [{"owner": row["owner_name"] or "미지정", "subject": row["subject"], "status": row["status"],
                  "total_count": row["total_count"], "sent_count": row["sent_count"],
                  "failed_count": row["failed_count"], "created_at": str(row["created_at"] or "")[:10]}
                 for row in rows]
    data = {"campaign_count": int(summary_row["campaign_count"] or 0),
            "sent_total": int(summary_row["sent_total"] or 0), "failed_total": int(summary_row["failed_total"] or 0)}
    display = _table(
        "명세서 발송 현황",
        f"{_period_label(start, end)} 발송 캠페인 {data['campaign_count']}건, 발송 {data['sent_total']}건, 실패 {data['failed_total']}건입니다.",
        [("owner", "발송자"), ("subject", "제목"), ("status", "상태"), ("total_count", "대상"),
         ("sent_count", "발송"), ("failed_count", "실패"), ("created_at", "등록일")], campaigns,
        [{"label": "스마트 명세서 발송으로 이동", "url": "/payroll", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), **data, "campaigns": campaigns}, display)


def get_mail_campaign_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "ai_mail_main", "스마트 메일 발송")
    start, end = _date_range(arguments)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        where = ["date(c.created_at) BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        summary_row = conn.execute(
            f"""
            SELECT COUNT(*) AS campaign_count, COALESCE(SUM(sent_count),0) AS sent_total,
                   COALESCE(SUM(failed_count),0) AS failed_total
            FROM ai_mail_campaigns c WHERE {' AND '.join(where)}
            """, params,
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT COALESCE(u.name, c.owner_emp_no) AS owner_name, c.name, c.subject, c.group_name,
                   c.status, c.sent_count, c.failed_count, c.created_at
            FROM ai_mail_campaigns c LEFT JOIN users u ON u.emp_no=c.owner_emp_no
            WHERE {' AND '.join(where)}
            ORDER BY c.created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    campaigns = [{"owner": row["owner_name"] or "미지정", "name": row["name"], "subject": row["subject"],
                  "group_name": row["group_name"], "status": row["status"], "sent_count": row["sent_count"],
                  "failed_count": row["failed_count"], "created_at": str(row["created_at"] or "")[:10]}
                 for row in rows]
    data = {"campaign_count": int(summary_row["campaign_count"] or 0),
            "sent_total": int(summary_row["sent_total"] or 0), "failed_total": int(summary_row["failed_total"] or 0)}
    display = _table(
        "메일 발송 현황",
        f"{_period_label(start, end)} 발송 캠페인 {data['campaign_count']}건, 발송 {data['sent_total']}건, 실패 {data['failed_total']}건입니다.",
        [("owner", "발송자"), ("name", "캠페인"), ("subject", "제목"), ("group_name", "수신그룹"),
         ("status", "상태"), ("sent_count", "발송"), ("failed_count", "실패"), ("created_at", "등록일")], campaigns,
        [{"label": "스마트 메일 발송으로 이동", "url": "/ai-mail", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), **data, "campaigns": campaigns}, display)


def get_smart_document_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "smart_document_main", "스마트 공문발송")
    start, end = _date_range(arguments)
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        if not _table_exists(conn, "smart_document_history"):
            return ToolExecution({"period": _period_label(start, end), "count": 0, "documents": []},
                                  _table("스마트 공문 발송 현황", "아직 발송 이력이 없습니다.",
                                         [("owner", "작성자"), ("title", "제목"), ("recipient", "수신처"),
                                          ("subject", "주제"), ("status", "상태"), ("issue_date", "발행일"),
                                          ("created_at", "등록일")], [],
                                         [{"label": "스마트 공문발송으로 이동", "url": "/smart-document", "style": "primary"}]))
        where = ["date(h.created_at) BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        summary_row = conn.execute(
            f"SELECT COUNT(*) AS doc_count FROM smart_document_history h WHERE {' AND '.join(where)}", params,
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT COALESCE(u.name, h.owner_emp_no) AS owner_name, h.title, h.recipient, h.subject,
                   h.status, h.issue_date, h.created_at
            FROM smart_document_history h LEFT JOIN users u ON u.emp_no=h.owner_emp_no
            WHERE {' AND '.join(where)}
            ORDER BY h.created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    documents = [{"owner": row["owner_name"] or "미지정", "title": row["title"], "recipient": row["recipient"],
                  "subject": row["subject"],
                  "status": SMART_DOCUMENT_STATUS_LABELS.get(row["status"], row["status"]),
                  "issue_date": row["issue_date"],
                  "created_at": str(row["created_at"] or "")[:10]} for row in rows]
    count = int(summary_row["doc_count"] or 0)
    display = _table(
        "스마트 공문 발송 현황", f"{_period_label(start, end)} 공문 {count}건입니다.",
        [("owner", "작성자"), ("title", "제목"), ("recipient", "수신처"), ("subject", "주제"), ("status", "상태"),
         ("issue_date", "발행일"), ("created_at", "등록일")], documents,
        [{"label": "스마트 공문발송으로 이동", "url": "/smart-document", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "count": count, "documents": documents}, display)


def get_parent_notification_summary(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "parent_notifications", "학부모알림전송")
    kind = _clean(arguments.get("kind"), 30)
    start, end = _date_range(arguments)
    conn = get_db()
    try:
        if not _table_exists(conn, "parent_notifications"):
            return ToolExecution(
                {"period": _period_label(start, end), "breakdown": [], "sent_total": 0, "failed_total": 0},
                _table("학부모알림 발송 현황", "아직 발송 이력이 없습니다.",
                       [("kind", "종류"), ("target_type", "대상"), ("notification_count", "건수"),
                        ("total_count", "대상인원"), ("sent_count", "발송"), ("failed_count", "실패")], [],
                       [{"label": "학부모알림전송으로 이동", "url": "/parent-notifications", "style": "primary"}]),
            )
        where = ["date(created_at) BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if kind:
            where.append("kind=?")
            params.append(kind)
        rows = conn.execute(
            f"""
            SELECT kind, COALESCE(target_type,'') AS target_type, COUNT(*) AS notification_count,
                   COALESCE(SUM(total_count),0) AS total_count, COALESCE(SUM(sent_count),0) AS sent_count,
                   COALESCE(SUM(failed_count),0) AS failed_count
            FROM parent_notifications WHERE {' AND '.join(where)}
            GROUP BY kind, target_type ORDER BY notification_count DESC
            """, params,
        ).fetchall()
    finally:
        conn.close()
    breakdown = [dict(row) for row in rows]
    total_sent = sum(int(row["sent_count"]) for row in breakdown)
    total_failed = sum(int(row["failed_count"]) for row in breakdown)
    display = _table(
        "학부모알림 발송 현황", f"{_period_label(start, end)} 발송 {total_sent}건, 실패 {total_failed}건입니다.",
        [("kind", "종류"), ("target_type", "대상"), ("notification_count", "건수"),
         ("total_count", "대상인원"), ("sent_count", "발송"), ("failed_count", "실패")], breakdown,
        [{"label": "학부모알림전송으로 이동", "url": "/parent-notifications", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "breakdown": breakdown,
                          "sent_total": total_sent, "failed_total": total_failed,
                          "privacy": "학생·보호자 개인정보는 집계에서 제외됨"}, display)


def search_ebooks(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "ebook_library", "e리플렛")
    keyword = _clean(arguments.get("keyword"), 80)
    kind = _clean(arguments.get("kind"), 20).lower()
    if kind and kind not in {"leaflet", "text"}:
        raise ValueError("kind는 leaflet 또는 text여야 합니다.")
    limit = _limit(arguments.get("limit"), 10)
    conn = get_db()
    try:
        if not _table_exists(conn, "ebooks"):
            return ToolExecution({"count": 0, "ebooks": []},
                                  _table("e리플렛 검색 결과", "아직 등록된 자료가 없습니다.",
                                         [("title", "제목"), ("author", "저자"), ("kind", "구분"),
                                          ("created_by", "등록자"), ("created_at", "등록일"), ("page_count", "페이지")], [],
                                         [{"label": "e리플렛으로 이동", "url": "/ebook", "style": "primary"}]))
        where: list[str] = []
        params: list[Any] = []
        if keyword:
            where.append("(e.title LIKE ? OR e.author LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        if kind:
            where.append("e.kind=?")
            params.append(kind)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"""
            SELECT e.title, e.author, e.kind, e.created_by, e.created_at, COUNT(p.id) AS page_count
            FROM ebooks e LEFT JOIN ebook_pages p ON p.ebook_id=e.id
            {clause}
            GROUP BY e.id ORDER BY e.created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
    finally:
        conn.close()
    items = [{"title": row["title"], "author": row["author"] or "",
              "kind": "e리플렛" if row["kind"] == "leaflet" else "eBook",
              "created_by": row["created_by"], "created_at": str(row["created_at"] or "")[:10],
              "page_count": int(row["page_count"] or 0)} for row in rows]
    display = _table(
        "e리플렛 검색 결과", f"조건에 맞는 자료 {len(items)}건입니다.",
        [("title", "제목"), ("author", "저자"), ("kind", "구분"), ("created_by", "등록자"),
         ("created_at", "등록일"), ("page_count", "페이지")], items,
        [{"label": "e리플렛으로 이동", "url": "/ebook", "style": "primary"}],
    )
    return ToolExecution({"count": len(items), "ebooks": items}, display)


_VERIFIED_CONTRACT_STATUS_BY_LABEL = {label: key for key, label in VERIFIED_CONTRACT_STATUS_LABELS.items()}


def search_verified_contracts(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "verified_contract_admin", "인증전자계약관리")
    status = _clean(arguments.get("status"), 20)
    status = _VERIFIED_CONTRACT_STATUS_BY_LABEL.get(status, status.lower())
    if status and status not in VERIFIED_CONTRACT_STATUSES:
        raise ValueError("status는 draft, pending, completed, revoked, expired 중 하나여야 합니다.")
    keyword = _clean(arguments.get("keyword"), 60)
    contract_type = _clean(arguments.get("contract_type"), 40)
    start, end = _date_range(arguments, default="year")
    limit = _limit(arguments.get("limit"), 15)
    conn = get_db()
    try:
        if not _table_exists(conn, "verified_contracts"):
            return ToolExecution({"period": _period_label(start, end), "count": 0, "contracts": [], "status_counts": {}},
                                  _table("인증전자계약 현황", "아직 등록된 인증전자계약이 없습니다.",
                                         [("contract_type", "계약구분"), ("school", "학교"), ("department", "부서"),
                                          ("signer_name", "성명"), ("status", "상태"), ("created_at", "등록일"),
                                          ("signed_at", "서명일")], [],
                                         [{"label": "인증전자계약관리로 이동", "url": "/verified-contract/admin", "style": "primary"}]))
        where = ["date(created_at) BETWEEN ? AND ?"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if status:
            where.append("LOWER(COALESCE(status,''))=?")
            params.append(status)
        if contract_type:
            where.append("contract_type LIKE ?")
            params.append(f"%{contract_type}%")
        if keyword:
            where.append("(signer_name LIKE ? OR school_name LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        rows = conn.execute(
            f"""
            SELECT contract_type, school_name, department, signer_name, status, created_at, signed_at
            FROM verified_contracts WHERE {' AND '.join(where)}
            ORDER BY created_at DESC LIMIT ?
            """, (*params, limit),
        ).fetchall()
        status_rows = conn.execute(
            """
            SELECT COALESCE(status,'') AS status, COUNT(*) AS cnt
            FROM verified_contracts WHERE date(created_at) BETWEEN ? AND ?
            GROUP BY status
            """, (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()
    items = [{"contract_type": row["contract_type"], "school": row["school_name"], "department": row["department"],
              "signer_name": row["signer_name"],
              "status": VERIFIED_CONTRACT_STATUS_LABELS.get(row["status"], row["status"]),
              "created_at": str(row["created_at"] or "")[:10],
              "signed_at": str(row["signed_at"] or "")[:10] if row["signed_at"] else ""} for row in rows]
    status_counts = {
        VERIFIED_CONTRACT_STATUS_LABELS.get(row["status"], row["status"] or "미지정"): int(row["cnt"])
        for row in status_rows
    }
    summary = ", ".join(f"{key} {value}건" for key, value in status_counts.items()) or "해당 없음"
    display = _table(
        "인증전자계약 현황", f"{_period_label(start, end)} 조건에 맞는 계약 {len(items)}건 ({summary}).",
        [("contract_type", "계약구분"), ("school", "학교"), ("department", "부서"),
         ("signer_name", "성명"), ("status", "상태"), ("created_at", "등록일"), ("signed_at", "서명일")], items,
        [{"label": "인증전자계약관리로 이동", "url": "/verified-contract/admin", "style": "primary"}],
    )
    return ToolExecution({"period": _period_label(start, end), "count": len(items), "contracts": items,
                          "status_counts": status_counts}, display)


APPROVAL_BOX_LABELS = {
    "pending": "수신 대기함",
    "my_drafts": "내 기안함",
    "rejected": "반려함",
    "completed": "결재/수신 완료",
    "reference": "참조함",
    "archive": "완료 문서함",
}


def _approval_name_set(value: Any) -> set[str]:
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _approval_boxes(conn, current_user: str, is_master: bool) -> dict[str, list]:
    """routes/approval.py의 index() 문서함 분류 규칙을 그대로 따른다."""
    pending_rows = conn.execute(
        """SELECT * FROM approvals
           WHERE (approver_1=? AND status='대기') OR (approver_2=? AND status='1차승인')
           ORDER BY created_at DESC""",
        (current_user, current_user),
    ).fetchall()
    draft_rows = conn.execute(
        """SELECT * FROM approvals
           WHERE drafter=? AND status NOT IN ('완료','반려','임시저장')
           ORDER BY created_at DESC""",
        (current_user,),
    ).fetchall()
    rejected_rows = conn.execute(
        """SELECT * FROM approvals
           WHERE drafter=? AND status='반려'
           ORDER BY updated_at DESC""",
        (current_user,),
    ).fetchall()
    completed_rows = conn.execute(
        "SELECT * FROM approvals WHERE status='완료' ORDER BY updated_at DESC"
    ).fetchall()

    def belongs_completed(row) -> bool:
        direct = {str(row["drafter"] or "").strip(), str(row["approver_1"] or "").strip(),
                  str(row["approver_2"] or "").strip()}
        direct |= _approval_name_set(row["receivers"])
        return current_user in direct

    def can_view(row) -> bool:
        if is_master:
            return True
        allowed = {str(row["drafter"] or "").strip(), str(row["approver_1"] or "").strip(),
                   str(row["approver_2"] or "").strip()}
        allowed |= _approval_name_set(row["receivers"])
        allowed |= _approval_name_set(row["cc_receivers"])
        return current_user in allowed

    return {
        "pending": list(pending_rows),
        "my_drafts": list(draft_rows),
        "rejected": list(rejected_rows),
        "completed": [row for row in completed_rows if belongs_completed(row)],
        "reference": [row for row in completed_rows if current_user in _approval_name_set(row["cc_receivers"])],
        "archive": [row for row in completed_rows if can_view(row)],
    }


def get_approval_status(arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    _require(context, "approval_main", "사내결재")
    current_user = _clean(context.get("user_name"), 50)
    if not current_user:
        raise ValueError("로그인 사용자 정보를 확인할 수 없습니다.")
    box = _clean(arguments.get("box"), 20).lower()
    limit = _limit(arguments.get("limit"), 10)

    conn = get_db()
    try:
        boxes = _approval_boxes(conn, current_user, _is_master(context))
    finally:
        conn.close()

    if not box or box == "summary":
        summary_rows = [{"box": label, "count": len(boxes[key])} for key, label in APPROVAL_BOX_LABELS.items()]
        display = _table(
            "사내결재 현황", f"{current_user}님의 문서함별 건수입니다.",
            [("box", "문서함"), ("count", "건수", "right")], summary_rows,
            [{"label": "사내결재로 이동", "url": "/approval", "style": "primary"}],
        )
        return ToolExecution({"user": current_user, "counts": {key: len(value) for key, value in boxes.items()}},
                              display)

    if box not in APPROVAL_BOX_LABELS:
        raise ValueError("box는 pending, my_drafts, rejected, completed, reference, archive 중 하나여야 합니다.")

    rows = boxes[box][:limit]
    items = [
        {
            "doc_type": row["doc_type"] or "",
            "title": row["title"] or "(제목 없음)",
            "drafter": row["drafter"] or "",
            "status": row["status"] or "",
            "date": str(row["updated_at"] or row["created_at"] or "")[:16],
        }
        for row in rows
    ]
    label = APPROVAL_BOX_LABELS[box]
    display = _table(
        label, f"{current_user}님의 {label} {len(boxes[box])}건 중 {len(items)}건입니다.",
        [("doc_type", "종류"), ("title", "제목"), ("drafter", "기안자"), ("status", "상태"), ("date", "일시")], items,
        [{"label": "사내결재로 이동", "url": "/approval", "style": "primary"}],
    )
    return ToolExecution({"user": current_user, "box": box, "count": len(boxes[box]), "items": items}, display)


def _nullable_string(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


def _nullable_integer(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}


DATE_PROPERTIES = {
    "start_date": _nullable_string("조회 시작일(YYYY-MM-DD). 생략 의미면 null"),
    "end_date": _nullable_string("조회 종료일(YYYY-MM-DD). 생략 의미면 null"),
}

TOOL_DEFINITIONS = [
    _tool("get_attendance_summary", "기간별로 전체 직원(또는 employee_name으로 특정 직원)의 근태 등록, 정상, 지각, 조퇴, 결근, 퇴근누락을 집계한다.",
          {**DATE_PROPERTIES, "employee_name": _nullable_string("특정 직원 이름 필터. 전체 직원이면 null")}),
    _tool("get_best_attendance", "근태 감점과 정상 출근 기록을 기준으로 우수자를 순위화한다.",
          {**DATE_PROPERTIES, "limit": _nullable_integer("결과 인원. 기본 5, 최대 20"),
           "position": _nullable_string("센터장 등 직급 필터. 없으면 null")}),
    _tool("get_missing_weekly_reports", "활성 학교의 지정 센터장과 주간업무 게시물을 비교해 미등록자를 찾는다.", DATE_PROPERTIES),
    _tool("get_expense_summary", "기간별 지출결의 건수와 신청액(반려 포함 전체)·결재액·지급완료액을 집계한다.", DATE_PROPERTIES),
    _tool("get_expense_ranking", "지출담당자 또는 작성자별 신청액·결재액·지급완료액 순위를 계산한다. keyword로 특정인만 조회하면 그 사람의 합계액 확인에도 쓸 수 있다. keyword를 주고 기간을 생략하면 그 사람의 전체 기간 합계를 계산한다.",
          {**DATE_PROPERTIES, "keyword": _nullable_string("특정 담당자/작성자 성명. 전체 순위면 null"),
           "limit": _nullable_integer("결과 인원. 기본 5, 최대 20")}),
    _tool("search_expense_reports",
          "지출결의서 내역을 결재대기·지급대기·지급완료·반려 등 상태 구분 없이 전부 조회하고 상태별 건수를 함께 "
          "보여준다. keyword로 특정 담당자·작성자만 볼 수도 있고, keyword를 비우면 전체 인원의 내역을 함께 조회한다. "
          "'총 내역', '전체 내역'뿐 아니라 '지출결의 냈어?', '지출결의서 올린 사람', '~있어?', '확인해줘'처럼 "
          "기간을 콕 집어 말하지 않은 질문이면 이번 달로 임의로 좁히지 말고 start_date/end_date를 비워서 전체 기간을 조회해야 한다.",
          {"keyword": _nullable_string("담당자 또는 작성자 성명. 특정 인물 없이 전체 인원이면 null"),
           "status": _nullable_string("결재대기, 지급대기, 지급완료, 반려 중 하나로 필터. 없으면 전체"),
           "start_date": _nullable_string("조회 시작일(YYYY-MM-DD). 기간을 특정하지 않았다면 null로 두면 전체 기간을 조회한다"),
           "end_date": _nullable_string("조회 종료일(YYYY-MM-DD). 기간을 특정하지 않았다면 null로 두면 전체 기간을 조회한다"),
           "limit": _nullable_integer("최대 표시 건수. 기본 20, 최대 20")}),
    _tool("get_contract_expirations", "계약기간 문자열에서 종료일을 추출해 기간 내 만료 예정자를 찾는다.",
          {**DATE_PROPERTIES, "limit": _nullable_integer("결과 건수. 기본 20, 최대 20")}),
    _tool("get_incomplete_contracts", "인증전자계약 중 완료되지 않은 계약을 찾는다.",
          {"limit": _nullable_integer("결과 건수. 기본 20, 최대 20")}),
    _tool("search_employees", "승인된 직원의 이름, 직급, 소속, 팀, 연락처, 이메일, 담당 학교를 검색한다. "
          "연락처·이메일은 본사연락망 메뉴에 공개된 정보이므로 함께 제공한다. 주소·주민번호·계좌번호 등은 반환하지 않는다.",
          {"keyword": _nullable_string("이름·소속·학교 검색어. 전체면 null"),
           "position": _nullable_string("직급 필터. 없으면 null"), "limit": _nullable_integer("최대 20")}),
    _tool("search_school_data", "학교명, 센터장, 계약과목으로 학교 기본정보를 검색한다. 학교주소, 학교전화, 지원실전화, "
          "이메일 등 연락처 정보도 함께 반환하므로 학교 연락처·주소를 물으면 이 도구를 쓴다.",
          {"keyword": _nullable_string("학교명·센터장·과목 검색어. 전체면 null"), "limit": _nullable_integer("최대 20")}),
    _tool("search_board_posts", "읽기 권한이 있는 사내 게시판(상단메뉴 '업무공간'의 게시판, 게시판마다 별도 메뉴)의 게시물을 "
          "검색하거나 특정 게시판의 최근 게시물을 조회한다. 업무메뉴얼은 이 도구가 아니라 search_manuals를 쓴다.",
          {"board": _nullable_string(
              "상단메뉴에 보이는 게시판 이름 그대로. 예: " + ", ".join(BOARD_TOP_MENU_LABELS.values()) +
              ". 특정 게시판으로 좁히지 않으면 null"),
           "keyword": _nullable_string("제목·내용·작성자 검색어. 최근 게시물만 보려면 null"),
           **DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("search_manuals", "상단메뉴 '업무메뉴얼'에 등록된 사용법 문서를 제목·설명·본문으로 검색하거나 최근 목록을 조회한다.",
          {"keyword": _nullable_string("제목·설명·본문 검색어. 최근 목록만 보려면 null"),
           "limit": _nullable_integer("최대 20")}),
    _tool("search_gallery", "권한 범위의 사내 또는 학교 갤러리 사진을 검색한다.",
          {"keyword": _nullable_string("제목·내용·작성자·학교 검색어. 최근 사진이면 null"), **DATE_PROPERTIES,
           "limit": _nullable_integer("최대 20")}),
    _tool("search_documents", "권한이 있는 게시판과 학교업무공간에서 실제 첨부파일이 있는 게시물만 검색한다(학교명 검색어도 지원). "
          "첨부파일 유무와 상관없이 특정 학교의 게시물 전체를 보려면 get_school_task_status를 사용한다.",
          {"keyword": {"type": "string", "description": "파일명·게시물 제목·학교명·작성자 검색어"}, **DATE_PROPERTIES,
           "limit": _nullable_integer("최대 20")}),
    _tool("search_certificate_requests", "증명서 발급 신청 현황을 성명·종류·상태로 검색한다. 주민번호 등 민감정보는 반환하지 않는다.",
          {"keyword": _nullable_string("성명 검색어. 전체면 null"),
           "certificate_type": _nullable_string("증명서종류 필터. 없으면 null"),
           "status": _nullable_string("상태 필터(대기/발급완료 등). 없으면 null"),
           **DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_school_task_status", "학교업무처리와 센터장 게시판(비품신청·청구·설문 등)의 상태와 본문 내용을 학교·분류·상태별로 조회한다. "
          "각 게시물의 본문 평문이 tasks[].content로 함께 반환되므로 '주간업무보고 내용 요약해줘'처럼 "
          "본문 요약·분석을 요구하는 질문도 이 도구 하나로 처리한다(category='weekly_report'). 본문을 못 본다고 답하지 마라. "
          "특정 학교(그 학교 센터장이 올린 게시물 포함)의 전체 게시물을 볼 때는 keyword에 학교명을 넣어 이 도구를 쓴다. 첨부파일 유무는 무관하다. "
          "'주간일정'처럼 개인/전사 캘린더 일정을 물으면 이 도구가 아니라 get_weekly_schedule_overview를 사용한다.",
          {"category": _nullable_string(
              "community, notice, weekly_report, open_class, expense, item_request, work_schedule, "
              "billing, survey, reference, director_resources, team_review 중 하나. 전체면 null"),
           "status": _nullable_string("상태 필터. 없으면 null"),
           "keyword": _nullable_string("학교명 검색어. 없으면 null"),
           **DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_weekly_schedule_overview",
          "'주간일정', '이번주 일정', '내 일정 알려줘'처럼 캘린더성 주간 일정을 물으면 이 도구를 쓴다. "
          "메인 화면 위젯과 동일하게 (1) 로그인한 사용자의 '나의 주간업무 요약'(오늘부터 7일간 개인 일정: 회의/면접/미팅/외근/기타/근태·휴가)과 "
          "(2) '주간 자원관리 현황'(이번 주 월~일 전체 직원의 동일한 일정)을 함께 반환한다. "
          "학교의 근무표 게시판이나 학교업무처리 현황과는 다른 기능이며, 그건 get_school_task_status를 쓴다.",
          {"start_date": _nullable_string(
              "주간 자원관리 현황 기준 주(월~일)에 포함할 임의의 날짜(YYYY-MM-DD). '이번 주'면 null로 두면 오늘 기준 이번 주를 조회한다.")}),
    _tool("get_payroll_campaign_summary", "전체 직원의 스마트 명세서 발송 캠페인 건수·발송·실패 현황을 발송자별로 집계한다. 계좌번호·급여액은 반환하지 않는다.",
          {**DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_mail_campaign_summary", "전체 직원의 스마트 메일 발송 캠페인 건수·발송·실패 현황을 발송자별로 집계한다.",
          {**DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_smart_document_summary", "전체 직원의 스마트 공문발송 이력 건수와 최근 발송 목록을 작성자별로 조회한다.",
          {**DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_parent_notification_summary", "학부모알림전송 현황을 종류·대상별로 집계한다. 학생·보호자 개인정보는 반환하지 않는다.",
          {"kind": _nullable_string("알림 종류 필터. 없으면 null"), **DATE_PROPERTIES}),
    _tool("search_ebooks", "e리플렛·eBook 자료를 제목·저자로 검색한다.",
          {"keyword": _nullable_string("제목·저자 검색어. 없으면 null"),
           "kind": _nullable_string("leaflet 또는 text. 없으면 null"), "limit": _nullable_integer("최대 20")}),
    _tool("search_verified_contracts", "인증전자계약을 상태·구분·성명·학교로 검색한다. 주민번호·계좌·연락처는 반환하지 않는다.",
          {"status": _nullable_string("draft, pending, completed, revoked, expired 중 하나. 없으면 null"),
           "keyword": _nullable_string("성명·학교명 검색어. 없으면 null"),
           "contract_type": _nullable_string("계약구분 필터. 없으면 null"),
           **DATE_PROPERTIES, "limit": _nullable_integer("최대 20")}),
    _tool("get_approval_status", "로그인한 사용자의 사내 전자결재함(수신 대기함/내 기안함/반려함/결재·수신 완료/참조함/완료 문서함) 현황을 조회한다. "
          "box를 비우면 6개 문서함 건수 요약을, box를 지정하면 그 문서함의 문서 목록(종류·제목·기안자·상태·일시)을 보여준다. "
          "본인이 기안·결재·수신·참조로 관련된 문서만 조회되며 타인의 문서함은 조회할 수 없다.",
          {"box": _nullable_string(
              "pending(수신 대기함), my_drafts(내 기안함), rejected(반려함), completed(결재/수신 완료), "
              "reference(참조함), archive(완료 문서함) 중 하나. 전체 건수 요약이면 null"),
           "limit": _nullable_integer("문서 목록 최대 건수. 기본 10, 최대 20")}),
]


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], ToolExecution]] = {
    "get_attendance_summary": get_attendance_summary,
    "get_best_attendance": get_best_attendance,
    "get_missing_weekly_reports": get_missing_weekly_reports,
    "get_expense_summary": get_expense_summary,
    "get_expense_ranking": get_expense_ranking,
    "search_expense_reports": search_expense_reports,
    "get_contract_expirations": get_contract_expirations,
    "get_incomplete_contracts": get_incomplete_contracts,
    "search_employees": search_employees,
    "search_school_data": search_school_data,
    "search_board_posts": search_board_posts,
    "search_manuals": search_manuals,
    "search_gallery": search_gallery,
    "search_documents": search_documents,
    "search_certificate_requests": search_certificate_requests,
    "get_school_task_status": get_school_task_status,
    "get_weekly_schedule_overview": get_weekly_schedule_overview,
    "get_payroll_campaign_summary": get_payroll_campaign_summary,
    "get_mail_campaign_summary": get_mail_campaign_summary,
    "get_smart_document_summary": get_smart_document_summary,
    "get_parent_notification_summary": get_parent_notification_summary,
    "search_ebooks": search_ebooks,
    "search_verified_contracts": search_verified_contracts,
    "get_approval_status": get_approval_status,
}


def execute_tool(name: str, arguments: dict[str, Any], context: dict[str, Any]) -> ToolExecution:
    handler = TOOL_HANDLERS.get(str(name or ""))
    if handler is None:
        raise ValueError("허용되지 않은 AI 도구입니다.")
    if not isinstance(arguments, dict):
        raise ValueError("도구 인자가 올바르지 않습니다.")
    return handler(arguments, context)
