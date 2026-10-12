"""통합관리에서 사용하는 SOLAPI 설정과 메시지 발송 도우미."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

from .database import get_db
from .security import load_credential_secret


SETTINGS_KEY = "solapi_settings"
SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send-many/detail"
CONTRACT_PUBLIC_ORIGIN = "https://works.saedam.org"
# 알림톡 실패 시 나가는 대체문자 제목. LMS는 제목이 비면 접수되지 않는다.
ALIMTALK_FALLBACK_SUBJECT = "새담 전자계약 안내"
# 설문조사 등 링크 문자에 사용하는 기본 기관명. 통합관리에서 바꿀 수 있다.
DEFAULT_SENDER_NAME = "새담"
# SMS 1건의 최대 본문 길이(EUC-KR 기준 바이트). 넘으면 LMS로 전환한다.
SMS_BYTE_LIMIT = 90
# 한 번의 send-many 요청에 담을 최대 건수.
BULK_CHUNK_SIZE = 100
# 설문조사 링크 알림톡 기본값(카카오 채널·승인 템플릿). 통합관리 > 솔라피설정에서 바꿀 수 있다.
SURVEY_KAKAO_PF_ID = "KA01PF260904081609259ib9u2XiMmdC"
SURVEY_KAKAO_TEMPLATE_ID = "KA01TP260928063952776ZNYzvHisk7Z"
# 학부모 안내 링크 알림톡 기본값(승인된 템플릿, 설문조사와 같은 카카오 채널). 통합관리 > 솔라피설정에서 바꿀 수 있다.
PARENT_KAKAO_PF_ID = "KA01PF260904081609259ib9u2XiMmdC"
PARENT_KAKAO_TEMPLATE_ID = "KA01TP261007082213496ANXs94PHj3a"
SOLAPI_TEMPLATE_URL = "https://api.solapi.com/kakao/v2/templates/{template_id}"
# 템플릿 조회 결과를 잠시 보관해 미리보기·발송 때마다 API를 부르지 않게 한다.
TEMPLATE_CACHE_SECONDS = 600

# 솔라피설정 화면의 [카카오 알림톡 템플릿] 칸. 기능마다 승인된 템플릿을 하나씩 연결한다.
# 새 기능이 알림톡을 쓰게 되면 여기에 한 줄을 더하고 환경변수·저장 항목(…_pf_id / …_template_id)을 만든다.
KAKAO_TEMPLATE_SLOTS = (
    {
        "key": "contract",
        "label": "인증전자계약",
        "used_in": "사내결재 > 강사전자계약·임직원전자계약 — 계약 링크 알림톡",
        "variables": "#{이름} #{url}",
        "pf_field": "pf_id",
        "template_field": "template_id",
        "pf_blank": "인증전자계약 알림톡에만 씁니다. 문자만 쓰면 비워 두세요.",
    },
    {
        "key": "survey",
        "label": "설문조사",
        "used_in": "학교관리 > 설문조사 — 알림톡 발송",
        "variables": "#{이름} #{제목} #{기관명} #{url} #{기간} 등 자동 입력",
        "pf_field": "survey_pf_id",
        "template_field": "survey_template_id",
        "pf_blank": "비워 두면 기본값을 씁니다.",
    },
    {
        "key": "parent",
        "label": "학부모 안내 링크",
        "used_in": "학교지원 > 학부모·강사 통합지원 > 학부모 안내 > 링크 발송",
        "variables": "#{학교명} #{url}",
        "pf_field": "parent_pf_id",
        "template_field": "parent_template_id",
        "pf_blank": "비워 두면 설문조사와 같은 채널을 씁니다.",
        # 솔라피 템플릿에 쓴 변수 이름이 다르면 화면에서 맞춘다(예: #{school_name}).
        "variable_names_key": "parent_variable_names",
        "variable_fields": (
            {"source": "school", "label": "학교 이름이 들어갈 변수", "field": "parent_var_school"},
            {"source": "url", "label": "안내 주소가 들어갈 변수", "field": "parent_var_url"},
        ),
    },
)
MAX_EXTRA_TEMPLATES = 30

# 링크 안내 알림톡(학부모 안내)이 채우는 변수는 학교 이름과 주소 두 가지다.
# 템플릿에 쓴 변수 이름은 솔라피설정에서 바꿀 수 있고, 바꾸지 않은 변수는 이름 속 단어(학교·school·url·링크…)로 짐작한다.
LINK_VARIABLE_DEFAULTS = {"school": "학교명", "url": "url"}
LINK_VARIABLE_LABELS = {"school": "학교 이름", "url": "학부모 안내 주소"}
_LINK_VARIABLE_RULES = (
    ("url", ("url", "link", "링크", "주소", "바로가기")),
    ("school", ("학교", "기관", "school")),
)
TEMPLATE_STATUS_LABELS = {
    "APPROVED": "승인됨", "PENDING": "검수 대기", "INSPECTING": "검수 중", "REJECTED": "반려", "STOP": "사용 중지",
}
_TEMPLATE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _fernet() -> Fernet:
    digest = hashlib.sha256(
        f"saedam-solapi-settings:{load_credential_secret()}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    text = str(value or "").strip()
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii") if text else ""


def _decrypt(token: object) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    try:
        return _fernet().decrypt(text.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "저장된 SOLAPI 자격증명을 복호화할 수 없습니다. 통합관리에서 다시 저장해 주세요."
        ) from exc


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _load_store(conn) -> dict[str, Any]:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT value, updated_at FROM admin_settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data["updated_at"] = str(row["updated_at"] or data.get("updated_at") or "")
    return data


def normalize_phone(value: object, *, required: bool = False) -> str:
    """대한민국 휴대폰번호를 SOLAPI 전송용 숫자 형식으로 정규화한다."""
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("휴대폰번호를 입력해 주세요.")
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    # 엑셀에서 휴대폰번호를 숫자로 저장하면 맨 앞의 0이 사라질 수 있다.
    if re.fullmatch(r"1[016789]\d{7,8}", digits):
        digits = "0" + digits
    if not re.fullmatch(r"01[016789]\d{7,8}", digits):
        raise ValueError("휴대폰번호는 010-1234-5678 형식으로 입력해 주세요.")
    return digits


def format_phone(value: object) -> str:
    digits = normalize_phone(value)
    if not digits:
        return ""
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"


def mask_phone(value: object) -> str:
    formatted = format_phone(value)
    if not formatted:
        return "-"
    parts = formatted.split("-")
    return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}"


def _environment_settings() -> dict[str, str]:
    return {
        "api_key": str(os.environ.get("SOLAPI_API_KEY", "")).strip(),
        "api_secret": str(os.environ.get("SOLAPI_API_SECRET", "")).strip(),
        "pf_id": str(os.environ.get("SOLAPI_PF_ID", "")).strip(),
        "template_id": str(os.environ.get("SOLAPI_TEMPLATE_ID", "")).strip(),
        "from_number": re.sub(r"\D", "", str(os.environ.get("SOLAPI_FROM", ""))),
        "public_origin": str(os.environ.get("PUBLIC_ORIGIN", "")).strip().rstrip("/"),
        "sender_name": str(os.environ.get("SOLAPI_SENDER_NAME", "")).strip(),
        "survey_pf_id": str(os.environ.get("SOLAPI_SURVEY_PF_ID", "")).strip(),
        "survey_template_id": str(os.environ.get("SOLAPI_SURVEY_TEMPLATE_ID", "")).strip(),
        "parent_pf_id": str(os.environ.get("SOLAPI_PARENT_PF_ID", "")).strip(),
        "parent_template_id": str(os.environ.get("SOLAPI_PARENT_TEMPLATE_ID", "")).strip(),
    }


def normalize_origin(value: object) -> str:
    """문자에 담을 공개 링크의 기준 주소를 검증한다."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc             or parsed.path or parsed.query or parsed.fragment             or re.search(r"\s", text):
        raise ValueError("공개 링크 주소는 https://example.com 형식으로 입력해 주세요.")
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_variable_name(value: object, *, label: str = "변수 이름") -> str:
    """#{학교명}·학교명 어느 쪽으로 적어도 이름만 남긴다. 비워 두면 ''(기본 이름). 공백과 # { } 는 쓸 수 없다."""
    text = str(value or "").strip()
    wrapped = re.fullmatch(r"#\{(.*)\}", text)
    if wrapped:
        text = wrapped.group(1).strip()
    if not text:
        return ""
    if len(text) > 40 or re.search(r"[\s{}#]", text):
        raise ValueError(f"{label} 값을 확인해 주세요. 공백과 # {{ }} 는 쓸 수 없고 40자까지 입력할 수 있습니다.")
    return text


def effective_link_variable_names(names: object = None) -> dict[str, str]:
    """저장된(또는 비어 있는) 변수 이름에 기본 이름을 채워 {'school': …, 'url': …}를 돌려준다."""
    given = names if isinstance(names, dict) else {}
    return {
        source: str(given.get(source) or "").strip() or default
        for source, default in LINK_VARIABLE_DEFAULTS.items()
    }


def link_variable_source(name: object, names: object = None) -> str | None:
    """템플릿 변수 하나가 'school'(학교 이름)인지 'url'(안내 주소)인지 정한다. 모르면 None.

    직접 정한 변수 이름이 먼저이고, 그다음 이름 속 단어로 짐작한다(school_name → 학교 이름).
    """
    chosen = effective_link_variable_names(names)
    key = re.sub(r"\s", "", str(name or "")).lower()
    for source, wanted in chosen.items():
        if key == re.sub(r"\s", "", wanted).lower():
            return source
    for source, words in _LINK_VARIABLE_RULES:
        if any(word in key for word in words):
            return source
    return None


def match_link_variables(variables: list[dict[str, Any]], names: object = None) -> dict[str, Any]:
    """템플릿 변수를 학교 이름·안내 주소에 연결해 본다(설정 화면의 [승인 상태 확인]에서 보여 준다).

    반환: {'mapped': [{'key','source','label'}], 'unmapped': [채울 수 없는 변수 key], 'absent': [템플릿에 없는 역할 이름]}
    """
    mapped: list[dict[str, str]] = []
    unmapped: list[str] = []
    for item in variables or []:
        source = link_variable_source(item.get("name"), names)
        if source:
            mapped.append({"key": item["key"], "source": source, "label": LINK_VARIABLE_LABELS[source]})
        else:
            unmapped.append(item["key"])
    present = {item["source"] for item in mapped}
    absent = [LINK_VARIABLE_LABELS[source] for source in LINK_VARIABLE_DEFAULTS if source not in present]
    return {"mapped": mapped, "unmapped": unmapped, "absent": absent}


def _normalize_extra_templates(value: object, *, default_channel: str = "") -> list[dict[str, str]]:
    """저장값에서 [추가한 템플릿] 목록을 안전하게 꺼낸다(예전·깨진 값은 건너뛴다)."""
    rows: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        template_id = str(item.get("template_id") or "").strip()
        if not name or not template_id:
            continue
        pf_id = str(item.get("pf_id") or "").strip()
        rows.append(
            {
                "name": name,
                "pf_id": pf_id,
                "channel_id": pf_id or default_channel,
                "template_id": template_id,
                "memo": str(item.get("memo") or "").strip(),
            }
        )
    return rows


def clean_extra_templates(rows: object) -> list[dict[str, str]]:
    """화면에서 받은 [추가한 템플릿] 줄을 검사한다. 모두 빈 줄은 건너뛰고, 잘못된 줄이 있으면 ValueError."""
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, (list, tuple)) else []:
        row = row if isinstance(row, dict) else {}
        name = str(row.get("name") or "").strip()
        pf_id = str(row.get("pf_id") or "").strip()
        template_id = str(row.get("template_id") or "").strip()
        memo = str(row.get("memo") or "").strip()
        if not (name or pf_id or template_id or memo):
            continue
        if not name:
            raise ValueError("추가한 템플릿의 이름(용도)을 입력해 주세요.")
        if len(name) > 30:
            raise ValueError(f"템플릿 이름은 30자까지 입력할 수 있습니다: {name[:12]}…")
        if not template_id:
            raise ValueError(f"[{name}] 템플릿 ID를 입력해 주세요.")
        for label, text in (("채널 ID", pf_id), ("템플릿 ID", template_id)):
            if len(text) > 120 or re.search(r"\s", text):
                raise ValueError(f"[{name}] {label} 값을 확인해 주세요.")
        key = re.sub(r"\s+", "", name).lower()
        if key in seen:
            raise ValueError(f"같은 이름의 템플릿이 두 번 들어 있습니다: {name}")
        seen.add(key)
        cleaned.append({"name": name, "pf_id": pf_id, "template_id": template_id, "memo": memo[:100]})
    if len(cleaned) > MAX_EXTRA_TEMPLATES:
        raise ValueError(f"추가한 템플릿은 최대 {MAX_EXTRA_TEMPLATES}개까지 저장할 수 있습니다.")
    return cleaned


def get_template_ref(name: object, settings: dict[str, Any] | None = None) -> dict[str, str] | None:
    """[추가한 템플릿]을 이름(공백·대소문자 무시)으로 찾아 {'name','channel_id','template_id',…}를 돌려준다. 없으면 None.

    새 기능이 알림톡을 보낼 때 send_bulk_alimtalk(pf_id=ref['channel_id'], template_id=ref['template_id'])로 쓴다.
    """
    active = settings or get_settings()
    wanted = re.sub(r"\s+", "", str(name or "")).lower()
    for item in active.get("extra_templates") or []:
        if re.sub(r"\s+", "", item["name"]).lower() == wanted:
            return item
    return None


def get_settings(conn=None) -> dict[str, Any]:
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
    finally:
        if owns_connection:
            conn.close()

    environment = _environment_settings()
    api_key = _decrypt(store.get("api_key_encrypted")) if store.get("api_key_encrypted") else ""
    api_secret = (
        _decrypt(store.get("api_secret_encrypted"))
        if store.get("api_secret_encrypted")
        else ""
    )
    result = {
        "api_key": api_key or environment["api_key"],
        "api_secret": api_secret or environment["api_secret"],
        "pf_id": str(store.get("pf_id") or environment["pf_id"]).strip(),
        "template_id": str(store.get("template_id") or environment["template_id"]).strip(),
        "from_number": re.sub(
            r"\D", "", str(store.get("from_number") or environment["from_number"])
        ),
        "public_origin": str(
            store.get("public_origin")
            or environment["public_origin"]
            or CONTRACT_PUBLIC_ORIGIN
        ).rstrip("/"),
        "sender_name": str(
            store.get("sender_name") or environment["sender_name"] or DEFAULT_SENDER_NAME
        ),
        "survey_pf_id": str(
            store.get("survey_pf_id") or environment["survey_pf_id"] or SURVEY_KAKAO_PF_ID
        ).strip(),
        "survey_template_id": str(
            store.get("survey_template_id")
            or environment["survey_template_id"]
            or SURVEY_KAKAO_TEMPLATE_ID
        ).strip(),
        # 학부모 안내 링크 알림톡. 저장값 → 환경변수 → 코드 기본값 순서로 쓴다.
        "parent_pf_id": str(
            store.get("parent_pf_id") or environment["parent_pf_id"] or PARENT_KAKAO_PF_ID
        ).strip(),
        "parent_template_id": str(
            store.get("parent_template_id")
            or environment["parent_template_id"]
            or PARENT_KAKAO_TEMPLATE_ID
        ).strip(),
        "parent_variable_names": effective_link_variable_names(
            {"school": store.get("parent_var_school"), "url": store.get("parent_var_url")}
        ),
        "updated_by": str(store.get("updated_by") or ""),
        "updated_at": str(store.get("updated_at") or ""),
    }
    result["extra_templates"] = _normalize_extra_templates(
        store.get("extra_templates"), default_channel=result["survey_pf_id"]
    )
    configured_from_db = any(
        store.get(key)
        for key in (
            "api_key_encrypted",
            "api_secret_encrypted",
            "pf_id",
            "template_id",
            "from_number",
        )
    )
    configured_from_env = any(
        value for key, value in environment.items()
        if key not in {"survey_pf_id", "survey_template_id", "parent_pf_id", "parent_template_id"}
    )
    result["source"] = (
        "database" if configured_from_db else "environment" if configured_from_env else "none"
    )
    result["configured"] = all(
        result.get(key)
        for key in ("api_key", "api_secret", "pf_id", "template_id", "from_number")
    )
    # 설문조사 URL 문자는 알림톡 템플릿 없이 API 키·시크릿·발신번호만으로 보낸다.
    result["sms_configured"] = all(
        result.get(key) for key in ("api_key", "api_secret", "from_number")
    )
    # 설문조사 알림톡은 문자 설정에 설문 전용 채널 ID·템플릿 ID가 더해져야 한다.
    result["survey_kakao_configured"] = bool(
        result["sms_configured"] and result["survey_pf_id"] and result["survey_template_id"]
    )
    # 학부모 안내 링크 알림톡도 문자 설정에 템플릿 ID가 더해져야 한다(채널은 설문조사 것으로 대신할 수 있다).
    result["parent_channel_id"] = result["parent_pf_id"] or result["survey_pf_id"]
    result["parent_kakao_configured"] = bool(
        result["sms_configured"] and result["parent_channel_id"] and result["parent_template_id"]
    )
    return result


def _template_slots_for_view(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """화면에 그릴 알림톡 템플릿 칸(기능별)과 설정 여부."""
    slots: list[dict[str, Any]] = []
    for slot in KAKAO_TEMPLATE_SLOTS:
        pf_id = str(settings.get(slot["pf_field"]) or "")
        template_id = str(settings.get(slot["template_field"]) or "")
        # 인증전자계약은 채널·템플릿이 모두 있어야 하고, 나머지는 채널을 설문조사 것으로 대신할 수 있다.
        channel = pf_id if slot["key"] == "contract" else (pf_id or str(settings.get("survey_pf_id") or ""))
        names = settings.get(slot.get("variable_names_key", "")) or {}
        inputs = [
            {**field, "default": LINK_VARIABLE_DEFAULTS[field["source"]], "value": names.get(field["source"], "")}
            for field in slot.get("variable_fields", ())
        ]
        slots.append({**slot, "pf_value": pf_id, "template_value": template_id,
                      "configured": bool(template_id and channel), "variable_inputs": inputs})
    return slots


def settings_for_view() -> dict[str, Any]:
    settings = get_settings()
    api_key = str(settings.get("api_key") or "")
    return {
        "template_slots": _template_slots_for_view(settings),
        "extra_templates": settings.get("extra_templates", []),
        "max_extra_templates": MAX_EXTRA_TEMPLATES,
        "api_key_masked": f"{'*' * max(4, len(api_key) - 4)}{api_key[-4:]}" if api_key else "",
        "has_api_key": bool(api_key),
        "has_api_secret": bool(settings.get("api_secret")),
        "pf_id": settings.get("pf_id", ""),
        "template_id": settings.get("template_id", ""),
        "from_number": settings.get("from_number", ""),
        "public_origin": settings.get("public_origin", ""),
        "sender_name": settings.get("sender_name", ""),
        "updated_by": settings.get("updated_by", ""),
        "updated_at": settings.get("updated_at", ""),
        "source": settings.get("source", "none"),
        "configured": bool(settings.get("configured")),
        "sms_configured": bool(settings.get("sms_configured")),
        "survey_pf_id": settings.get("survey_pf_id", ""),
        "survey_template_id": settings.get("survey_template_id", ""),
        "survey_kakao_configured": bool(settings.get("survey_kakao_configured")),
        "parent_pf_id": settings.get("parent_pf_id", ""),
        "parent_template_id": settings.get("parent_template_id", ""),
        "parent_kakao_configured": bool(settings.get("parent_kakao_configured")),
    }


def save_settings(
    *,
    api_key: object,
    api_secret: object,
    pf_id: object,
    template_id: object,
    from_number: object,
    actor: object,
    public_origin: object = None,
    sender_name: object = None,
    survey_pf_id: object = None,
    survey_template_id: object = None,
    parent_pf_id: object = None,
    parent_template_id: object = None,
    extra_templates: object = None,
    parent_var_school: object = None,
    parent_var_url: object = None,
    clear_credentials: bool = False,
) -> None:
    api_key_text = str(api_key or "").strip()
    api_secret_text = str(api_secret or "").strip()
    pf_id_text = str(pf_id or "").strip()
    template_id_text = str(template_id or "").strip()
    from_digits = re.sub(r"\D", "", str(from_number or ""))
    origin_text = normalize_origin(public_origin) if public_origin is not None else None
    sender_text = (
        str(sender_name).strip()[:30] if sender_name is not None else None
    )
    survey_pf_text = str(survey_pf_id).strip() if survey_pf_id is not None else None
    survey_template_text = (
        str(survey_template_id).strip() if survey_template_id is not None else None
    )
    parent_pf_text = str(parent_pf_id).strip() if parent_pf_id is not None else None
    parent_template_text = (
        str(parent_template_id).strip() if parent_template_id is not None else None
    )
    # None이면 저장된 추가 템플릿을 그대로 두고, 목록(빈 목록 포함)이면 그 목록으로 바꾼다.
    extra_rows = clean_extra_templates(extra_templates) if extra_templates is not None else None
    # 변수 이름: None이면 그대로 두고, 비우면 기본 이름(학교명·url)으로 돌아간다.
    var_school_text = (
        normalize_variable_name(parent_var_school, label="학교 이름 변수") if parent_var_school is not None else None
    )
    var_url_text = (
        normalize_variable_name(parent_var_url, label="안내 주소 변수") if parent_var_url is not None else None
    )

    # 알림톡 항목은 문자 전용으로만 쓰는 설치본을 위해 선택 입력으로 둔다.
    for label, value, maximum in (
        ("SOLAPI PF ID", pf_id_text, 120),
        ("SOLAPI 템플릿 ID", template_id_text, 120),
        ("설문조사 알림톡 채널 ID", survey_pf_text or "", 120),
        ("설문조사 알림톡 템플릿 ID", survey_template_text or "", 120),
        ("학부모 안내 알림톡 채널 ID", parent_pf_text or "", 120),
        ("학부모 안내 알림톡 템플릿 ID", parent_template_text or "", 120),
    ):
        if value and (len(value) > maximum or re.search(r"\s", value)):
            raise ValueError(f"{label} 값을 확인해 주세요.")
    if not re.fullmatch(r"\d{8,12}", from_digits):
        raise ValueError("회사 발신번호는 지역번호를 포함한 숫자 8~12자리로 입력해 주세요.")
    if api_key_text and (len(api_key_text) > 500 or re.search(r"\s", api_key_text)):
        raise ValueError("SOLAPI API KEY 형식을 확인해 주세요.")
    if api_secret_text and (len(api_secret_text) > 500 or re.search(r"\s", api_secret_text)):
        raise ValueError("SOLAPI API SECRET 형식을 확인해 주세요.")

    conn = get_db()
    try:
        store = _load_store(conn)
        environment = _environment_settings()
        stored_api_key = (
            _decrypt(store.get("api_key_encrypted"))
            if store.get("api_key_encrypted") and not clear_credentials
            else ""
        )
        stored_api_secret = (
            _decrypt(store.get("api_secret_encrypted"))
            if store.get("api_secret_encrypted") and not clear_credentials
            else ""
        )
        if not (api_key_text or stored_api_key or environment["api_key"]):
            raise ValueError("SOLAPI API KEY를 입력해 주세요.")
        if not (api_secret_text or stored_api_secret or environment["api_secret"]):
            raise ValueError("SOLAPI API SECRET을 입력해 주세요.")
        if clear_credentials:
            store["api_key_encrypted"] = ""
            store["api_secret_encrypted"] = ""
        if api_key_text:
            store["api_key_encrypted"] = _encrypt(api_key_text)
        if api_secret_text:
            store["api_secret_encrypted"] = _encrypt(api_secret_text)
        store.update(
            {
                "pf_id": pf_id_text,
                "template_id": template_id_text,
                "from_number": from_digits,
                "updated_by": str(actor or "admin")[:100],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        if origin_text is not None:
            store["public_origin"] = origin_text
        if sender_text is not None:
            store["sender_name"] = sender_text
        # 비워 두면 저장값을 지워 코드에 들어 있는 기본 채널·템플릿으로 돌아간다.
        if survey_pf_text is not None:
            store["survey_pf_id"] = survey_pf_text
        if survey_template_text is not None:
            store["survey_template_id"] = survey_template_text
            _TEMPLATE_CACHE.clear()
        if parent_pf_text is not None:
            store["parent_pf_id"] = parent_pf_text
        if parent_template_text is not None:
            store["parent_template_id"] = parent_template_text
            _TEMPLATE_CACHE.clear()
        if extra_rows is not None:
            store["extra_templates"] = extra_rows
            _TEMPLATE_CACHE.clear()
        if var_school_text is not None:
            store["parent_var_school"] = var_school_text
        if var_url_text is not None:
            store["parent_var_url"] = var_url_text
        chosen = effective_link_variable_names(
            {"school": store.get("parent_var_school"), "url": store.get("parent_var_url")}
        )
        if chosen["school"].lower() == chosen["url"].lower():
            raise ValueError("학교 이름 변수와 안내 주소 변수는 서로 다른 이름이어야 합니다.")
        conn.execute(
            """
            INSERT INTO admin_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (SETTINGS_KEY, json.dumps(store, ensure_ascii=False)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def csrf_token(session_store) -> str:
    token = session_store.get("solapi_settings_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session_store["solapi_settings_csrf"] = token
    return str(token)


def valid_csrf(session_store, supplied: object) -> bool:
    expected = str(session_store.get("solapi_settings_csrf") or "")
    received = str(supplied or "")
    return bool(expected and received and hmac.compare_digest(expected, received))


def _require_complete(settings: dict[str, Any], *, kakao: bool) -> None:
    required = ["api_key", "api_secret", "from_number"]
    if kakao:
        required.extend(["pf_id", "template_id"])
    if not all(settings.get(key) for key in required):
        destination = "알림톡" if kakao else "문자"
        raise RuntimeError(f"SOLAPI {destination} 발송 설정이 완료되지 않았습니다.")


def _authorization(settings: dict[str, Any]) -> str:
    date_text = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    salt = secrets.token_hex(16)
    signature = hmac.new(
        str(settings["api_secret"]).encode("utf-8"),
        f"{date_text}{salt}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"HMAC-SHA256 apiKey={settings['api_key']}, date={date_text}, "
        f"salt={salt}, signature={signature}"
    )


def _send_message(message: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "messages": [message],
            "strict": True,
            "allowDuplicates": True,
            "showMessageList": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request_object = Request(
        SOLAPI_SEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": _authorization(settings),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Saedam-Intranet/1.0",
        },
    )
    try:
        with urlopen(request_object, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            failures = detail.get("failedMessageList") or []
            first = failures[0] if failures else {}
            reason = first.get("statusMessage") or detail.get("errorMessage") or detail.get("message") or "요청 거절"
            if first.get("statusCode"):
                reason = f"[{first['statusCode']}] {reason}"
        except Exception:
            reason = str(exc)
        raise RuntimeError(f"SOLAPI 요청 실패: {reason}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"SOLAPI 서버 연결 실패: {exc}") from exc

    return _parse_send_result(result)


def _parse_send_result(result: object) -> dict[str, str]:
    """send-many/detail 접수 결과를 판별한다. 접수 성공은 수신 완료와 다르다."""
    if not isinstance(result, dict):
        raise RuntimeError("SOLAPI 응답 형식을 확인할 수 없습니다.")
    group = result.get("groupInfo") or {}
    counts = group.get("count") or {}
    failures = result.get("failedMessageList") or []
    messages = result.get("messageList") or []
    first = messages[0] if isinstance(messages, list) and messages else {}
    failed = failures[0] if isinstance(failures, list) and failures else {}
    code = str(first.get("statusCode") or "")
    if failures or counts.get("registeredFailed") or counts.get("sentFailed") or group.get("status") == "FAILED" or result.get("errorCode") or (code and code not in {"2000", "4000"}):
        item = failed or first
        reason = item.get("statusMessage") or item.get("reason") or result.get("errorMessage") or "발송 접수 실패"
        error_code = str(item.get("statusCode") or result.get("errorCode") or "")
        raise RuntimeError(f"SOLAPI 발송 실패 [{error_code or 'UNKNOWN'}]: {reason}")
    # showMessageList=True이므로 성공 메시지 ID가 없으면 성공으로 기록하지 않는다.
    if not first.get("messageId") or code not in {"2000", "4000"}:
        raise RuntimeError("SOLAPI 발송 접수를 확인할 수 없습니다. 솔라피 발송 내역을 확인해 주세요.")
    return {
        "message_id": str(first["messageId"]),
        "group_id": str(group.get("groupId") or ""),
        "status_code": code,
    }


def send_alimtalk(
    to: object,
    *,
    signer_name: object,
    invitation_url: object,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = settings or get_settings()
    _require_complete(active, kakao=True)
    phone = normalize_phone(to, required=True)
    name = str(signer_name or "").strip()
    if not name:
        raise ValueError("알림톡에 사용할 계약자 이름이 없습니다.")
    link = str(invitation_url or "").strip()
    parsed = urlsplit(link)
    if (parsed.scheme != "https" or parsed.netloc != "works.saedam.org"
            or not parsed.path.startswith("/verified-contract/sign/")
            or not parsed.path.removeprefix("/verified-contract/sign/")
            or re.search(r"\s", link) or parsed.query or parsed.fragment):
        raise ValueError("계약 링크는 https://works.saedam.org의 인증전자계약 주소여야 합니다.")
    # 승인 버튼 주소는 https://#{url}. 변수에는 스킴을 제외해야 한다.
    # 본문·강조문구·버튼(targetOut 포함)은 SOLAPI의 승인 템플릿을 그대로 사용한다.
    # 대체발송 문구는 템플릿에 저장된 값(제목이 빈 문자열)을 쓰면 안 된다. 계약 링크가 붙어
    # 90바이트를 넘는 순간 LMS로 전환되는데, 제목이 비어 있으면 1010(필수 입력 값 미입력)으로
    # 접수 자체가 거절된다. 발송할 때마다 제목이 있는 대체문자를 직접 실어 보낸다.
    return _send_message(
        {
            "to": phone,
            "from": active["from_number"],
            "type": "ATA",
            "kakaoOptions": {
                "pfId": active["pf_id"],
                "templateId": active["template_id"],
                "disableSms": False,
                "variables": {
                    "#{이름}": name,
                    "#{url}": link.removeprefix("https://"),
                },
            },
            "replacements": [
                {
                    "from": active["from_number"],
                    "subject": ALIMTALK_FALLBACK_SUBJECT,
                    "text": (
                        f"[새담 인증전자계약] {name}님, 전자계약서가 도착했습니다.\n"
                        f"아래 주소에서 계약을 진행해 주세요.\n{link}"
                    ),
                }
            ],
        },
        active,
    )


def send_sms_otp(
    to: object,
    *,
    signer_name: object,
    code: object,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = settings or get_settings()
    _require_complete(active, kakao=False)
    phone = normalize_phone(to, required=True)
    return _send_message(
        {
            "to": phone,
            "from": active["from_number"],
            "type": "SMS",
            "text": (
                f"[새담 인증전자계약] {str(signer_name or '계약자').strip()}님 "
                f"인증번호는 {str(code)}입니다. 5분 안에 입력해 주세요."
            ),
            "autoTypeDetect": False,
        },
        active,
    )


def resolve_public_origin(settings: dict[str, Any] | None = None) -> str:
    """설문 링크 등 외부 공개 주소의 기준이 되는 origin을 반환한다."""
    active = settings or get_settings()
    return str(active.get("public_origin") or CONTRACT_PUBLIC_ORIGIN).rstrip("/")


def message_byte_length(text: object) -> int:
    """SOLAPI가 SMS/LMS를 나누는 기준인 EUC-KR 바이트 길이를 계산한다."""
    body = str(text or "")
    try:
        return len(body.encode("euc-kr"))
    except UnicodeEncodeError:
        # EUC-KR로 표현할 수 없는 글자(이모지 등)가 있으면 LMS로 보내야 한다.
        return SMS_BYTE_LIMIT + 1


def _send_many(messages: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    """여러 건을 한 번에 접수한다. strict=False라 일부 실패해도 나머지는 접수된다."""
    payload = json.dumps(
        {
            "messages": messages,
            "strict": False,
            "allowDuplicates": True,
            "showMessageList": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request_object = Request(
        SOLAPI_SEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": _authorization(settings),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Saedam-Intranet/1.0",
        },
    )
    try:
        with urlopen(request_object, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            reason = (
                detail.get("errorMessage") or detail.get("message") or "요청 거절"
            )
        except Exception:
            reason = str(exc)
        raise RuntimeError(f"SOLAPI 요청 실패: {reason}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"SOLAPI 서버 연결 실패: {exc}") from exc


def _entry_phone(item: object) -> str:
    """응답 항목에서 수신번호를 뽑는다. 문자열(접수번호)만 오는 응답도 있다."""
    if isinstance(item, dict):
        return re.sub(r"\D", "", str(item.get("to") or ""))
    return ""


def _entry_message_id(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("messageId") or "")
    return ""


def _entry_failure_reason(item: object) -> str:
    if not isinstance(item, dict):
        return "발송 접수 실패"
    code = str(item.get("statusCode") or "")
    reason = (
        item.get("statusMessage")
        or item.get("reason")
        or item.get("errorMessage")
        or "발송 접수 실패"
    )
    return f"[{code or 'UNKNOWN'}] {reason}"


def _apply_bulk_result(chunk: list[dict[str, Any]], result: object) -> None:
    """send-many 응답을 수신자 행에 반영한다.

    strict=False로 접수하면 SOLAPI는 거절한 건만 failedMessageList에 담아 돌려준다.
    따라서 이 목록에 없는 번호는 접수된 것으로 판정해야 한다. messageList는 접수번호를
    붙이는 용도로만 쓰며, 항목이 객체든 접수번호 문자열이든, to가 있든 없든 견딘다.
    (예전 구현은 messageList의 to로만 성공을 확인해서, to를 돌려주지 않는 응답에서는
     실제로 발송된 문자까지 전부 실패로 기록했다.)
    """
    payload = result if isinstance(result, dict) else {}
    group = payload.get("groupInfo") or {}
    if payload.get("errorCode") or str(group.get("status") or "") == "FAILED":
        reason = str(
            payload.get("errorMessage") or payload.get("message") or "그룹 발송 실패"
        )
        code = str(payload.get("errorCode") or "GROUP_FAILED")
        for row in chunk:
            row["error"] = f"[{code}] {reason}"
        return

    failed_by_phone: dict[str, object] = {}
    floating_failures: list[object] = []
    for item in payload.get("failedMessageList") or []:
        phone = _entry_phone(item)
        if phone:
            failed_by_phone[phone] = item
        else:
            floating_failures.append(item)

    sent_by_phone: dict[str, object] = {}
    floating_sent: list[object] = []
    for item in payload.get("messageList") or []:
        code = str(item.get("statusCode") or "") if isinstance(item, dict) else ""
        phone = _entry_phone(item)
        if code and code not in {"2000", "4000"}:
            if phone:
                failed_by_phone.setdefault(phone, item)
            else:
                floating_failures.append(item)
            continue
        if phone:
            sent_by_phone[phone] = item
        else:
            floating_sent.append(item)

    unresolved: list[dict[str, Any]] = []
    for row in chunk:
        phone = row["phone"]
        if phone in failed_by_phone:
            row["error"] = _entry_failure_reason(failed_by_phone[phone])
        elif phone in sent_by_phone:
            row["ok"] = True
            row["message_id"] = _entry_message_id(sent_by_phone[phone])
        else:
            unresolved.append(row)

    # 번호를 알 수 없는 실패 건수가 남은 행 수와 정확히 맞을 때만 실패로 돌린다.
    if floating_failures and len(floating_failures) == len(unresolved):
        for row, item in zip(unresolved, floating_failures):
            row["error"] = _entry_failure_reason(item)
        return
    for row in unresolved:
        row["ok"] = True
        row["message_id"] = _entry_message_id(floating_sent.pop(0)) if floating_sent else ""


def send_bulk_text(
    recipients: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """문자(SMS/LMS)를 여러 명에게 보내고 수신자별 접수 결과를 그대로 돌려준다.

    recipients 각 항목은 {'to': 휴대폰번호, 'text': 본문, 'subject': 제목(선택)} 형태이며,
    반환값은 입력 순서를 유지한 {'to', 'ok', 'message_id', 'error'} 목록이다.
    """
    active = settings or get_settings()
    _require_complete(active, kakao=False)

    prepared: list[dict[str, Any]] = []
    for entry in recipients:
        row: dict[str, Any] = {
            "to": str((entry or {}).get("to") or ""),
            "ok": False,
            "message_id": "",
            "error": "",
            "phone": "",
        }
        try:
            row["phone"] = normalize_phone(row["to"], required=True)
        except ValueError as exc:
            row["error"] = str(exc)
            prepared.append(row)
            continue
        body = str((entry or {}).get("text") or "").strip()
        if not body:
            row["error"] = "발송할 문자 내용이 없습니다."
            prepared.append(row)
            continue
        subject = str((entry or {}).get("subject") or "").strip()
        if message_byte_length(body) > SMS_BYTE_LIMIT:
            row["payload"] = {
                "to": row["phone"],
                "from": active["from_number"],
                "type": "LMS",
                "subject": (subject or DEFAULT_SENDER_NAME)[:40],
                "text": body,
                "autoTypeDetect": False,
            }
        else:
            row["payload"] = {
                "to": row["phone"],
                "from": active["from_number"],
                "type": "SMS",
                "text": body,
                "autoTypeDetect": False,
            }
        prepared.append(row)

    return _dispatch_prepared(prepared, active)


def _dispatch_prepared(
    prepared: list[dict[str, Any]], active: dict[str, Any]
) -> list[dict[str, Any]]:
    """payload가 준비된 행을 BULK_CHUNK_SIZE씩 접수하고 결과를 행에 기록한다."""
    sendable = [row for row in prepared if row.get("payload")]
    for start in range(0, len(sendable), BULK_CHUNK_SIZE):
        chunk = sendable[start:start + BULK_CHUNK_SIZE]
        try:
            result = _send_many([row["payload"] for row in chunk], active)
        except RuntimeError as exc:
            for row in chunk:
                row["error"] = str(exc)
            continue
        _apply_bulk_result(chunk, result)

    for row in prepared:
        row.pop("payload", None)
    return prepared


# ---------------------------------------------------------------------------
# 카카오 알림톡 템플릿 조회 · 대량 발송 (설문조사)
# ---------------------------------------------------------------------------
_VARIABLE_PATTERN = re.compile(r"#\{([^{}#]{1,40})\}")
# 변수가 들어갈 수 있는 템플릿 항목. comments(검수 의견) 등은 제외한다.
_TEMPLATE_TEXT_FIELDS = (
    "content", "emphasizeTitle", "emphasizeSubtitle", "header", "extra", "ad",
    "buttons", "quickReplies", "highlight", "item",
)


def _collect_strings(value: object, sink: list[str]) -> None:
    if isinstance(value, str):
        sink.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, sink)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, sink)


def template_variables(template: dict[str, Any]) -> list[dict[str, Any]]:
    """템플릿에서 #{변수} 목록을 등장 순서대로 뽑는다.

    버튼 주소가 https://#{url} 처럼 스킴 뒤에 변수를 두는 경우 그 변수에는
    https:// 를 뺀 주소를 넣어야 하므로 strip_scheme 표시를 함께 돌려준다.
    """
    texts: list[str] = []
    for field in _TEMPLATE_TEXT_FIELDS:
        _collect_strings(template.get(field), texts)
    for item in template.get("variables") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            texts.append(str(name))
    joined = "\n".join(texts)

    ordered: list[str] = []
    for match in _VARIABLE_PATTERN.finditer(joined):
        name = match.group(1).strip()
        if name and name not in ordered:
            ordered.append(name)
    return [
        {
            "name": name,
            "key": f"#{{{name}}}",
            "strip_scheme": bool(
                re.search(r"https?://#\{" + re.escape(name) + r"\}", joined)
            ),
        }
        for name in ordered
    ]


def _get_json(url: str, settings: dict[str, Any]) -> dict[str, Any]:
    request_object = Request(
        url,
        method="GET",
        headers={
            "Authorization": _authorization(settings),
            "User-Agent": "Saedam-Intranet/1.0",
        },
    )
    try:
        with urlopen(request_object, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            reason = detail.get("errorMessage") or detail.get("message") or "요청 거절"
            code = detail.get("errorCode") or exc.code
            reason = f"[{code}] {reason}"
        except Exception:
            reason = str(exc)
        raise RuntimeError(f"SOLAPI 템플릿 조회 실패: {reason}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"SOLAPI 서버 연결 실패: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("SOLAPI 템플릿 응답 형식을 확인할 수 없습니다.")
    return data


def fetch_kakao_template(
    template_id: object = None,
    *,
    settings: dict[str, Any] | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """솔라피에 등록된 알림톡 템플릿(본문·버튼·변수)을 조회한다."""
    active = settings or get_settings()
    if not all(active.get(key) for key in ("api_key", "api_secret")):
        raise RuntimeError("SOLAPI API KEY·SECRET을 먼저 저장해 주세요.")
    target = str(template_id or active.get("survey_template_id") or "").strip()
    if not target or re.search(r"[^A-Za-z0-9_-]", target):
        raise RuntimeError("알림톡 템플릿 ID를 확인해 주세요.")

    cached = _TEMPLATE_CACHE.get(target)
    if cached and not refresh and time.time() - cached[0] < TEMPLATE_CACHE_SECONDS:
        return cached[1]

    data = _get_json(SOLAPI_TEMPLATE_URL.format(template_id=target), active)
    template = {
        "template_id": str(data.get("templateId") or target),
        "name": str(data.get("name") or ""),
        "status": str(data.get("status") or ""),
        "channel_id": str(data.get("channelId") or ""),
        "content": str(data.get("content") or ""),
        "emphasize_type": str(data.get("emphasizeType") or "NONE"),
        "emphasize_title": str(data.get("emphasizeTitle") or ""),
        "emphasize_subtitle": str(data.get("emphasizeSubtitle") or ""),
        "header": str(data.get("header") or ""),
        "extra": str(data.get("extra") or ""),
        "buttons": [
            {
                "type": str(item.get("buttonType") or ""),
                "name": str(item.get("buttonName") or ""),
                "link_mo": str(item.get("linkMo") or ""),
                "link_pc": str(item.get("linkPc") or ""),
            }
            for item in data.get("buttons") or []
            if isinstance(item, dict)
        ],
        "variables": template_variables(data),
    }
    _TEMPLATE_CACHE[target] = (time.time(), template)
    return template


def fill_template_text(text: object, variables: dict[str, str]) -> str:
    """미리보기용으로 템플릿 문구의 #{변수}를 실제 값으로 바꾼다."""
    result = str(text or "")
    for key, value in variables.items():
        result = result.replace(key, str(value))
    return result


def kakao_template_problem(template: dict[str, Any]) -> str:
    """승인되지 않았거나 내용을 못 불러온 템플릿이면 사유를, 문제없으면 빈 문자열을 돌려준다."""
    status = str(template.get("status") or "").upper()
    if status and status != "APPROVED":
        labels = {"PENDING": "검수 대기", "INSPECTING": "검수 중", "REJECTED": "반려"}
        return (
            f"알림톡 템플릿이 아직 승인되지 않았습니다(상태: {labels.get(status, status)}). "
            "솔라피에서 승인된 뒤 발송하거나 문자로 보내 주세요."
        )
    if not template.get("variables") and not template.get("content"):
        return "알림톡 템플릿 내용을 불러오지 못했습니다. 템플릿 ID를 확인해 주세요."
    return ""


def kakao_template_preview(template: dict[str, Any], values: dict[str, str]) -> str:
    """수신자 화면에 보일 알림톡 모습(강조·본문·부가정보·버튼)을 글로 만든다."""
    parts: list[str] = []
    if template.get("emphasize_title"):
        title = fill_template_text(template["emphasize_title"], values)
        sub = fill_template_text(template.get("emphasize_subtitle"), values)
        parts.append(f"【{title}】" + (f"\n{sub}" if sub else ""))
    if template.get("header"):
        parts.append(fill_template_text(template["header"], values))
    parts.append(fill_template_text(template.get("content"), values))
    if template.get("extra"):
        parts.append(fill_template_text(template["extra"], values))
    for button in template.get("buttons") or []:
        link = button.get("link_mo") or button.get("link_pc")
        label = f"[버튼] {button.get('name') or button.get('type')}"
        if link:
            label += f" → {fill_template_text(link, values)}"
        parts.append(label)
    return "\n\n".join(part for part in parts if part)


def send_bulk_alimtalk(
    recipients: list[dict[str, Any]],
    *,
    pf_id: object = None,
    template_id: object = None,
    fallback: bool = True,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """카카오 알림톡을 여러 명에게 보내고 수신자별 접수 결과를 돌려준다.

    recipients 각 항목은 {'to', 'variables': {'#{변수}': 값},
    'fallback_text', 'fallback_subject'} 형태다. fallback=True이면 알림톡이
    실패한 수신자에게 fallback_text를 문자(SMS/LMS)로 대신 보낸다.
    반환값은 send_bulk_text와 같은 {'to', 'ok', 'message_id', 'error'} 목록이다.
    """
    active = settings or get_settings()
    _require_complete(active, kakao=False)
    channel = str(pf_id or active.get("survey_pf_id") or "").strip()
    template = str(template_id or active.get("survey_template_id") or "").strip()
    if not channel or not template:
        raise RuntimeError("알림톡 채널 ID·템플릿 ID가 설정되지 않았습니다.")

    prepared: list[dict[str, Any]] = []
    for entry in recipients:
        entry = entry or {}
        row: dict[str, Any] = {
            "to": str(entry.get("to") or ""),
            "ok": False,
            "message_id": "",
            "error": "",
            "phone": "",
        }
        try:
            row["phone"] = normalize_phone(row["to"], required=True)
        except ValueError as exc:
            row["error"] = str(exc)
            prepared.append(row)
            continue
        variables = {
            str(key): str(value if value is not None else "")
            for key, value in (entry.get("variables") or {}).items()
        }
        payload: dict[str, Any] = {
            "to": row["phone"],
            "from": active["from_number"],
            "type": "ATA",
            "kakaoOptions": {
                "pfId": channel,
                "templateId": template,
                "disableSms": not fallback,
                "variables": variables,
            },
        }
        fallback_text = str(entry.get("fallback_text") or "").strip()
        if fallback and fallback_text:
            # 템플릿에 저장된 대체문구는 제목이 비어 LMS 전환 시 1010으로 거절될 수 있어
            # (전자계약 알림톡과 같은 이유) 제목이 있는 대체문자를 직접 실어 보낸다.
            replacement: dict[str, Any] = {
                "from": active["from_number"],
                "text": fallback_text,
            }
            if message_byte_length(fallback_text) > SMS_BYTE_LIMIT:
                replacement["subject"] = (
                    str(entry.get("fallback_subject") or "").strip() or DEFAULT_SENDER_NAME
                )[:40]
            payload["replacements"] = [replacement]
        row["payload"] = payload
        prepared.append(row)

    return _dispatch_prepared(prepared, active)
