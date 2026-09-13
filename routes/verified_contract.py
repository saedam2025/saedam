"""기존 전자계약과 완전히 분리된 인증전자계약 시스템."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import threading
import zipfile
import tempfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape
from pathlib import Path
from urllib.request import Request, urlopen

import pdfkit
from PyPDF2 import PdfReader, PdfWriter
import pandas as pd
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from cryptography.fernet import Fernet, InvalidToken
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .ai_mail import _csrf_token as _shared_sender_csrf_token
from .ai_mail import _owner_emp_no
from .database import get_db
from .payroll import (
    _ensure_sender_schema,
    _payroll_sender_dict,
    _sender_from_header,
    _smtp_login_for_sender,
    _verify_smtp_sender,
)
from .security import load_credential_secret, menu_permission_required
from .storage import (
    APP_ROOT,
    COMPANY_STAMP_ROOT,
    DATA_ROOT,
    TERMS_ROOT,
    VERIFIED_CONTRACT_ROOT,
    VERIFIED_CONTRACTS_ROOT,
    VERIFIED_LOGO_ROOT,
    VERIFIED_PDF_FONT_ROOT,
    VERIFIED_SIGNATURE_ROOT,
    VERIFIED_STAMP_ROOT,
    VERIFIED_TERMS_ROOT,
)
from .verified_contract_repository import (
    add_verified_contract_event,
    insert_verified_contract,
    update_verified_contract,
)
from .solapi_settings import (
    CONTRACT_PUBLIC_ORIGIN,
    format_phone,
    get_settings as get_solapi_settings,
    mask_phone,
    normalize_phone,
    send_alimtalk,
)
from .secure_files import (
    delete_file,
    encrypted_response,
    encrypted_storage_name,
    encrypt_bytes,
    encrypt_stream,
    encrypt_upload,
    original_filename,
    read_decrypted,
    stored_plain_size,
    temporary_decrypted_path,
)


verified_contract_bp = Blueprint("verified_contract", __name__)
_settings_file_lock = threading.RLock()

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
DEFAULT_CATEGORIES = [
    "방과후강사",
    "맞춤형강사",
    "코디사업자",
    "코디근로자",
    "안전코디",
    "직원근로자",
    "직원사업자",
    "원어민근로자",
    "원어민사업자",
]
MAX_COMPANY_PROFILES = 20
# 이메일 인증번호 유효시간. 화면 안내·메일 본문·만료 판정이 모두 이 값을 따른다.
OTP_VALID_MINUTES = 3
OTP_VALID_SECONDS = OTP_VALID_MINUTES * 60
# 인증번호 재발송 대기시간. 주소를 잘못 입력했을 때 너무 오래 묶이지 않도록 짧게 둔다.
OTP_RESEND_COOLDOWN_SECONDS = 60
# 계약서 양식 편집기에서 선택 삽입할 수 있는 치환 변수 목록 (샘플엑셀 열 + 회사정보)
TEMPLATE_VARIABLES = (
    "계약구분",
    "성명",
    "email",
    "연락처",
    "수탁학교명",
    "부서명",
    "계약기간",
    "근무시간",
    "수수료",
    "보조금",
    "경력수당",
    "직책수당",
    "기타",
    "비고1",
    "비고2",
    "비고3",
    "비고4",
    "거주지",
    "주민번호",
    "은행",
    "계좌번호",
    "회사명",
    "대표직함",
    "대표자명",
    "계약서제목",
)
MONEY_FIELDS = ("수수료", "보조금", "경력수당", "직책수당", "기타")
CONTRACT_FIELDS = (
    "계약구분",
    "수탁학교명",
    "부서명",
    "성명",
    "수수료",
    "보조금",
    "경력수당",
    "직책수당",
    "기타",
    "근무시간",
    "계약기간",
    "비고1",
    "비고2",
    "비고3",
    "비고4",
    "email",
    "연락처",
    "거주지",
    "주민번호",
    "은행",
    "계좌번호",
    "회사명",
    "대표직함",
    "대표자명",
    "계약서제목",
)
AGREEMENTS = (
    {
        "key": "contract_content",
        "title": "계약 내용 및 전자서명 동의",
        "text": (
            "본인은 계약서 전체 내용을 확인하였으며 전자문서 및 전자서명 방식으로 "
            "계약을 체결하는 데 동의합니다. 본인이 입력한 성명, 직접 작성한 서명과 "
            "계약완료 버튼 클릭을 본인의 계약 체결 의사표시로 사용하는 데 동의합니다."
        ),
    },
    {
        "key": "identity",
        "title": "본인 확인",
        "text": "본인은 계약서에 표시 및 개인정보를 입력한 계약 당사자 본인이며 타인에게 인증번호와 계약 링크를 제공하지 않았습니다.",
    },
    {
        "key": "privacy",
        "title": "개인정보 수집·이용 안내 확인",
        "text": (
            "계약 체결·이행·보관 및 대금 지급을 위해 성명, 이메일, 연락처, 주소, 주민등록번호, "
            "은행명, 계좌번호, 접속기록, 서명정보를 "
            "수집·이용하며 관계 법령과 내부 보존기준에 따른 기간 동안 보관하는 안내를 확인했습니다."
        ),
    },
)

DEFAULTS_ROOT = APP_ROOT / "verified_contract_defaults"
# 구 전자계약 메뉴가 제거된 뒤에도 신규 배포가 기본 계약양식을 확보할 수 있게 한다.
BUNDLED_TERMS_ROOT = APP_ROOT / "terms"
VERIFIED_CATEGORIES_FILE = VERIFIED_CONTRACT_ROOT / "categories.json"
VERIFIED_TITLES_FILE = VERIFIED_CONTRACT_ROOT / "contract_titles.json"
VERIFIED_COMPANY_FILE = VERIFIED_CONTRACT_ROOT / "company_settings.json"
VERIFIED_MAIL_FILE = VERIFIED_CONTRACT_ROOT / "mail_settings.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _format_kst(value: object) -> str:
    parsed = _parse_iso(value)
    if not parsed:
        text = str(value or "")
        try:
            parsed = datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=UTC)
        except ValueError:
            return text
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _sensitive_cipher() -> Fernet:
    """서버에 자동 보관되는 자격증명 키로 계약 민감정보를 암호화한다."""
    secret = load_credential_secret()
    digest = hashlib.sha256(
        f"verified-contract-sensitive-data:{secret}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_sensitive(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _sensitive_cipher().encrypt(text.encode("utf-8")).decode("ascii")


def _decrypt_sensitive(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    try:
        return _sensitive_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):
        return "[암호화 자료 확인 필요]"


def _normalize_rrn(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 13 or digits[6] not in "12345678":
        raise ValueError("주민등록번호를 13자리로 정확히 입력해 주세요.")
    try:
        datetime.strptime(digits[:6], "%y%m%d")
    except ValueError as exc:
        raise ValueError("주민등록번호 앞자리의 생년월일을 확인해 주세요.") from exc
    return f"{digits[:6]}-{digits[6:]}"


def _normalize_account(value: object) -> str:
    account = re.sub(r"\s+", "", str(value or "").strip())
    if not re.fullmatch(r"[0-9-]{5,80}", account) or not re.search(r"\d", account):
        raise ValueError("계좌번호는 숫자와 하이픈(-)으로 정확히 입력해 주세요.")
    return account


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr or "")[:100]


def _user_agent() -> str:
    return str(request.headers.get("User-Agent", ""))[:500]


def _json_file(path: Path, default):
    backup = path.with_suffix(path.suffix + ".bak")
    with _settings_file_lock:
        for candidate in (path, backup):
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError, TypeError):
                continue
        return default


def _save_json(path: Path, value) -> None:
    """설정 파일을 중간 손상 없이 교체하고 직전 정상본도 보존한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".bak")
    backup_temporary = path.with_suffix(path.suffix + ".bak.tmp")
    with _settings_file_lock:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_file():
            shutil.copy2(path, backup_temporary)
            os.replace(backup_temporary, backup)
        os.replace(temporary, path)


def _copy_if_missing(source: Path, target: Path) -> bool:
    if target.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _bootstrap_verified_storage() -> None:
    """기존 값을 최초 한 번만 복제하고 이후에는 인증계약 전용 파일만 사용한다."""
    for directory in (
        VERIFIED_CONTRACT_ROOT,
        VERIFIED_CONTRACTS_ROOT,
        VERIFIED_TERMS_ROOT,
        VERIFIED_STAMP_ROOT,
        VERIFIED_LOGO_ROOT,
        VERIFIED_SIGNATURE_ROOT,
        VERIFIED_PDF_FONT_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    default_terms = DEFAULTS_ROOT / "terms"
    for category in DEFAULT_CATEGORIES:
        for suffix in ("", "2"):
            filename = f"{category}{suffix}.txt"
            target = VERIFIED_TERMS_ROOT / filename
            if target.exists():
                continue
            # Render에 기존 관리자가 수정한 양식이 있으면 그 상태를 최초 복제한다.
            source = TERMS_ROOT / filename
            if not source.is_file():
                source = default_terms / filename
            if not source.is_file():
                source = BUNDLED_TERMS_ROOT / filename
            _copy_if_missing(source, target)

    if not VERIFIED_CATEGORIES_FILE.exists():
        legacy = DATA_ROOT / "categories.json"
        categories = _json_file(legacy, DEFAULT_CATEGORIES)
        _save_json(
            VERIFIED_CATEGORIES_FILE,
            categories if isinstance(categories, list) and categories else DEFAULT_CATEGORIES,
        )

    if not VERIFIED_TITLES_FILE.exists():
        legacy = DATA_ROOT / "contract_titles.json"
        titles = _json_file(legacy, {})
        _save_json(VERIFIED_TITLES_FILE, titles if isinstance(titles, dict) else {})

    if not VERIFIED_COMPANY_FILE.exists():
        legacy = DATA_ROOT / "company_settings.json"
        settings = _json_file(legacy, {})
        if not settings:
            settings = {
                "active_profile_id": "verified-default",
                "profiles": [
                    {
                        "id": "verified-default",
                        "label": "기본 회사",
                        "company_name": "(사)새담청소년교육문화원",
                        "representative_title": "이사장",
                        "representative_name": "",
                        "stamp_filename": "verified_default_stamp.png",
                    }
                ],
            }
        _save_json(VERIFIED_COMPANY_FILE, settings)

        for profile in settings.get("profiles", []):
            filename = os.path.basename(str(profile.get("stamp_filename", "")))
            if not filename:
                continue
            source = COMPANY_STAMP_ROOT / filename
            if source.is_file():
                _copy_if_missing(source, VERIFIED_STAMP_ROOT / filename)

    default_stamp = VERIFIED_STAMP_ROOT / "verified_default_stamp.png"
    _copy_if_missing(DEFAULTS_ROOT / "stamp.png", default_stamp)

    for font_name in ("NanumGothic-Regular.ttf", "NanumGothic-Bold.ttf"):
        font_target = VERIFIED_PDF_FONT_ROOT / font_name
        if font_target.exists():
            continue
        font_source = DATA_ROOT / "pdf_fonts" / font_name
        if not font_source.is_file():
            font_source = DEFAULTS_ROOT / "pdf_fonts" / font_name
        _copy_if_missing(font_source, font_target)

    if not VERIFIED_MAIL_FILE.exists():
        legacy_file = APP_ROOT / "mail_settings.json"
        settings = _json_file(legacy_file, {})
        username = str(settings.get("MAIL_USERNAME") or os.environ.get("MAIL_USERNAME") or "").strip()
        password = str(settings.get("MAIL_PASSWORD") or os.environ.get("MAIL_PASSWORD") or "").strip()
        account_id = "verified-mail-default"
        _save_json(
            VERIFIED_MAIL_FILE,
            {
                "active_account_id": account_id if username else "",
                "accounts": (
                    [
                        {
                            "id": account_id,
                            "label": "기본 발송계정",
                            "email": username,
                            "encrypted_password": _encrypt_sensitive(password),
                        }
                    ]
                    if username
                    else []
                ),
            },
        )


_bootstrap_verified_storage()


@verified_contract_bp.after_request
def _verified_contract_security_headers(response):
    if request.path.startswith("/verified-contract/sign/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _categories() -> list[str]:
    value = _json_file(VERIFIED_CATEGORIES_FILE, DEFAULT_CATEGORIES)
    if not isinstance(value, list):
        return DEFAULT_CATEGORIES.copy()
    result = []
    for item in value:
        name = str(item).strip()
        if (
            name
            and len(name) <= 40
            and not any(character in name for character in '\\/:*?"<>|')
        ):
            result.append(name)
    return result or DEFAULT_CATEGORIES.copy()


def _titles() -> dict[str, str]:
    value = _json_file(VERIFIED_TITLES_FILE, {})
    return value if isinstance(value, dict) else {}


def _normalize_companies(value) -> dict:
    default_profile = {
        "id": "verified-default",
        "label": "기본 회사",
        "company_name": "(사)새담청소년교육문화원",
        "representative_title": "이사장",
        "representative_name": "",
        "business_number": "",
        "address": "",
        "phone": "",
        "logo_filename": "",
        "logo_original_name": "",
        "stamp_filename": "verified_default_stamp.png",
        "stamp_original_name": "기본 도장.png",
    }
    profiles = []
    if isinstance(value, dict):
        for index, item in enumerate(value.get("profiles", [])):
            if not isinstance(item, dict):
                continue
            profile = default_profile.copy()
            profile.update(item)
            profile["id"] = str(profile.get("id") or f"verified-{index + 1}")
            profiles.append(profile)
    if not profiles:
        profiles = [default_profile]
    active_id = str((value or {}).get("active_profile_id", "")) if isinstance(value, dict) else ""
    if not any(profile["id"] == active_id for profile in profiles):
        active_id = profiles[0]["id"]
    return {
        "active_profile_id": active_id,
        "profiles": profiles[:MAX_COMPANY_PROFILES],
    }


def _company_settings() -> dict:
    stored = _json_file(VERIFIED_COMPANY_FILE, {})
    normalized = _normalize_companies(stored)
    if stored != normalized:
        _save_json(VERIFIED_COMPANY_FILE, normalized)
    return normalized


def _company_profile(profile_id: str | None = None) -> dict:
    settings = _company_settings()
    target_id = profile_id or settings["active_profile_id"]
    return next(
        (profile for profile in settings["profiles"] if profile["id"] == target_id),
        settings["profiles"][0],
    )


def _company_snapshot(profile_id: str | None = None) -> dict:
    profile = _company_profile(profile_id)
    return {
        "profile_id": profile["id"],
        "label": str(profile.get("label", "")),
        "company_name": str(profile.get("company_name", "")).strip(),
        "representative_title": str(profile.get("representative_title", "")).strip(),
        "representative_name": str(profile.get("representative_name", "")).strip(),
        "business_number": str(profile.get("business_number", "")).strip(),
        "address": str(profile.get("address", "")).strip(),
        "phone": str(profile.get("phone", "")).strip(),
        "logo_filename": os.path.basename(str(profile.get("logo_filename", ""))),
        "logo_original_name": str(profile.get("logo_original_name", "")).strip(),
        "stamp_filename": os.path.basename(str(profile.get("stamp_filename", ""))),
        "stamp_original_name": str(profile.get("stamp_original_name", "")).strip(),
    }


def _default_title(contract_type: str, company_name: str) -> str:
    mapping = {
        "방과후강사": f"{company_name} 위탁교육계약서",
        "맞춤형강사": f"{company_name} 위탁교육계약서",
        "코디근로자": f"{company_name} 센터장 계약서",
        "코디사업자": f"{company_name} 센터장 계약서",
        "원어민근로자": "방과후 영어 원어민 강사 위탁 계약서",
        "원어민사업자": "방과후 영어 원어민 강사 위탁 계약서",
        "안전코디": f"{company_name} 위수탁계약서",
        "직원근로자": f"{company_name} 근로계약서",
        "직원사업자": f"{company_name} 위탁업무계약서",
    }
    return mapping.get(contract_type, f"{company_name} 계약서 ({contract_type})")


def _contract_title(contract_type: str, company_name: str) -> str:
    saved = str(_titles().get(contract_type, "")).strip()
    if not saved:
        return _default_title(contract_type, company_name)
    for marker in (
        "{{ data.회사명 }}",
        "{{data.회사명}}",
        "{{ company.name }}",
        "{{company.name}}",
    ):
        saved = saved.replace(marker, company_name)
    return saved


def _read_terms(contract_type: str) -> tuple[str, str]:
    def read(suffix: str) -> str:
        path = VERIFIED_TERMS_ROOT / f"{contract_type}{suffix}.txt"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    return read(""), read("2")


def _format_value(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        number = float(raw.replace(",", ""))
        if 0 < number < 1:
            return f"{int(number * 100)}%"
        if number >= 100:
            return f"{int(number):,}"
    except ValueError:
        pass
    return raw


def _empty_value(value: object) -> bool:
    cleaned = str(value or "").strip().lower().replace(",", "").replace("원", "")
    if cleaned in {"", "none", "nan", "null", "-", "0", "0.0", "0.00"}:
        return True
    try:
        return float(cleaned) == 0
    except ValueError:
        return False


def _render_terms(raw_html: str, values: dict[str, str]) -> str:
    result = _sanitize_contract_html(raw_html)
    row_pattern = re.compile(r"<tr\b[^>]*>.*?</tr>", flags=re.IGNORECASE | re.DOTALL)
    for field in MONEY_FIELDS:
        if _empty_value(values.get(field)):
            marker = re.compile(
                r"\{\{\s*(?:data|style)\." + re.escape(field) + r"\s*\}\}",
                flags=re.IGNORECASE,
            )
            result = row_pattern.sub(
                lambda match: "" if marker.search(match.group(0)) else match.group(0),
                result,
            )
    result = re.sub(
        r"\sdata-show-if\s*=\s*([\"']).*?\1",
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for field in CONTRACT_FIELDS:
        value = _format_value(values.get(field)) if field in MONEY_FIELDS else str(values.get(field, ""))
        result = result.replace(f"{{{{ data.{field} }}}}", value)
        result = result.replace(
            f"{{{{ style.{field} }}}}",
            "display:none" if _empty_value(values.get(field)) else "display:table-row",
        )
    return result


def _sanitize_contract_html(raw_html: object) -> str:
    """양식 편집에 불필요한 실행 코드와 외부 프레임을 제거한다."""
    result = str(raw_html or "")
    result = re.sub(
        r"<\s*(script|iframe|object|embed|form|meta)\b[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = re.sub(
        r"<\s*(script|iframe|object|embed|form|meta)\b[^>]*/?\s*>",
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = re.sub(
        r"\s+on[a-z]+\s*=\s*([\"']).*?\1",
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    result = re.sub(
        r"\s+(href|src)\s*=\s*([\"'])\s*javascript:.*?\2",
        "",
        result,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return result


def _active_sender_id() -> str:
    """인증전자계약 발송에 사용할 공유 발송계정(ai_mail_senders) id를 반환한다."""
    value = _json_file(VERIFIED_MAIL_FILE, {})
    return str((value or {}).get("active_sender_id", "")).strip()


def _set_active_sender_id(sender_id: str) -> None:
    _save_json(VERIFIED_MAIL_FILE, {"active_sender_id": str(sender_id)})


def _mail_senders_for_view(conn) -> list[dict]:
    """스마트명세서·인감증명서와 동일한 공유 발송계정 목록을 그대로 사용한다."""
    _ensure_sender_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM ai_mail_senders
        WHERE owner_emp_no=? AND is_active=1
        ORDER BY updated_at DESC, id DESC
        """,
        (_owner_emp_no(),),
    ).fetchall()
    return [_payroll_sender_dict(row) for row in rows]


def _active_sender_row(conn):
    """설정에서 지정한 발송계정을 가져온다.

    인증번호·계약완료 메일은 관리자 세션이 없는 계약자(외부인)가 여는 화면에서도
    발송되므로, 이미 골라 둔 sender_id는 owner_emp_no(로그인 세션)와 무관하게
    조회해야 한다. owner_emp_no로 제한하면 계약자 쪽 요청에서는 세션이 비어 있어
    항상 계정을 못 찾거나 엉뚱한 계정으로 새어나간다.
    """
    _ensure_sender_schema(conn)
    sender_id = _active_sender_id()
    row = None
    if sender_id:
        row = conn.execute(
            "SELECT * FROM ai_mail_senders WHERE id=? AND is_active=1",
            (sender_id,),
        ).fetchone()
    if not row:
        owner = _owner_emp_no()
        if owner:
            row = conn.execute(
                """
                SELECT * FROM ai_mail_senders
                WHERE owner_emp_no=? AND is_active=1
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (owner,),
            ).fetchone()
    return row


def _send_mail(to, subject: str, contents, attachments=None) -> None:
    """스마트명세서·인감증명서와 동일한 공유 발송계정(Gmail/ZeptoMail)으로 발송한다."""
    conn = get_db()
    try:
        row = _active_sender_row(conn)
    finally:
        conn.close()
    if not row:
        raise RuntimeError(
            "인증전자계약 발송계정이 설정되지 않았습니다. 양식관리 > 발송메일계정에서 계정을 등록·선택해 주세요."
        )
    # _payroll_sender_dict()는 화면 표시용으로 암호화된 비밀번호를 일부러 빼고 돌려주므로
    # 실제 SMTP 로그인에는 쓰면 안 된다(비밀번호가 없어 매번 복호화 실패로 이어진다).
    sender = dict(row)
    message = MIMEMultipart()
    message["From"] = _sender_from_header(sender)
    message["To"] = to if isinstance(to, str) else ", ".join(to)
    message["Subject"] = subject
    message.attach(MIMEText(contents, "html", "utf-8"))
    if attachments:
        paths = attachments if isinstance(attachments, (list, tuple)) else [attachments]
        for item in paths:
            item_path = Path(item)
            part = MIMEApplication(item_path.read_bytes(), _subtype="pdf")
            part.add_header(
                "Content-Disposition", "attachment", filename=item_path.name
            )
            message.attach(part)
    smtp = _smtp_login_for_sender(sender)
    try:
        _verify_smtp_sender(smtp, sender)
        smtp.send_message(
            message, from_addr=str(sender.get("email") or "").strip().lower()
        )
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()


def _csrf_token() -> str:
    token = session.get("verified_contract_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["verified_contract_csrf"] = token
    return token


def _csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("verified_contract_csrf", "")
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            return jsonify({"status": "error", "message": "보안 확인값이 만료되었습니다. 화면을 새로고침해 주세요."}), 403
        return view(*args, **kwargs)

    return wrapped


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _external_url(token: str) -> str:
    # 운영 프록시의 내부 Host/HTTP 값이 계약자에게 전달되지 않게 한다.
    return CONTRACT_PUBLIC_ORIGIN + url_for(
        "verified_contract.public_contract",
        token=token,
        _external=False,
    )


def _load_by_token(conn, token: str):
    return conn.execute(
        "SELECT * FROM verified_contracts WHERE invitation_token_hash=?",
        (_token_hash(token),),
    ).fetchone()


def _contract_available(row) -> tuple[bool, str]:
    if not row:
        return False, "유효하지 않은 계약 링크입니다."
    if row["status"] == "revoked":
        return False, "관리자가 취소한 계약 링크입니다."
    if row["status"] == "expired":
        return False, "유효기간이 만료된 계약 링크입니다."
    expiry = _parse_iso(row["invitation_expires_at"])
    if row["status"] == "pending" and expiry and expiry < _now():
        return False, "유효기간이 만료된 계약 링크입니다."
    return True, ""


def _public_verified(row, token: str) -> bool:
    access = session.get("verified_contract_access", {})
    return (
        isinstance(access, dict)
        and access.get(str(row["id"])) == _token_hash(token)[:24]
        and bool(row["verified_at"])
    )


def _mask_email(email: str) -> str:
    local, separator, domain = str(email).partition("@")
    if not separator:
        return "-"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(2, len(local) - len(visible))}@{domain}"


def _normalized_email(value: object) -> str:
    """공개 계약 화면에서 입력받은 이메일을 정규화한다. 형식이 틀리면 빈 문자열."""
    email = str(value or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return ""
    return email


def _delivery_recipient(row) -> tuple[str, str, str]:
    """전화번호가 있으면 알림톡/SMS, 없으면 이메일을 선택한다."""
    phone = normalize_phone(row["signer_phone"])
    if phone:
        return "alimtalk", phone, mask_phone(phone)
    email = str(row["signer_email"] or "").strip().lower()
    if email:
        return "email", email, _mask_email(email)
    raise RuntimeError("계약대상자의 휴대폰번호 또는 이메일주소가 없습니다.")


def _send_invitation(row, invitation_url: str) -> tuple[str, str, str]:
    channel, recipient, _masked = _delivery_recipient(row)
    if channel == "alimtalk":
        result = send_alimtalk(
            recipient,
            signer_name=row["signer_name"],
            invitation_url=invitation_url,
        )
        return channel, recipient, str(result.get("message_id") or "")
    _invitation_mail(row, invitation_url)
    return channel, recipient, ""


def _row_for_view(row) -> dict:
    item = dict(row)
    try:
        contract_data = json.loads(item.get("contract_data_json") or "{}")
    except (TypeError, ValueError):
        contract_data = {}
    for field in (
        "수수료",
        "보조금",
        "경력수당",
        "직책수당",
        "기타",
        "근무시간",
        "계약기간",
        "비고1",
        "비고2",
        "비고3",
        "비고4",
    ):
        item[field] = contract_data.get(field, "")
    item["주민번호"] = _decrypt_sensitive(item.pop("signer_rrn_encrypted", ""))
    item["은행"] = _decrypt_sensitive(item.pop("signer_bank_encrypted", ""))
    item["계좌번호"] = _decrypt_sensitive(item.pop("signer_account_encrypted", ""))
    date_source = item.get("signed_at") or item.get("created_at") or ""
    item["연도"] = str(date_source)[:4]
    item["created_display"] = _format_kst(item.get("created_at"))
    item["expires_display"] = _format_kst(item.get("invitation_expires_at"))
    item["signed_display"] = _format_kst(item.get("signed_at"))
    status_map = {
        "draft": "등록대기",
        "pending": "서명대기",
        "completed": "계약완료",
        "revoked": "취소",
        "expired": "기간만료",
        "voided": "폐기",
        "superseded": "변경계약",
    }
    item["status_label"] = status_map.get(item.get("status"), item.get("status"))
    try:
        item["signer_phone_display"] = format_phone(item.get("signer_phone"))
    except ValueError:
        item["signer_phone_display"] = str(item.get("signer_phone") or "")
    channel_labels = {"alimtalk": "카카오 알림톡", "email": "이메일"}
    item["invite_channel_label"] = channel_labels.get(
        item.get("invite_channel"), item.get("invite_channel") or "미발송"
    )
    # 발송 상태를 화면에 그대로 노출하지 않고 우리말로 풀어서 보여준다.
    mail_status_labels = {
        "sent": "발송 됨",
        "not_sent": "발송 전",
        "sending": "발송 중",
        "waiting": "발송 대기",
        "failed": "발송 실패",
    }
    mail_status = str(item.get("invite_mail_status") or "")
    item["invite_mail_status_label"] = mail_status_labels.get(
        mail_status, mail_status or "발송 전"
    )
    # 발송 수단을 모르는 예전 기록은 상태만 보여준다.
    item["invite_delivery_label"] = (
        f"{item['invite_channel_label']} {item['invite_mail_status_label']}"
        if item.get("invite_channel")
        else item["invite_mail_status_label"]
    )
    item["invite_sent_display"] = (
        _format_kst(item["invitation_sent_at"]) if item.get("invitation_sent_at") else ""
    )
    if item["invite_sent_display"]:
        item["invite_delivery_label"] += f" · {item['invite_sent_display']}"
    return item


def _record_event(conn, row_or_id, event_type: str, details=None) -> None:
    contract_id = int(row_or_id["id"] if hasattr(row_or_id, "keys") else row_or_id)
    add_verified_contract_event(
        conn,
        contract_id,
        event_type,
        _iso(),
        ip_address=_client_ip(),
        user_agent=_user_agent(),
        details=details,
    )


def _invitation_mail(row, invitation_url: str) -> None:
    _send_mail(
        row["signer_email"],
        f"[전자계약 요청] {row['title_snapshot']}",
        _invitation_html(row, invitation_url),
    )


def _invitation_html(row, invitation_url: str) -> str:
    return f"""
    <div style="font-family:Arial,'Malgun Gothic',sans-serif;line-height:1.7;color:#1f2937">
      <h2 style="color:#123b6d">새담 인증전자계약 요청</h2>
      <p><b>{escape(row['signer_name'])}</b>님, {escape(row['title_snapshot'])} 확인과 서명을 요청드립니다.</p>
      <p>아래 버튼을 누른 후 본인 인증번호를 확인하면 계약서를 작성할 수 있습니다.</p>
      <p style="margin:28px 0">
        <a href="{escape(invitation_url)}" style="background:#123b6d;color:#fff;padding:13px 24px;border-radius:7px;text-decoration:none;font-weight:bold">계약서 확인하기</a>
      </p>
      <p style="font-size:13px;color:#64748b">유효기간: {escape(_format_kst(row['invitation_expires_at']))}<br>
      본인이 요청한 계약이 아니라면 링크를 열지 말고 새담 담당자에게 알려주세요.</p>
    </div>
    """


def _void_notice_html(row, reason_label: str, has_attachment: bool = False) -> str:
    """폐기·변경계약 안내메일. 계약완료 안내메일과 같은 남색 문서 톤으로 맞춘다."""
    name = escape(row["signer_name"])
    title = escape(row["title_snapshot"])
    label = escape(reason_label)
    company = json.loads(row["company_snapshot_json"] or "{}") if "company_snapshot_json" in row.keys() else {}
    org_name = escape(str(company.get("company_name") or "(사)새담청소년교육문화원"))
    logo_url = f"{CONTRACT_PUBLIC_ORIGIN}/static/logo01.gif"
    attachment_line = (
        "<br>· 첨부된 계약서 파일은 <b>효력을 잃은 계약서</b>이며, 어떤 계약이 처리되었는지"
        " 확인하실 수 있도록 참고용으로만 함께 보내 드립니다."
        if has_attachment else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:26px 12px 44px;background:#eef1f5;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       style="width:100%;max-width:640px;border:1px solid #ccd6e2;border-radius:6px;
              background:#ffffff;">

<tr><td style="height:5px;line-height:5px;font-size:0;background:#a33330;
               border-radius:5px 5px 0 0;">&nbsp;</td></tr>

<tr><td style="padding:30px 32px 0;color:#33404f;font-size:14px;line-height:1.85;">
    안녕하세요, {org_name}입니다.<br>
    <b style="color:#16243a;">{name}</b> 님, 아래 계약 건이 <b style="color:#a33330;">{label}</b> 처리되었음을 알려 드립니다.
</td></tr>

<tr><td style="padding:26px 32px 0;">
    <div style="color:#5f7799;font-size:11px;font-weight:700;letter-spacing:.16em;">CONTRACT</div>
    <h2 style="margin:6px 0 12px;color:#16243a;font-size:17px;font-weight:700;">{label} 처리된 계약</h2>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border:1px solid #d7e0ea;border-left:3px solid #a33330;background:#f6f8fb;">
        <tr><td style="padding:13px 16px;color:#16243a;font-size:14px;font-weight:700;">
            {title}
            <div style="margin-top:6px;color:#5f6f85;font-size:12.5px;font-weight:500;">
                계약구분 {escape(row['contract_type'])} · 수탁학교 {escape(row['school_name'] or '-')}
                · 부서 {escape(row['department'] or '-')}
            </div>
        </td></tr>
    </table>
</td></tr>

<tr><td style="padding:26px 32px 0;">
    <div style="color:#5f7799;font-size:11px;font-weight:700;letter-spacing:.16em;">NOTICE</div>
    <h2 style="margin:6px 0 12px;color:#16243a;font-size:17px;font-weight:700;">안내 사항</h2>
    <div style="padding:15px 17px;border:1px solid #d7e0ea;background:#f6f8fb;
                color:#46586e;font-size:12.5px;line-height:1.85;">
        · <b style="color:#a33330;">본 안내 이후로 위 계약서는 더 이상 유효하지 않으며 법적 효력이 없습니다.</b><br>
        · 이전에 받으신 계약 링크로는 계약서를 여실 수 없습니다.<br>
        · 동일한 내용으로 다시 계약이 필요한 경우, 새담 담당자로부터 별도의 계약 요청 메일을 받으시게 됩니다.{attachment_line}
    </div>
</td></tr>

<tr><td style="padding:28px 32px 30px;text-align:center;">
    <div style="color:#33404f;font-size:14px;line-height:1.8;">
        문의사항은 새담 계약 담당자에게 연락해 주세요.</div>
</td></tr>

<tr><td style="padding:18px 20px 22px;border-top:1px solid #d7e0ea;text-align:center;
               background:#f6f8fb;border-radius:0 0 5px 5px;">
    <img src="{logo_url}" alt="{org_name}"
         style="display:block;margin:0 auto;height:34px;width:auto;">
    <div style="margin-top:9px;color:#7b8aa0;font-size:11px;line-height:1.7;">
        {org_name}<br>
        본 메일은 새담 인트라넷 인증전자계약에서 발송되었습니다.
    </div>
</td></tr>

</table></td></tr></table>
</body></html>"""


def _completion_mail_html(row, pdf_hash: str, company_name: str = "") -> str:
    """계약완료 안내메일. 합격 안내메일(금색 한지 결)과 구분되도록 남색 문서 톤으로 꾸민다."""
    name = escape(row["signer_name"])
    title = escape(row["title_snapshot"])
    org_name = escape(str(company_name or "(사)새담청소년교육문화원"))
    logo_url = f"{CONTRACT_PUBLIC_ORIGIN}/static/logo01.gif"
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:26px 12px 44px;background:#eef1f5;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
       style="width:100%;max-width:640px;border:1px solid #ccd6e2;border-radius:6px;
              background:#ffffff;">

<tr><td style="height:5px;line-height:5px;font-size:0;background:#1f3a5f;
               border-radius:5px 5px 0 0;">&nbsp;</td></tr>

<tr><td style="padding:30px 32px 0;color:#33404f;font-size:14px;line-height:1.85;">
    안녕하세요, {org_name}입니다.<br>
    <b style="color:#16243a;">{name}</b> 님, 계약서 확인과 전자서명까지 모두 마쳐주셔서 감사합니다.<br>
    첨부해 드린 최종 계약서(PDF)는 아래 안내를 참고하시어 안전하게 보관해 주시기 바랍니다.
</td></tr>

<tr><td style="padding:26px 32px 0;">
    <div style="color:#5f7799;font-size:11px;font-weight:700;letter-spacing:.16em;">CONTRACT</div>
    <h2 style="margin:6px 0 12px;color:#16243a;font-size:17px;font-weight:700;">체결 계약서</h2>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border:1px solid #d7e0ea;border-left:3px solid #1f3a5f;background:#f6f8fb;">
        <tr><td style="padding:13px 16px;color:#16243a;font-size:14px;font-weight:700;">
            {title}</td></tr>
    </table>
</td></tr>

<tr><td style="padding:26px 32px 0;">
    <div style="color:#5f7799;font-size:11px;font-weight:700;letter-spacing:.16em;">KEEP SAFE</div>
    <h2 style="margin:6px 0 12px;color:#16243a;font-size:17px;font-weight:700;">계약서 보관 안내</h2>
    <div style="padding:15px 17px;border:1px solid #d7e0ea;background:#f6f8fb;
                color:#46586e;font-size:12px;line-height:1.85;">
        · 첨부된 계약서 PDF는 다운로드 후 별도 폴더에 안전하게 보관해 주세요. 보관의 책임은 계약자에게 있습니다.<br>
        · 계약서 원문과 서명 이미지에는 개인정보가 담겨 있으니, 계약 당사자 외의 사람에게 전달하거나 공개된 장소에 올리지 말아 주세요.<br>
        · 컴퓨터가 아닌 클라우드(이메일, 개인 드라이브 등)에도 사본을 하나 더 남겨 두시면 분실을 예방할 수 있습니다.
    </div>
</td></tr>

<tr><td style="padding:26px 32px 0;">
    <div style="color:#5f7799;font-size:11px;font-weight:700;letter-spacing:.16em;">VERIFICATION</div>
    <h2 style="margin:6px 0 12px;color:#16243a;font-size:17px;font-weight:700;">위변조 확인용 고유번호</h2>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
           style="border:1px solid #d7e0ea;border-left:3px solid #1f3a5f;background:#f6f8fb;">
        <tr><td style="padding:14px 16px;">
            <div style="color:#5f7799;font-size:12px;font-weight:700;margin-bottom:6px;">SHA-256</div>
            <div style="color:#1f3a5f;font-size:13px;font-weight:700;font-family:'Consolas',monospace;
                        word-break:break-all;line-height:1.6;">{escape(pdf_hash)}</div>
        </td></tr>
    </table>
    <div style="margin-top:8px;color:#6b7889;font-size:12px;line-height:1.75;">
        이 번호는 첨부된 계약서 파일 내용으로 계산한 고유값으로, 이후 파일이 조금이라도 바뀌면 전혀 다른 값이 됩니다.<br>
        보관하신 계약서가 원본과 같은지 확인이 필요할 때 새담 담당자에게 이 번호와 함께 문의해 주시면 되며, 평소에는 별도로 입력하거나 사용하실 필요가 없습니다.
    </div>
</td></tr>

<tr><td style="padding:28px 32px 30px;text-align:center;">
    <div style="color:#33404f;font-size:14px;line-height:1.8;">
        함께해 주셔서 감사합니다. 앞으로도 잘 부탁드립니다.</div>
</td></tr>

<tr><td style="padding:18px 20px 22px;border-top:1px solid #d7e0ea;text-align:center;
               background:#f6f8fb;border-radius:0 0 5px 5px;">
    <img src="{logo_url}" alt="{org_name}"
         style="display:block;margin:0 auto;height:34px;width:auto;">
    <div style="margin-top:9px;color:#7b8aa0;font-size:11px;line-height:1.7;">
        {org_name}<br>
        본 메일은 새담 인트라넷 인증전자계약에서 발송되었습니다.
    </div>
</td></tr>

</table></td></tr></table>
</body></html>"""


def _excel_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _normalized_excel_records(frame: pd.DataFrame) -> list[dict[str, str]]:
    aliases = {
        "이름": "성명",
        "계약자명": "성명",
        "이메일": "email",
        "계약자이메일": "email",
        "휴대폰": "핸드폰번호",
        "휴대폰번호": "핸드폰번호",
        "전화번호": "핸드폰번호",
        "연락처": "핸드폰번호",
        "계약자핸드폰번호": "핸드폰번호",
        "학교명": "수탁학교명",
        "부서": "부서명",
        "담당업무": "부서명",
        "강의과목": "부서명",
        "과목": "부서명",
    }
    frame = frame.dropna(how="all").copy()
    frame.columns = [str(column).replace("\ufeff", "").strip() for column in frame.columns]
    for source, target in aliases.items():
        if target not in frame.columns and source in frame.columns:
            frame.rename(columns={source: target}, inplace=True)
    return [
        {str(key): _excel_text(value) for key, value in record.items()}
        for record in frame.to_dict("records")
    ]


def _resolve_company_snapshot(value: object) -> dict:
    requested = str(value or "").strip()
    settings = _company_settings()
    if not requested:
        return _company_snapshot(settings["active_profile_id"])
    for profile in settings["profiles"]:
        if requested in {
            str(profile.get("id", "")),
            str(profile.get("label", "")),
            str(profile.get("company_name", "")),
        }:
            return _company_snapshot(str(profile["id"]))
    raise ValueError(f"회사정보 '{requested}'를 찾을 수 없습니다.")


@verified_contract_bp.route("/admin")
@menu_permission_required("verified_contract_admin")
def admin_page():
    page = max(1, request.args.get("page", 1, type=int))
    status_filter = str(request.args.get("status", "")).strip()
    query = str(request.args.get("q", "")).strip()
    year_filter = str(request.args.get("year", "")).strip()
    category_filter = str(request.args.get("category", "")).strip()
    school_filter = str(request.args.get("school", "")).strip()
    department_filter = str(request.args.get("dept", "")).strip()
    name_filter = str(request.args.get("name", "")).strip()
    params: list[object] = []
    where = []
    if category_filter == "미작성":
        where.append("status IN ('draft','pending','expired')")
    elif category_filter:
        where.append("contract_type=?")
        params.append(category_filter)
    if status_filter:
        where.append("status=?")
        params.append(status_filter)
    if year_filter:
        where.append("substr(COALESCE(signed_at, created_at), 1, 4)=?")
        params.append(year_filter)
    if school_filter:
        where.append("school_name=?")
        params.append(school_filter)
    if department_filter:
        where.append("department=?")
        params.append(department_filter)
    if name_filter:
        where.append("signer_name LIKE ?")
        params.append(f"%{name_filter}%")
    if query:
        where.append(
            "(signer_name LIKE ? OR signer_email LIKE ? OR signer_phone LIKE ? OR school_name LIKE ? OR department LIKE ?)"
        )
        params.extend([f"%{query}%"] * 5)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sort_columns = {
        "id": "id",
        "year": "COALESCE(signed_at, created_at)",
        "category": "contract_type",
        "school": "school_name",
        "dept": "department",
        "name": "signer_name",
        "email": "signer_email",
        "phone": "signer_phone",
        "status": "status",
        "created": "created_at",
        "signed": "signed_at",
    }
    sort_key = request.args.get("sort", "")
    if sort_key not in sort_columns:
        sort_key = "id"
    sort_dir = "asc" if request.args.get("dir") == "asc" else "desc"
    sort_column = sort_columns[sort_key]
    sort_dir_sql = "ASC" if sort_dir == "asc" else "DESC"
    conn = get_db()
    try:
        now_text = _iso()
        expired_rows = conn.execute(
            """
            SELECT id FROM verified_contracts
            WHERE status='pending' AND invitation_expires_at < ?
            """,
            (now_text,),
        ).fetchall()
        for expired_row in expired_rows:
            update_verified_contract(conn, expired_row["id"], {"status": "expired"})
            _record_event(conn, expired_row["id"], "EXPIRED")
        if expired_rows:
            conn.commit()
        counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM verified_contracts GROUP BY status"
            ).fetchall()
        }
        filter_values = conn.execute(
            """
            SELECT DISTINCT substr(COALESCE(signed_at, created_at),1,4) AS year,
                            school_name, department
            FROM verified_contracts
            """
        ).fetchall()
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM verified_contracts {where_sql}",
                params,
            ).fetchone()[0]
        )
        # 강사 20~30명을 한 화면에서 전체 선택해 일괄 발송할 수 있도록 넉넉히 표시한다.
        per_page = 50
        rows = conn.execute(
            f"""
            SELECT * FROM verified_contracts
            {where_sql}
            ORDER BY {sort_column} {sort_dir_sql}, id DESC LIMIT ? OFFSET ?
            """,
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
        dup_counts = {
            (
                dup_row["signer_name"],
                dup_row["contract_type"],
                dup_row["school_name"],
                dup_row["department"],
            ): dup_row["cnt"]
            for dup_row in conn.execute(
                """
                SELECT signer_name, contract_type, school_name, department, COUNT(*) AS cnt
                FROM verified_contracts
                WHERE status NOT IN ('voided','superseded','revoked')
                GROUP BY signer_name, contract_type, school_name, department
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        }
    finally:
        conn.close()
    total_pages = max(1, (total + per_page - 1) // per_page)
    all_total = sum(int(value) for value in counts.values())
    completed_count = int(counts.get("completed", 0))
    pending_count = int(counts.get("draft", 0)) + int(counts.get("pending", 0)) + int(counts.get("expired", 0))
    completion_rate = round(completed_count / all_total * 100, 1) if all_total else 0
    years = sorted({row["year"] for row in filter_values if row["year"]}, reverse=True)
    schools = sorted({row["school_name"] for row in filter_values if row["school_name"]})
    departments = sorted({row["department"] for row in filter_values if row["department"]})
    items = []
    for row in rows:
        item = _row_for_view(row)
        key = (item["signer_name"], item["contract_type"], item["school_name"], item["department"])
        item["is_duplicate"] = (
            dup_counts.get(key, 0) > 1
            and item["status"] not in ("voided", "superseded", "revoked")
        )
        items.append(item)
    companies = _company_settings()
    return render_template(
        "verified_contract/admin.html",
        items=items,
        counts=counts,
        total_count=all_total,
        completed_count=completed_count,
        pending_count=pending_count,
        completion_rate=completion_rate,
        total=total,
        page=page,
        total_pages=total_pages,
        status_filter=status_filter,
        query=query,
        categories=_categories(),
        categories_list=_categories(),
        years=years,
        schools=schools,
        depts=departments,
        companies=companies,
        csrf_token=_csrf_token(),
        sort_key=sort_key,
        sort_dir=sort_dir,
        current_page=page,
        start_page=max(1, ((page - 1) // 20) * 20 + 1),
        end_page=min(total_pages, ((page - 1) // 20) * 20 + 20),
    )


@verified_contract_bp.route("/admin/settings")
@menu_permission_required("verified_contract_admin")
def settings_page():
    """계약 목록과 분리된 인증계약 양식·발송 리소스 관리 화면."""
    companies = _company_settings()
    conn = get_db()
    try:
        mail_senders = _mail_senders_for_view(conn)
    finally:
        conn.close()
    active_sender_id = _active_sender_id()
    if not any(str(item["id"]) == active_sender_id for item in mail_senders):
        active_sender_id = str(mail_senders[0]["id"]) if mail_senders else ""
    return render_template(
        "verified_contract/settings.html",
        categories_list=_categories(),
        template_variables=TEMPLATE_VARIABLES,
        companies=companies,
        mail_senders=mail_senders,
        active_sender_id=active_sender_id,
        csrf_token=_csrf_token(),
        sender_csrf_token=_shared_sender_csrf_token(),
    )


@verified_contract_bp.route(
    "/admin/settings/company/<string:profile_id>/<string:asset_kind>"
)
@menu_permission_required("verified_contract_admin")
def company_asset(profile_id: str, asset_kind: str):
    """회사관리 카드에서 암호화된 로고·도장을 안전하게 미리보기한다."""
    if asset_kind not in {"logo", "stamp"}:
        return "지원하지 않는 회사 이미지입니다.", 404
    profile = next(
        (
            item
            for item in _company_settings()["profiles"]
            if str(item.get("id", "")) == str(profile_id)
        ),
        None,
    )
    if not profile:
        return "회사정보를 찾을 수 없습니다.", 404
    filename = os.path.basename(str(profile.get(f"{asset_kind}_filename", "")))
    if not filename:
        return "등록된 이미지가 없습니다.", 404
    root = VERIFIED_LOGO_ROOT if asset_kind == "logo" else VERIFIED_STAMP_ROOT
    path = root / filename
    if not path.is_file():
        return "회사 이미지 파일을 찾을 수 없습니다.", 404
    display_name = str(
        profile.get(f"{asset_kind}_original_name") or filename
    ).strip()
    return encrypted_response(
        path,
        display_name,
        as_attachment=False,
        mimetype=mimetypes.guess_type(display_name)[0] or "image/png",
    )


@verified_contract_bp.route("/admin/create", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def create_contract():
    data = request.get_json(silent=True) or {}
    contract_type = str(data.get("contract_type", "")).strip()
    signer_name = str(data.get("signer_name", "")).strip()
    signer_email = str(data.get("signer_email", "")).strip().lower()
    try:
        signer_phone = normalize_phone(data.get("signer_phone"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    if contract_type not in _categories():
        return jsonify({"status": "error", "message": "올바른 계약구분을 선택해 주세요."}), 400
    if not signer_name or not re.fullmatch(r"[^@\s]{1,80}", signer_name):
        return jsonify({"status": "error", "message": "계약자 성명을 확인해 주세요."}), 400
    if signer_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", signer_email):
        return jsonify({"status": "error", "message": "계약자 이메일을 확인해 주세요."}), 400
    if not signer_phone and not signer_email:
        return jsonify({"status": "error", "message": "휴대폰번호 또는 이메일주소를 입력해 주세요."}), 400

    profile = _company_snapshot(str(data.get("company_profile_id", "")).strip() or None)
    terms1, terms2 = _read_terms(contract_type)
    if not terms1.strip():
        return jsonify({"status": "error", "message": "선택한 계약구분의 1페이지 양식이 비어 있습니다."}), 400

    contract_data = {
        "계약구분": contract_type,
        "수탁학교명": str(data.get("school_name", "")).strip(),
        "부서명": str(data.get("department", "")).strip(),
        "성명": signer_name,
        "email": signer_email,
        "연락처": signer_phone,
    }
    for key in (
        "수수료",
        "보조금",
        "경력수당",
        "직책수당",
        "기타",
        "근무시간",
        "계약기간",
        "비고1",
        "비고2",
        "비고3",
        "비고4",
    ):
        contract_data[key] = str(data.get(key, "")).strip()

    conn = get_db()
    try:
        duplicate_rows = conn.execute(
            """
            SELECT id FROM verified_contracts
            WHERE signer_name=? AND contract_type=? AND school_name=? AND department=?
              AND status NOT IN ('voided','superseded','revoked')
            """,
            (
                signer_name,
                contract_type,
                contract_data["수탁학교명"],
                contract_data["부서명"],
            ),
        ).fetchall()
    finally:
        conn.close()
    duplicate_ids = [int(r["id"]) for r in duplicate_rows]

    token = secrets.token_urlsafe(32)
    expires_days = min(30, max(1, int(data.get("expires_days") or 7)))
    expires_at = _iso(_now() + timedelta(days=expires_days))
    values = {
        "contract_type": contract_type,
        "school_name": contract_data["수탁학교명"],
        "department": contract_data["부서명"],
        "signer_name": signer_name,
        "signer_email": signer_email,
        "signer_phone": signer_phone,
        "contract_data_json": json.dumps(contract_data, ensure_ascii=False),
        "status": "pending",
        "title_snapshot": _contract_title(contract_type, profile["company_name"]),
        "terms1_snapshot": terms1,
        "terms2_snapshot": terms2,
        "company_snapshot_json": json.dumps(profile, ensure_ascii=False),
        "agreement_snapshot_json": json.dumps(AGREEMENTS, ensure_ascii=False),
        "invitation_token_hash": _token_hash(token),
        "invitation_expires_at": expires_at,
        "created_by": str(session.get("user_name") or session.get("emp_no") or "admin"),
    }
    conn = get_db()
    try:
        contract_id = insert_verified_contract(conn, values)
        _record_event(conn, contract_id, "CREATED", {"expires_days": expires_days})
        conn.commit()
        row = conn.execute(
            "SELECT * FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    invitation_url = _external_url(token)
    delivery_status = "sent"
    delivery_error = ""
    delivery_channel = "alimtalk" if signer_phone else "email"
    delivery_recipient = signer_phone or signer_email
    message_id = ""
    try:
        delivery_channel, delivery_recipient, message_id = _send_invitation(row, invitation_url)
    except Exception as exc:
        delivery_status = "failed"
        delivery_error = str(exc)[:500]

    conn = get_db()
    try:
        update_verified_contract(
            conn,
            contract_id,
            {
                "invitation_sent_at": _iso() if delivery_status == "sent" else None,
                "invite_mail_status": delivery_status,
                "invite_mail_error": delivery_error,
                "invite_channel": delivery_channel,
                "invite_message_id": message_id,
            },
        )
        _record_event(
            conn,
            contract_id,
            "INVITATION_SENT" if delivery_status == "sent" else "INVITATION_FAILED",
            {
                "channel": delivery_channel,
                "recipient": mask_phone(delivery_recipient) if delivery_channel == "alimtalk" else _mask_email(delivery_recipient),
                "message_id": message_id,
                "error": delivery_error,
            },
        )
        conn.commit()
    finally:
        conn.close()

    channel_label = "카카오 알림톡" if delivery_channel == "alimtalk" else "이메일"
    message = f"계약을 등록하고 인증 링크의 {channel_label} 발송을 접수했습니다."
    if delivery_status == "failed":
        message = f"계약은 등록했지만 {channel_label} 발송에 실패했습니다: {delivery_error}"
    if duplicate_ids:
        message += (
            f"\n\n⚠ 동일 인물·계약구분·학교·부서 조합의 기존 계약이 {len(duplicate_ids)}건 있습니다."
            " 목록에서 기존 계약을 확인하고 [폐기] 또는 [변경계약] 처리해 주세요."
        )
    return jsonify(
        {
            "status": "success" if delivery_status == "sent" else "warning",
            "message": message,
            "contract_id": contract_id,
            "invitation_url": invitation_url,
            "duplicate_ids": duplicate_ids,
            "duplicate_name": signer_name if duplicate_ids else "",
        }
    )


def _build_excel_workbook(*, include_samples: bool) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "계약자 일괄등록"
    headers = [
        "계약구분",
        "성명",
        "email",
        "핸드폰번호",
        "수탁학교명",
        "부서명",
        "계약기간",
        "수수료",
        "보조금",
        "경력수당",
        "직책수당",
        "기타",
        "근무시간",
        "비고1",
        "비고2",
        "비고3",
        "비고4",
        "회사정보",
    ]
    sheet.append(headers)
    if include_samples:
        company_label = _company_profile().get("label", "기본 회사")
        # 계약구분별 샘플 1행씩 (열 순서는 위 headers 와 동일)
        sample_presets = {
            "방과후강사": [
                "홍길동", "", "010-1234-5678", "새담초등학교", "방과후 코딩",
                "2026.03.01 ~ 2027.02.28", "50000", "", "", "", "",
                "화·목 14:00~16:00", "주 2회 수업", "", "", "",
            ],
            "맞춤형강사": [
                "김새담", "sadam.kim@example.com", "", "새담중학교", "맞춤형 수학",
                "2026.03.01 ~ 2027.02.28", "45000", "50000", "", "", "",
                "월·수·금 15:00~17:00", "맞춤형 교육과정", "", "", "",
            ],
            "코디사업자": [
                "이코디", "cordi.lee@example.com", "010-2345-6789", "새담초등학교", "센터장",
                "2026.03.01 ~ 2027.02.28", "1500000", "", "100000", "200000", "",
                "평일 13:00~18:00", "센터 운영 총괄", "", "", "",
            ],
            "코디근로자": [
                "박코디", "", "010-3456-7890", "새담초등학교", "방과후 코디네이터",
                "2026.03.01 ~ 2027.02.28", "", "2100000", "50000", "", "",
                "평일 13:00~18:00 (주 25시간)", "4대보험 가입 대상", "", "", "",
            ],
            "안전코디": [
                "최안전", "", "010-4567-8901", "새담초등학교", "안전관리",
                "2026.03.01 ~ 2027.02.28", "", "1200000", "", "", "50000",
                "평일 12:30~16:30", "등하교 안전지도", "", "", "",
            ],
            "직원근로자": [
                "정직원", "staff.jung@example.com", "010-5678-9012", "본사", "교육운영팀",
                "2026.03.01 ~ 2027.02.28", "", "2600000", "100000", "150000", "",
                "평일 09:00~18:00 (주 40시간)", "정규 근로계약", "", "", "",
            ],
            "직원사업자": [
                "한사업", "biz.han@example.com", "010-6789-0123", "본사", "교육기획",
                "2026.03.01 ~ 2027.02.28", "3000000", "", "", "", "",
                "협의 근무", "프리랜서 위탁계약", "", "", "",
            ],
            "원어민근로자": [
                "John Smith", "john.smith@example.com", "010-7890-1234", "새담중학교", "영어회화",
                "2026.03.01 ~ 2027.02.28", "", "2800000", "", "", "300000",
                "평일 09:00~17:00", "E-2 비자 / 숙소지원", "", "", "",
            ],
            "원어민사업자": [
                "Emily Brown", "emily.brown@example.com", "010-8901-2345", "새담고등학교", "영어회화",
                "2026.03.01 ~ 2027.02.28", "60000", "", "", "", "",
                "화·목 16:00~18:00", "F-4 비자 / 위탁계약", "", "", "",
            ],
            "전담코디": [
                "윤전담", "", "010-9012-3456", "새담초등학교", "전담 코디네이터",
                "2026.03.01 ~ 2027.02.28", "", "1800000", "50000", "", "",
                "평일 12:00~18:00", "학교 전담 배치", "", "", "",
            ],
        }
        default_preset = [
            "샘플이름", "sample@example.com", "010-0000-0000", "새담초등학교", "담당업무",
            "2026.03.01 ~ 2027.02.28", "50000", "", "", "", "",
            "주 2회", "", "", "", "",
        ]
        for category in _categories():
            preset = sample_presets.get(category, default_preset)
            sheet.append([category, *preset, company_label])
    header_fill = PatternFill("solid", fgColor="173F6A")
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")
    widths = {
        "A": 18,
        "B": 14,
        "C": 28,
        "D": 18,
        "E": 24,
        "F": 22,
        "G": 27,
        "H": 14,
        "I": 14,
        "J": 14,
        "K": 14,
        "L": 14,
        "M": 18,
        "N": 18,
        "O": 18,
        "P": 18,
        "Q": 18,
        "R": 20,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    guide = workbook.create_sheet("작성안내")
    guide_rows = [
        ("필수 열", "계약구분, 성명, 핸드폰번호 또는 email 중 하나"),
        ("발송 방법", "핸드폰번호가 있으면 카카오 알림톡, 없고 email만 있으면 이메일로 계약링크 발송"),
        ("학교·업무", "수탁학교명, 부서명(담당업무·강의과목·직책)"),
        ("회사정보", "비우면 현재 기본 회사정보 사용. 등록된 구분명·회사명도 입력 가능"),
        ("민감정보 입력", "주민번호·은행·계좌번호는 계약자가 계약 작성 화면에서 직접 입력합니다."),
        ("민감정보 보관", "입력값은 서버에서 암호화하여 저장되며 엑셀에는 작성하지 않습니다."),
        ("대량발송", "업로드 후 목록에서 대상을 선택하고 '선택 계약링크 발송'을 누릅니다."),
        ("계약구분", ", ".join(_categories())),
    ]
    for row in guide_rows:
        guide.append(row)
    guide.column_dimensions["A"].width = 18
    guide.column_dimensions["B"].width = 100
    for cell in guide["A"]:
        cell.font = Font(bold=True, color="173F6A")

    return workbook


def _excel_download(*, include_samples: bool, filename: str):
    workbook = _build_excel_workbook(include_samples=include_samples)

    memory = io.BytesIO()
    workbook.save(memory)
    memory.seek(0)
    return send_file(
        memory,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@verified_contract_bp.route("/admin/excel-template")
@menu_permission_required("verified_contract_admin")
def download_excel_template():
    """현재 등록 열 구성을 반영한 빈 업로드 양식을 요청 시 생성한다."""
    return _excel_download(
        include_samples=False,
        filename="인증전자계약_일괄등록_양식.xlsx",
    )


@verified_contract_bp.route("/admin/sample-excel")
@menu_permission_required("verified_contract_admin")
def download_sample_excel():
    """Render 배포 환경에서도 요청할 때마다 최신 열 구성으로 샘플을 자동 생성한다."""
    return _excel_download(
        include_samples=True,
        filename="인증전자계약_샘플엑셀.xlsx",
    )


@verified_contract_bp.route("/admin/upload-excel", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def upload_excel():
    uploaded = request.files.get("excel_file")
    if not uploaded or not uploaded.filename:
        return jsonify({"status": "error", "message": "업로드할 엑셀 파일을 선택해 주세요."}), 400
    if Path(uploaded.filename).suffix.lower() != ".xlsx":
        return jsonify({"status": "error", "message": "엑셀 파일은 .xlsx 형식만 사용할 수 있습니다."}), 400
    if request.content_length and request.content_length > 10 * 1024 * 1024:
        return jsonify({"status": "error", "message": "엑셀 파일은 10MB 이하만 사용할 수 있습니다."}), 413
    try:
        records = _normalized_excel_records(pd.read_excel(uploaded, dtype=str))
    except Exception as exc:
        return jsonify({"status": "error", "message": f"엑셀 파일을 읽을 수 없습니다: {str(exc)[:200]}"}), 400
    if not records:
        return jsonify({"status": "error", "message": "등록할 계약자 행이 없습니다."}), 400
    if len(records) > 500:
        return jsonify({"status": "error", "message": "한 번에 최대 500명까지 등록할 수 있습니다."}), 400

    categories = set(_categories())
    existing_keys = set()
    conn = get_db()
    try:
        for existing in conn.execute(
            """
            SELECT contract_type, signer_name, signer_email, signer_phone, school_name, department
            FROM verified_contracts WHERE status IN ('draft','pending')
            """
        ).fetchall():
            existing_keys.add(
                (
                    existing["contract_type"],
                    existing["signer_name"],
                    existing["signer_email"].lower(),
                    existing["signer_phone"],
                    existing["school_name"],
                    existing["department"],
                )
            )

        inserted = 0
        skipped = 0
        duplicated = 0
        errors = []
        seen_keys = set()
        terms_cache: dict[str, tuple[str, str]] = {}
        created_by = str(session.get("user_name") or session.get("emp_no") or "admin")
        for excel_row, record in enumerate(records, start=2):
            contract_type = _excel_text(record.get("계약구분"))
            signer_name = _excel_text(record.get("성명"))
            signer_email = _excel_text(record.get("email")).lower()
            raw_phone = record.get("핸드폰번호")
            try:
                signer_phone = normalize_phone(raw_phone)
            except ValueError as exc:
                signer_phone = ""
                row_errors = [str(exc)]
            else:
                row_errors = []
            school_name = _excel_text(record.get("수탁학교명"))
            department = _excel_text(record.get("부서명"))
            if contract_type not in categories:
                row_errors.append(f"등록되지 않은 계약구분: {contract_type or '빈 값'}")
            if not signer_name or len(signer_name) > 80:
                row_errors.append("성명 확인 필요")
            if signer_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", signer_email):
                row_errors.append("이메일 형식 확인 필요")
            if not signer_phone and not signer_email:
                row_errors.append("핸드폰번호 또는 이메일 중 하나 필요")
            try:
                profile = _resolve_company_snapshot(record.get("회사정보"))
            except ValueError as exc:
                profile = None
                row_errors.append(str(exc))
            key = (
                contract_type,
                signer_name,
                signer_email,
                signer_phone,
                school_name,
                department,
            )
            # 같은 파일 안에서의 완전 중복 행만 막고, 이미 등록된 서명대기/등록대기 건은
            # 재계약(폐기·변경계약) 시나리오일 수 있으므로 차단하지 않고 등록 후 안내한다.
            if key in seen_keys:
                row_errors.append("업로드한 엑셀 파일 안에서 같은 행이 중복됨")
            if row_errors:
                skipped += 1
                errors.append({"row": excel_row, "name": signer_name or "-", "reason": ", ".join(row_errors)})
                continue
            if key in existing_keys:
                duplicated += 1
            terms1, terms2 = terms_cache.setdefault(contract_type, _read_terms(contract_type))
            if not terms1.strip():
                skipped += 1
                errors.append({"row": excel_row, "name": signer_name, "reason": "계약양식 내용 1이 비어 있음"})
                continue

            contract_data = {
                "계약구분": contract_type,
                "수탁학교명": school_name,
                "부서명": department,
                "성명": signer_name,
                "email": signer_email,
                "연락처": signer_phone,
            }
            for field in (
                "수수료",
                "보조금",
                "경력수당",
                "직책수당",
                "기타",
                "근무시간",
                "계약기간",
                "비고1",
                "비고2",
                "비고3",
                "비고4",
            ):
                contract_data[field] = _excel_text(record.get(field))
            placeholder_token = secrets.token_urlsafe(32)
            contract_id = insert_verified_contract(
                conn,
                {
                    "contract_type": contract_type,
                    "school_name": school_name,
                    "department": department,
                    "signer_name": signer_name,
                    "signer_email": signer_email,
                    "signer_phone": signer_phone,
                    "contract_data_json": json.dumps(contract_data, ensure_ascii=False),
                    "status": "draft",
                    "title_snapshot": _contract_title(contract_type, profile["company_name"]),
                    "terms1_snapshot": terms1,
                    "terms2_snapshot": terms2,
                    "company_snapshot_json": json.dumps(profile, ensure_ascii=False),
                    "agreement_snapshot_json": json.dumps(AGREEMENTS, ensure_ascii=False),
                    "invitation_token_hash": _token_hash(placeholder_token),
                    "invitation_expires_at": _iso(),
                    "invite_mail_status": "not_sent",
                    "invite_channel": "alimtalk" if signer_phone else "email",
                    "created_by": created_by,
                },
            )
            _record_event(
                conn,
                contract_id,
                "BULK_IMPORTED",
                {"excel_row": excel_row, "source_filename": secure_filename(uploaded.filename)},
            )
            seen_keys.add(key)
            inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if inserted == 0:
        return jsonify(
            {
                "status": "error",
                "message": "등록된 계약자가 없습니다. 오류 내용을 확인해 주세요.",
                "inserted": 0,
                "skipped": skipped,
                "errors": errors[:50],
            }
        ), 400
    message = f"{inserted}명 등록 완료" + (f", {skipped}명 제외" if skipped else "")
    if duplicated:
        message += (
            f"\n\n⚠ {duplicated}명은 기존 서명대기/등록대기 계약과 동일한 조합(성명·계약구분·학교·부서)입니다."
            " 목록에서 기존 계약을 확인하고 [폐기] 또는 [변경계약] 처리해 주세요."
        )
    return jsonify(
        {
            "status": "warning" if (skipped or duplicated) else "success",
            "message": message,
            "inserted": inserted,
            "skipped": skipped,
            "duplicated": duplicated,
            "errors": errors[:50],
        }
    )


@verified_contract_bp.route("/admin/bulk-send", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def bulk_send_invitations():
    data = request.get_json(silent=True) or {}
    try:
        ids = sorted({int(value) for value in data.get("ids", [])})
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "선택한 계약번호를 확인해 주세요."}), 400
    if not ids:
        return jsonify({"status": "error", "message": "발송할 계약자를 선택해 주세요."}), 400
    if len(ids) > 100:
        return jsonify({"status": "error", "message": "한 번에 최대 100명까지 발송할 수 있습니다."}), 400
    expires_days = min(30, max(1, int(data.get("expires_days") or 7)))
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    queued = []
    try:
        rows = conn.execute(
            f"SELECT * FROM verified_contracts WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
        for row in rows:
            if row["status"] not in {"draft", "pending", "expired"}:
                continue
            token = secrets.token_urlsafe(32)
            expires_at = _iso(_now() + timedelta(days=expires_days))
            update_verified_contract(
                conn,
                row["id"],
                {
                    "status": "pending",
                    "invitation_token_hash": _token_hash(token),
                    "invitation_expires_at": expires_at,
                    "opened_at": None,
                    "otp_hash": None,
                    "otp_expires_at": None,
                    "otp_attempts": 0,
                    "otp_sent_at": None,
                    "verified_at": None,
                    "invite_mail_status": "sending",
                    "invite_mail_error": "",
                },
            )
            queued_row = dict(row)
            queued_row["invitation_expires_at"] = expires_at
            queued.append((queued_row, token, _external_url(token)))
            _record_event(
                conn,
                row,
                "BULK_SEND_QUEUED",
                {"expires_days": expires_days},
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not queued:
        return jsonify({"status": "error", "message": "발송 가능한 등록대기·서명대기 계약이 없습니다."}), 400

    sent_count = 0
    failed = []
    smtp = None
    smtp_sender = None
    smtp_setup_error = ""
    solapi_config = None

    conn = get_db()
    try:
        for row, token, invitation_url in queued:
            channel = "alimtalk" if str(row["signer_phone"] or "").strip() else "email"
            recipient = row["signer_phone"] if channel == "alimtalk" else row["signer_email"]
            masked_recipient = "-"
            message_id = ""
            error = ""
            try:
                channel, recipient, masked_recipient = _delivery_recipient(row)
            except (ValueError, RuntimeError) as exc:
                error = str(exc)[:500]
            if not error and channel == "alimtalk":
                try:
                    if solapi_config is None:
                        solapi_config = get_solapi_settings()
                    result = send_alimtalk(
                        recipient,
                        signer_name=row["signer_name"],
                        invitation_url=invitation_url,
                        settings=solapi_config,
                    )
                    message_id = str(result.get("message_id") or "")
                except Exception as exc:
                    error = str(exc)[:500]
            elif not error:
                if smtp is None and not smtp_setup_error:
                    try:
                        sender_row = _active_sender_row(conn)
                        if not sender_row:
                            raise RuntimeError(
                                "인증전자계약 발송계정이 설정되지 않았습니다. "
                                "양식관리 > 발송메일계정에서 계정을 등록·선택해 주세요."
                            )
                        # 표시용 딕셔너리는 비밀번호를 빼고 돌려주므로 로그인에는 원본 row를 그대로 쓴다.
                        smtp_sender = dict(sender_row)
                        smtp = _smtp_login_for_sender(smtp_sender)
                    except Exception as exc:
                        smtp_setup_error = str(exc)[:500]
                error = smtp_setup_error
                if smtp is not None:
                    try:
                        message = MIMEMultipart()
                        message["From"] = _sender_from_header(smtp_sender)
                        message["To"] = recipient
                        message["Subject"] = f"[전자계약 요청] {row['title_snapshot']}"
                        message.attach(
                            MIMEText(_invitation_html(row, invitation_url), "html", "utf-8")
                        )
                        _verify_smtp_sender(smtp, smtp_sender)
                        smtp.send_message(
                            message,
                            from_addr=str(smtp_sender.get("email") or "").strip().lower(),
                        )
                    except Exception as exc:
                        error = str(exc)[:500]
            if error:
                failed.append(
                    {
                        "id": row["id"],
                        "name": row["signer_name"],
                        "channel": channel,
                        "reason": error,
                    }
                )
                update_verified_contract(
                    conn,
                    row["id"],
                    {
                        "invite_mail_status": "failed",
                        "invite_mail_error": error,
                        "invite_channel": channel,
                        "invite_message_id": message_id,
                    },
                )
                _record_event(
                    conn,
                    row,
                    "INVITATION_FAILED",
                    {
                        "channel": channel,
                        "recipient": masked_recipient,
                        "error": error,
                    },
                )
            else:
                sent_count += 1
                update_verified_contract(
                    conn,
                    row["id"],
                    {
                        "invitation_sent_at": _iso(),
                        "invite_mail_status": "sent",
                        "invite_mail_error": "",
                        "invite_channel": channel,
                        "invite_message_id": message_id,
                    },
                )
                _record_event(
                    conn,
                    row,
                    "INVITATION_SENT",
                    {
                        "channel": channel,
                        "recipient": masked_recipient,
                        "message_id": message_id,
                        "bulk": True,
                    },
                )
        conn.commit()
    finally:
        conn.close()
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                try:
                    smtp.close()
                except Exception:
                    pass
    return jsonify(
        {
            "status": "warning" if failed else "success",
            "message": f"{sent_count}명 발송 접수" + (f", {len(failed)}명 실패" if failed else ""),
            "sent": sent_count,
            "failed": failed,
        }
    )


@verified_contract_bp.route("/admin/bulk-revoke", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def bulk_revoke():
    try:
        ids = sorted({int(value) for value in (request.get_json(silent=True) or {}).get("ids", [])})
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "선택 항목을 확인해 주세요."}), 400
    if not ids:
        return jsonify({"status": "error", "message": "취소할 계약을 선택해 주세요."}), 400
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT id, status FROM verified_contracts WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        changed = 0
        for row in rows:
            if row["status"] == "completed":
                continue
            update_verified_contract(conn, row["id"], {"status": "revoked"})
            _record_event(conn, row, "REVOKED", {"bulk": True})
            changed += 1
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "message": f"{changed}건의 계약 링크를 취소했습니다."})


@verified_contract_bp.route("/admin/bulk-delete", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def bulk_delete():
    try:
        ids = sorted({int(value) for value in (request.get_json(silent=True) or {}).get("ids", [])})
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "선택 항목을 확인해 주세요."}), 400
    if not ids:
        return jsonify({"status": "error", "message": "삭제할 계약을 선택해 주세요."}), 400
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT id, pdf_filename, signature_filename FROM verified_contracts WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        for row in rows:
            delete_file(VERIFIED_CONTRACTS_ROOT / os.path.basename(row["pdf_filename"] or ""))
            delete_file(VERIFIED_SIGNATURE_ROOT / os.path.basename(row["signature_filename"] or ""))
        conn.execute(f"DELETE FROM verified_contract_events WHERE contract_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM verified_contracts WHERE id IN ({placeholders})", ids)
        conn.commit()
        deleted = len(rows)
    finally:
        conn.close()
    return jsonify({"status": "success", "message": f"{deleted}건의 계약 등록정보와 계약서 파일을 완전히 삭제했습니다."})


@verified_contract_bp.route("/admin/bulk-void", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def bulk_void():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip()
    if action not in ("discard", "amend"):
        return jsonify({"status": "error", "message": "처리 방식을 확인해 주세요."}), 400
    notify = bool(data.get("notify"))
    try:
        ids = sorted({int(value) for value in data.get("ids", [])})
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "선택 항목을 확인해 주세요."}), 400
    if not ids:
        return jsonify({"status": "error", "message": "처리할 계약을 선택해 주세요."}), 400
    new_status = "voided" if action == "discard" else "superseded"
    reason_label = "폐기" if action == "discard" else "변경계약"
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM verified_contracts WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        processed = 0
        mail_sent = 0
        mail_failed = []
        for row in rows:
            # 상태만 변경하며, 계약 정보나 계약서·서명 파일은 그대로 보존한다.
            if row["status"] in ("voided", "superseded"):
                continue
            update_verified_contract(conn, row["id"], {"status": new_status})
            _record_event(
                conn,
                row,
                "VOIDED" if action == "discard" else "SUPERSEDED",
                {"reason": reason_label, "notify": notify},
            )
            processed += 1
            if not notify:
                continue
            try:
                pdf_filename = str(row["pdf_filename"] or "")
                if pdf_filename:
                    pdf_path = VERIFIED_CONTRACTS_ROOT / os.path.basename(pdf_filename)
                    with temporary_decrypted_path(pdf_path, pdf_path.name) as mail_pdf_path:
                        _send_mail(
                            row["signer_email"],
                            f"[전자계약 {reason_label} 안내] {row['title_snapshot']}",
                            _void_notice_html(row, reason_label, has_attachment=True),
                            attachments=mail_pdf_path,
                        )
                else:
                    _send_mail(
                        row["signer_email"],
                        f"[전자계약 {reason_label} 안내] {row['title_snapshot']}",
                        _void_notice_html(row, reason_label),
                    )
                mail_sent += 1
            except Exception as exc:
                mail_failed.append({"name": row["signer_name"], "reason": str(exc)[:200]})
        conn.commit()
    finally:
        conn.close()
    message = f"{processed}건을 {reason_label} 처리했습니다. (계약 정보·파일은 삭제되지 않았습니다)"
    if notify:
        message += f" 안내메일 {mail_sent}건 발송."
    if mail_failed:
        message += f" (안내메일 발송 실패 {len(mail_failed)}건)"
    return jsonify({"status": "success", "message": message, "failed": mail_failed})


def _pdf_first_pages(data: bytes, limit: int) -> bytes:
    """계약서 PDF 앞쪽 몇 장만 잘라 낸다. 실패하면 원본을 그대로 돌려준다."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) <= limit:
            return data
        writer = PdfWriter()
        for page in reader.pages[:limit]:
            writer.add_page(page)
        sliced = io.BytesIO()
        writer.write(sliced)
        return sliced.getvalue()
    except Exception:
        return data


@verified_contract_bp.route("/admin/download-selected")
@menu_permission_required("verified_contract_admin")
def download_selected():
    try:
        ids = sorted({int(value) for value in request.args.get("ids", "").split(",") if value})
    except ValueError:
        return "선택한 계약번호를 확인해 주세요.", 400
    if not ids or len(ids) > 500:
        return "완료 계약서를 1~500건 선택해 주세요.", 400
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT id, signer_name, pdf_filename
            FROM verified_contracts
            WHERE id IN ({placeholders}) AND status='completed'
            ORDER BY id
            """,
            ids,
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        # 계약자가 아직 서명하지 않아 계약서 파일 자체가 만들어지지 않은 경우.
        return "아직 계약이 완료되지 않았습니다.", 404
    # pages=1-2 면 계약서 앞 2장만 담는다(기본값은 전체).
    page_limit = 2 if request.args.get("pages") == "1-2" else 0
    memory = io.BytesIO()
    count = 0
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            path = VERIFIED_CONTRACTS_ROOT / os.path.basename(row["pdf_filename"] or "")
            if not path.is_file():
                continue
            safe_signer = re.sub(r'[\\/:*?"<>|]+', "_", str(row["signer_name"]))[:60]
            content = read_decrypted(path)
            name = path.name
            if page_limit:
                content = _pdf_first_pages(content, page_limit)
                name = f"1-2p_{name}"
            archive.writestr(f"{row['id']}_{safe_signer}_{name}", content)
            count += 1
    if count == 0:
        return "완료된 계약서 파일을 찾을 수 없습니다. 담당자에게 문의해 주세요.", 404
    memory.seek(0)
    response = send_file(
        memory,
        mimetype="application/zip",
        as_attachment=True,
        download_name=(
            f"인증전자계약{'_1-2p' if page_limit else ''}"
            f"_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.zip"
        ),
    )
    # 고른 건수와 실제로 담긴 건수가 다르면 화면에서 알려 줄 수 있게 함께 내려보낸다.
    response.headers["X-Contract-Requested"] = str(len(ids))
    response.headers["X-Contract-Included"] = str(count)
    return response


BACKUP_CHUNK_SIZES = (20, 50, 100)
BACKUP_LIST_FILENAME = "인증전자계약_계약리스트.xlsx"


def _backup_rows(conn):
    """완료된 계약을 번호 순으로 모은다. 파일이 없는 건도 함께 돌려준다."""
    rows = conn.execute(
        """
        SELECT * FROM verified_contracts
        WHERE status='completed'
        ORDER BY id
        """
    ).fetchall()
    entries = []
    for row in rows:
        filename = os.path.basename(str(row["pdf_filename"] or ""))
        path = VERIFIED_CONTRACTS_ROOT / filename if filename else None
        exists = bool(path and path.is_file())
        entries.append({
            "row": row,
            "path": path if exists else None,
            "size": stored_plain_size(path) if exists else 0,
            "missing": not exists,
        })
    return entries


def _backup_chunk_size(value: object) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 50
    return size if size in BACKUP_CHUNK_SIZES else 50


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def _build_contract_list_workbook(entries) -> Workbook:
    """계약리스트 엑셀. 주민번호·계좌번호 같은 식별정보는 담지 않는다."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "계약리스트"
    headers = [
        "번호", "계약구분", "연도", "수탁학교명", "부서명", "성명",
        "연락처", "이메일", "계약기간", "근무시간",
        "수수료", "보조금", "경력수당", "직책수당", "기타",
        "비고1", "비고2", "비고3", "비고4",
        "등록일", "완료일", "계약서 파일명",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for index, entry in enumerate(entries, start=1):
        row = entry["row"]
        item = _row_for_view(row)
        data = json.loads(row["contract_data_json"] or "{}")
        sheet.append([
            index,
            row["contract_type"],
            item.get("연도", ""),
            row["school_name"],
            row["department"],
            row["signer_name"],
            item.get("signer_phone_display", ""),
            row["signer_email"],
            str(data.get("계약기간", "")),
            str(data.get("근무시간", "")),
            str(data.get("수수료", "")),
            str(data.get("보조금", "")),
            str(data.get("경력수당", "")),
            str(data.get("직책수당", "")),
            str(data.get("기타", "")),
            str(data.get("비고1", "")),
            str(data.get("비고2", "")),
            str(data.get("비고3", "")),
            str(data.get("비고4", "")),
            item.get("created_display", ""),
            item.get("signed_display", ""),
            os.path.basename(str(row["pdf_filename"] or "")) or "(파일 없음)",
        ])
    widths = [6, 12, 7, 18, 14, 10, 15, 24, 22, 18, 11, 11, 11, 11, 11, 14, 14, 14, 14, 18, 18, 40]
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[sheet.cell(row=1, column=column).column_letter].width = width
    sheet.freeze_panes = "A2"
    return workbook


def _contract_list_bytes(entries) -> bytes:
    memory = io.BytesIO()
    _build_contract_list_workbook(entries).save(memory)
    return memory.getvalue()


@verified_contract_bp.route("/admin/backup")
@menu_permission_required("verified_contract_admin")
def backup_page():
    """완료 계약서를 한 번에 받으면 파일이 너무 커서, 번호순으로 나눠 받는 화면."""
    chunk_size = _backup_chunk_size(request.args.get("size"))
    conn = get_db()
    try:
        entries = _backup_rows(conn)
    finally:
        conn.close()
    available = [entry for entry in entries if not entry["missing"]]
    total_bytes = sum(entry["size"] for entry in available)
    parts = []
    for start in range(0, len(available), chunk_size):
        group = available[start:start + chunk_size]
        parts.append({
            "index": len(parts) + 1,
            "first": start + 1,
            "last": start + len(group),
            "count": len(group),
            "size_text": _human_size(sum(entry["size"] for entry in group)),
        })
    return render_template(
        "verified_contract/backup.html",
        chunk_size=chunk_size,
        chunk_sizes=BACKUP_CHUNK_SIZES,
        parts=parts,
        file_count=len(available),
        missing=[
            {"id": entry["row"]["id"], "name": entry["row"]["signer_name"]}
            for entry in entries if entry["missing"]
        ],
        total_size_text=_human_size(total_bytes),
        list_count=len(entries),
        storage_path=str(VERIFIED_CONTRACTS_ROOT),
    )


@verified_contract_bp.route("/admin/backup/list")
@menu_permission_required("verified_contract_admin")
def backup_contract_list():
    conn = get_db()
    try:
        entries = _backup_rows(conn)
    finally:
        conn.close()
    if not entries:
        return "완료된 계약이 없습니다.", 404
    return send_file(
        io.BytesIO(_contract_list_bytes(entries)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=BACKUP_LIST_FILENAME,
    )


@verified_contract_bp.route("/admin/backup/part")
@menu_permission_required("verified_contract_admin")
def backup_part():
    chunk_size = _backup_chunk_size(request.args.get("size"))
    part = max(1, request.args.get("part", 1, type=int))
    conn = get_db()
    try:
        entries = _backup_rows(conn)
    finally:
        conn.close()
    available = [entry for entry in entries if not entry["missing"]]
    start = (part - 1) * chunk_size
    group = available[start:start + chunk_size]
    if not group:
        return "해당 구간에 받을 계약서가 없습니다.", 404
    total_parts = max(1, (len(available) + chunk_size - 1) // chunk_size)
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # 계약리스트는 첫 번째 묶음에만 넣어 중복을 피한다.
        if part == 1:
            archive.writestr(BACKUP_LIST_FILENAME, _contract_list_bytes(entries))
        for offset, entry in enumerate(group, start=start + 1):
            row = entry["row"]
            safe_signer = re.sub(r'[\\/:*?"<>|]+', "_", str(row["signer_name"]))[:60]
            archive.writestr(
                f"{offset:04d}_{row['id']}_{safe_signer}_{entry['path'].name}",
                read_decrypted(entry["path"]),
            )
    memory.seek(0)
    return send_file(
        memory,
        mimetype="application/zip",
        as_attachment=True,
        download_name=(
            f"인증전자계약_백업_{part:02d}of{total_parts:02d}"
            f"_{start + 1}-{start + len(group)}.zip"
        ),
    )


@verified_contract_bp.route("/admin/<int:contract_id>/resend", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def resend_invitation(contract_id: int):
    token = secrets.token_urlsafe(32)
    expires_at = _iso(_now() + timedelta(days=7))
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "계약을 찾을 수 없습니다."}), 404
        if row["status"] == "completed":
            return jsonify({"status": "error", "message": "완료된 계약은 다시 발송할 수 없습니다."}), 400
        update_verified_contract(
            conn,
            contract_id,
            {
                "status": "pending",
                "invitation_token_hash": _token_hash(token),
                "invitation_expires_at": expires_at,
                "opened_at": None,
                "otp_hash": None,
                "otp_expires_at": None,
                "otp_attempts": 0,
                "otp_sent_at": None,
                "verified_at": None,
                "invite_mail_status": "waiting",
                "invite_mail_error": "",
                "invite_message_id": "",
            },
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
    finally:
        conn.close()

    invitation_url = _external_url(token)
    channel = "alimtalk" if str(row["signer_phone"] or "").strip() else "email"
    recipient = row["signer_phone"] if channel == "alimtalk" else row["signer_email"]
    masked_recipient = "-"
    message_id = ""
    try:
        channel, recipient, masked_recipient = _delivery_recipient(row)
        channel, recipient, message_id = _send_invitation(row, invitation_url)
        status, error = "sent", ""
    except Exception as exc:
        status, error = "failed", str(exc)[:500]
    conn = get_db()
    try:
        update_verified_contract(
            conn,
            contract_id,
            {
                "invitation_sent_at": _iso() if status == "sent" else None,
                "invite_mail_status": status,
                "invite_mail_error": error,
                "invite_channel": channel,
                "invite_message_id": message_id,
            },
        )
        _record_event(
            conn,
            contract_id,
            "INVITATION_RESENT" if status == "sent" else "INVITATION_FAILED",
            {
                "channel": channel,
                "recipient": masked_recipient,
                "message_id": message_id,
                "error": error,
            },
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(
        {
            "status": "success" if status == "sent" else "warning",
            "message": (
                f"새 인증 링크의 {'카카오 알림톡' if channel == 'alimtalk' else '이메일'} 발송을 접수했습니다."
                if status == "sent"
                else f"{'카카오 알림톡' if channel == 'alimtalk' else '이메일'} 발송 실패: {error}"
            ),
            "invitation_url": invitation_url,
        }
    )


@verified_contract_bp.route("/admin/<int:contract_id>/revoke", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def revoke_contract(contract_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT status FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "계약을 찾을 수 없습니다."}), 404
        if row["status"] == "completed":
            return jsonify({"status": "error", "message": "완료된 계약은 취소할 수 없습니다."}), 400
        update_verified_contract(conn, contract_id, {"status": "revoked"})
        _record_event(conn, contract_id, "REVOKED")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "message": "계약 링크를 취소했습니다."})


@verified_contract_bp.route("/admin/<int:contract_id>/evidence")
@menu_permission_required("verified_contract_admin")
def evidence(contract_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        events = conn.execute(
            """
            SELECT event_type, event_at, ip_address, user_agent, details_json
            FROM verified_contract_events WHERE contract_id=? ORDER BY id
            """,
            (contract_id,),
        ).fetchall()
    finally:
        conn.close()
    if not row:
        return jsonify({"status": "error", "message": "계약을 찾을 수 없습니다."}), 404
    return jsonify(
        {
            "status": "success",
            "contract": _row_for_view(row),
            "events": [
                {
                    **dict(event),
                    "event_display": _format_kst(event["event_at"]),
                    "details": json.loads(event["details_json"] or "{}"),
                }
                for event in events
            ],
            "agreements": json.loads(row["agreement_snapshot_json"] or "[]"),
        }
    )


@verified_contract_bp.route("/admin/<int:contract_id>/download")
@menu_permission_required("verified_contract_admin")
def admin_download(contract_id: int):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT pdf_filename FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["pdf_filename"]:
        return "완료된 계약서가 없습니다.", 404
    path = VERIFIED_CONTRACTS_ROOT / os.path.basename(row["pdf_filename"])
    if not path.is_file():
        return "계약서 파일을 찾을 수 없습니다.", 404
    return encrypted_response(path, path.name, as_attachment=True, mimetype='application/pdf')


@verified_contract_bp.route("/admin/<int:contract_id>/preview")
@menu_permission_required("verified_contract_admin")
def admin_preview(contract_id: int):
    """계약자에게 실제로 발송되기 전, 등록된 정보로 채워질 계약서를 미리 확인한다."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM verified_contracts WHERE id=?", (contract_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "계약을 찾을 수 없습니다.", 404
    contract_data = json.loads(row["contract_data_json"] or "{}")
    company = json.loads(row["company_snapshot_json"] or "{}")
    values = _public_values(row, contract_data, company)
    values["연락처"] = escape(str(contract_data.get("연락처") or row["signer_phone"] or ""))
    values["거주지"] = escape(str(contract_data.get("거주지") or row["signer_address"] or ""))
    content1 = _render_terms(row["terms1_snapshot"], values)
    content2 = _render_terms(row["terms2_snapshot"], values)
    stamp = _stamp_data_uri(company)
    company_text = " ".join(
        escape(str(company.get(key, "")))
        for key in ("company_name", "representative_title", "representative_name")
        if company.get(key)
    )
    sign_block = f"""
      <div class="sign">
        <p class="sign-date">서명일: (서명 예정)</p>
        <div class="party"><b>[위탁자]</b>
          <p class="party-line">{company_text}{f'<img class="stamp" src="{stamp}">' if stamp else ''}</p>
        </div>
        <div class="party"><b>[수탁자]</b>
          <p class="party-line">성명: {escape(row['signer_name'])}</p>
          <table class="sign-table"><tr>
            <td class="sign-label">서명:</td>
            <td class="sign-cell sign-cell-empty">계약자 서명 예정</td>
          </tr></table>
        </div>
      </div>
    """
    header_block = f"""
      <h1>{escape(row['title_snapshot'])}</h1>
      <table class="info">
        <tr><th>계약자</th><td>{escape(row['signer_name'])}</td><th>주민번호</th><td>(서명 시 입력)</td></tr>
        <tr><th>학교</th><td>{values.get('수탁학교명','')}</td><th>부서</th><td>{values.get('부서명','')}</td></tr>
        <tr><th>연락처</th><td>{values.get('연락처','') or '(서명 시 입력)'}</td><th>이메일</th><td>{escape(row['signer_email'])}</td></tr>
        <tr><th>주소</th><td colspan="3">{values.get('거주지','') or '(서명 시 입력)'}</td></tr>
        <tr><th>은행</th><td>(서명 시 입력)</td><th>계좌번호</th><td>(서명 시 입력)</td></tr>
      </table>
    """
    html = f"""
    <!doctype html><html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <title>계약서 미리보기 - {escape(row['signer_name'])}</title>
    <style>
    *{{box-sizing:border-box}}
    {_preview_font_css()}
    body{{color:#111;font-size:15px;line-height:1.72;word-break:keep-all;font-family:'VerifiedNanum','Malgun Gothic',Arial,sans-serif;background:#5b6b82;margin:0;padding:24px 0}}
    .preview-banner{{max-width:900px;margin:0 auto 16px;padding:12px 18px;background:#fff3cd;border:1px solid #f0c766;border-radius:8px;color:#7a5b00;font-size:.88rem;text-align:center}}
    .sheet{{max-width:900px;margin:0 auto;background:#fff;padding:40px 46px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25)}}
    h1{{font-size:24px;text-align:center;text-decoration:underline;margin:10px 0 28px}}
    .info{{width:100%;border-collapse:collapse;margin-bottom:24px;table-layout:fixed}}
    .info th,.info td{{border-bottom:1px solid #ccc;padding:8px;text-align:left}}
    .info th{{width:110px}}
    .terms table{{width:100%;border-collapse:collapse;margin:12px 0}}
    .terms th,.terms td{{border:1px solid #333;padding:7px}}
    .terms p{{margin:0 0 8px}}
    .terms h1,.terms h2,.terms h3,.terms h4,.terms h5,.terms h6{{font-size:17px;font-weight:700;margin:16px 0 8px}}
    .terms{{margin-bottom:48px}}
    .sign{{margin-top:48px;min-height:250px}}
    .sign-date{{text-align:center;margin:40px 0 124px}}
    .party{{width:44%;display:inline-block;vertical-align:top}}
    .party+.party{{margin-left:8%}}
    .party-line{{margin:8px 0 0}}
    .stamp{{width:76px;vertical-align:middle;margin-left:8px}}
    .sign-table{{border-collapse:collapse;margin-top:6px}}
    .sign-label{{vertical-align:bottom;padding:0 6px 5px 0;white-space:nowrap}}
    .sign-cell{{width:225px;border-bottom:1px solid #222;padding:14px 0 5px}}
    .sign-cell-empty{{color:#aaa;text-align:center;font-size:.82rem}}
    </style></head><body>
      <div class="preview-banner">⚠ 미리보기 화면입니다. 실제 발송·서명되는 계약서가 아니며 법적 효력이 없습니다.</div>
      <div class="sheet">
      {header_block}
      <div class="terms">{content1}</div>
      {sign_block}
      {f'{header_block}<div class="terms">{content2}</div>{sign_block}' if content2.strip() else ''}
      </div>
    </body></html>
    """
    return html


@verified_contract_bp.route("/admin/terms")
@menu_permission_required("verified_contract_admin")
def get_terms():
    contract_type = str(request.args.get("type", "")).strip()
    if contract_type not in _categories():
        return jsonify({"status": "error", "message": "계약구분을 확인해 주세요."}), 400
    content1, content2 = _read_terms(contract_type)
    profile = _company_snapshot()
    title = _titles().get(contract_type) or _default_title(contract_type, profile["company_name"])
    return jsonify(
        {
            "status": "success",
            "content1": content1,
            "content2": content2,
            "title": title,
        }
    )


@verified_contract_bp.route("/admin/terms", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def save_terms():
    data = request.get_json(silent=True) or {}
    contract_type = str(data.get("type", "")).strip()
    if contract_type not in _categories():
        return jsonify({"status": "error", "message": "계약구분을 확인해 주세요."}), 400
    (VERIFIED_TERMS_ROOT / f"{contract_type}.txt").write_text(
        _sanitize_contract_html(data.get("content1", "")), encoding="utf-8"
    )
    (VERIFIED_TERMS_ROOT / f"{contract_type}2.txt").write_text(
        _sanitize_contract_html(data.get("content2", "")), encoding="utf-8"
    )
    titles = _titles()
    title = str(data.get("title", "")).strip()
    if title:
        titles[contract_type] = title
    else:
        titles.pop(contract_type, None)
    _save_json(VERIFIED_TITLES_FILE, titles)
    return jsonify({"status": "success", "message": "인증전자계약 전용 양식을 저장했습니다."})


@verified_contract_bp.route("/admin/categories", methods=["GET", "POST"])
@menu_permission_required("verified_contract_admin")
def add_category():
    if request.method == "GET":
        return jsonify({"status": "success", "categories": _categories()})
    supplied = request.headers.get("X-CSRF-Token") or ""
    expected = session.get("verified_contract_csrf", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        return jsonify({"status": "error", "message": "보안 확인값이 만료되었습니다."}), 403
    data = request.get_json(silent=True) or {}
    categories = _categories()
    if isinstance(data.get("categories"), list):
        requested = []
        for item in data["categories"]:
            name = str(item).strip()
            if (
                not name
                or len(name) > 40
                or any(character in name for character in '\\/:*?"<>|')
            ):
                return jsonify({"status": "error", "message": f"사용할 수 없는 계약구분: {name}"}), 400
            if name not in requested:
                requested.append(name)
        if not requested:
            return jsonify({"status": "error", "message": "계약구분은 한 개 이상 필요합니다."}), 400
        removed = set(categories) - set(requested)
        if removed:
            conn = get_db()
            try:
                used = {
                    row["contract_type"]
                    for row in conn.execute(
                        "SELECT DISTINCT contract_type FROM verified_contracts"
                    ).fetchall()
                }
            finally:
                conn.close()
            blocked = sorted(removed & used)
            if blocked:
                return jsonify(
                    {
                        "status": "error",
                        "message": "계약 기록이 있는 구분은 삭제할 수 없습니다: " + ", ".join(blocked),
                    }
                ), 400
        categories = requested
        for name in categories:
            (VERIFIED_TERMS_ROOT / f"{name}.txt").touch(exist_ok=True)
            (VERIFIED_TERMS_ROOT / f"{name}2.txt").touch(exist_ok=True)
        _save_json(VERIFIED_CATEGORIES_FILE, categories)
        return jsonify({"status": "success", "message": "계약구분을 저장했습니다.", "categories": categories})

    name = str(data.get("name", "")).strip()
    if not name or len(name) > 40 or any(character in name for character in '\\/:*?"<>|'):
        return jsonify({"status": "error", "message": "사용할 수 없는 계약구분 이름입니다."}), 400
    if name not in categories:
        categories.append(name)
        _save_json(VERIFIED_CATEGORIES_FILE, categories)
        (VERIFIED_TERMS_ROOT / f"{name}.txt").touch(exist_ok=True)
        (VERIFIED_TERMS_ROOT / f"{name}2.txt").touch(exist_ok=True)
    return jsonify({"status": "success", "message": "새 계약구분을 추가했습니다.", "categories": categories})


@verified_contract_bp.route("/admin/settings/mail", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def save_mail_settings():
    """발송계정 자체는 스마트명세서와 공유하는 /payroll/api/senders에서 등록·수정·연결테스트하고,
    여기서는 인증전자계약에서 사용할 계정을 선택만 한다."""
    data = request.get_json(silent=True) or {}
    sender_id = str(data.get("sender_id") or data.get("account_id") or "").strip()
    if not sender_id:
        return jsonify({"status": "error", "message": "사용할 발송계정을 선택해 주세요."}), 400
    conn = get_db()
    try:
        _ensure_sender_schema(conn)
        row = conn.execute(
            "SELECT id FROM ai_mail_senders WHERE id=? AND owner_emp_no=? AND is_active=1",
            (sender_id, _owner_emp_no()),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"status": "error", "message": "발송계정을 찾을 수 없습니다."}), 404
    _set_active_sender_id(sender_id)
    return jsonify(
        {
            "status": "success",
            "message": "선택한 계정을 계약 발송계정으로 적용했습니다.",
            "active_sender_id": sender_id,
        }
    )


@verified_contract_bp.route("/admin/settings/company", methods=["POST"])
@menu_permission_required("verified_contract_admin")
@_csrf_required
def save_company_settings():
    settings = _company_settings()
    profile_id = str(request.form.get("profile_id", "")).strip()
    action = str(request.form.get("action", "save")).strip()
    profiles = settings["profiles"]
    if action == "select":
        if not any(item["id"] == profile_id for item in profiles):
            return jsonify({"status": "error", "message": "적용할 발송회사를 찾을 수 없습니다."}), 404
        settings["active_profile_id"] = profile_id
        _save_json(VERIFIED_COMPANY_FILE, settings)
        return jsonify({"status": "success", "message": "선택한 회사를 기본 발송회사로 적용했습니다."})
    if action == "delete":
        if len(profiles) <= 1:
            return jsonify({"status": "error", "message": "발송회사는 최소 1개 이상 필요합니다."}), 400
        profile = next((item for item in profiles if item["id"] == profile_id), None)
        if not profile:
            return jsonify({"status": "error", "message": "삭제할 발송회사를 찾을 수 없습니다."}), 404
        profiles = [item for item in profiles if item["id"] != profile_id]
        if settings["active_profile_id"] == profile_id:
            settings["active_profile_id"] = profiles[0]["id"]
        settings["profiles"] = profiles
        _save_json(VERIFIED_COMPANY_FILE, settings)
        for key, root in (
            ("logo_filename", VERIFIED_LOGO_ROOT),
            ("stamp_filename", VERIFIED_STAMP_ROOT),
        ):
            if profile.get(key):
                delete_file(root / os.path.basename(profile[key]))
        return jsonify({"status": "success", "message": "발송회사를 삭제했습니다."})
    if action == "add":
        if len(profiles) >= MAX_COMPANY_PROFILES:
            return jsonify(
                {
                    "status": "error",
                    "message": f"회사정보는 최대 {MAX_COMPANY_PROFILES}개까지 등록할 수 있습니다.",
                }
            ), 400
        profile_id = f"verified-{secrets.token_hex(8)}"
        profile = {"id": profile_id, "logo_filename": "", "stamp_filename": ""}
        profiles.append(profile)
    else:
        profile = next((item for item in profiles if item["id"] == profile_id), None)
        if not profile:
            return jsonify({"status": "error", "message": "회사정보를 찾을 수 없습니다."}), 404
    profile.update(
        {
            "label": str(request.form.get("label", "")).strip() or "회사정보",
            "company_name": str(request.form.get("company_name", "")).strip(),
            "representative_title": str(request.form.get("representative_title", "")).strip(),
            "representative_name": str(request.form.get("representative_name", "")).strip(),
        }
    )
    for optional_field in ("business_number", "address", "phone"):
        if optional_field in request.form:
            profile[optional_field] = str(request.form.get(optional_field, "")).strip()
    if not profile["company_name"]:
        return jsonify({"status": "error", "message": "회사명을 입력해 주세요."}), 400
    if not profile["representative_name"]:
        return jsonify({"status": "error", "message": "대표자 이름을 입력해 주세요."}), 400

    new_asset_paths = []
    replaced_assets = []
    try:
        for asset_kind, upload_key, root, fallback, label in (
            ("logo", "logo_file", VERIFIED_LOGO_ROOT, "logo.png", "회사 로고"),
            ("stamp", "stamp_file", VERIFIED_STAMP_ROOT, "stamp.png", "회사 도장"),
        ):
            upload = request.files.get(upload_key)
            if not upload or not upload.filename:
                continue
            display_name = original_filename(upload.filename, fallback)
            extension = Path(display_name).suffix.lower()
            if extension not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                raise ValueError(f"{label}는 PNG, JPG, GIF 또는 WEBP 파일만 사용할 수 있습니다.")
            filename = encrypted_storage_name(display_name)
            new_path = root / filename
            old_filename = str(profile.get(f"{asset_kind}_filename") or "")
            encrypt_upload(upload, new_path)
            new_asset_paths.append(new_path)
            if old_filename and old_filename != filename:
                replaced_assets.append((root, old_filename))
            profile[f"{asset_kind}_filename"] = filename
            profile[f"{asset_kind}_original_name"] = display_name
    except ValueError as exc:
        for path in new_asset_paths:
            delete_file(path)
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception:
        for path in new_asset_paths:
            delete_file(path)
        raise
    settings["active_profile_id"] = profile_id
    settings["profiles"] = profiles
    try:
        _save_json(VERIFIED_COMPANY_FILE, settings)
    except Exception:
        for path in new_asset_paths:
            delete_file(path)
        raise
    for root, old_filename in replaced_assets:
        delete_file(root / os.path.basename(old_filename))
    return jsonify({"status": "success", "message": "인증전자계약 전용 회사정보를 저장했습니다."})


@verified_contract_bp.route("/sign/<string:token>")
def public_contract(token: str):
    conn = get_db()
    try:
        row = _load_by_token(conn, token)
        available, message = _contract_available(row)
        if row and not available and row["status"] == "pending":
            update_verified_contract(conn, row["id"], {"status": "expired"})
            _record_event(conn, row, "EXPIRED")
            conn.commit()
        if row and available and not row["opened_at"]:
            update_verified_contract(conn, row["id"], {"opened_at": _iso()})
            _record_event(conn, row, "LINK_OPENED")
            conn.commit()
            row = _load_by_token(conn, token)
    finally:
        conn.close()
    if not available:
        return render_template("verified_contract/public.html", state="error", message=message), 410
    if row["status"] == "completed" and _public_verified(row, token):
        return render_template(
            "verified_contract/public.html",
            state="completed",
            data=_row_for_view(row),
            token=token,
            can_download=_public_verified(row, token),
        )
    if not _public_verified(row, token):
        # 본인 확인은 이메일로만 한다. 계약자가 입력한 주소로 인증번호가 오가야
        # 완료 계약서를 받을 주소가 실제로 도달 가능한 주소임이 확인된다.
        return render_template(
            "verified_contract/public.html",
            state="verify",
            data=_row_for_view(row),
            registered_email=str(row["signer_email"] or "").strip().lower(),
            token=token,
            csrf_token=_csrf_token(),
            otp_valid_seconds=OTP_VALID_SECONDS,
            otp_resend_seconds=OTP_RESEND_COOLDOWN_SECONDS,
        )
    contract_data = json.loads(row["contract_data_json"] or "{}")
    company = json.loads(row["company_snapshot_json"] or "{}")
    values = _public_values(row, contract_data, company)
    return render_template(
        "verified_contract/public.html",
        state="sign",
        data=_row_for_view(row),
        token=token,
        csrf_token=_csrf_token(),
        agreements=json.loads(row["agreement_snapshot_json"] or "[]"),
        content1=_render_terms(row["terms1_snapshot"], values),
        content2=_render_terms(row["terms2_snapshot"], values),
        contract_values=contract_data,
        company=company,
    )


@verified_contract_bp.route("/sign/<string:token>/send-code", methods=["POST"])
@_csrf_required
def send_otp(token: str):
    # 계약 등록 때 휴대폰번호만 아는 경우가 많아, 계약자가 이 화면에서 최종 계약서를 받을
    # 이메일 주소를 직접 입력하고 그 주소로만 인증번호를 받는다. 이 인증을 통과해야
    # 계약서를 열 수 있으므로, 주소를 잘못 적으면 계약이 진행되지 않아 완료 계약서를
    # 못 받는 상황이 생기지 않는다. 입력한 주소는 인증에 성공한 뒤에야 계약에 반영한다.
    entered_email = _normalized_email((request.get_json(silent=True) or {}).get("email"))
    if not entered_email:
        return jsonify({"status": "error", "message": "이메일 주소를 정확히 입력해 주세요."}), 400
    conn = get_db()
    try:
        row = _load_by_token(conn, token)
        available, message = _contract_available(row)
        if not available or row["status"] not in {"pending", "completed"}:
            return jsonify({"status": "error", "message": message or "인증할 수 없는 계약입니다."}), 410
        sent_at = _parse_iso(row["otp_sent_at"])
        if sent_at and (_now() - sent_at).total_seconds() < OTP_RESEND_COOLDOWN_SECONDS:
            return jsonify({
                "status": "error",
                "message": f"인증번호는 {OTP_RESEND_COOLDOWN_SECONDS}초 후 다시 요청할 수 있습니다.",
            }), 429
        masked_recipient = _mask_email(entered_email)
        code = f"{secrets.randbelow(900000) + 100000:06d}"
        update_verified_contract(
            conn,
            row["id"],
            {
                "otp_hash": generate_password_hash(code),
                "otp_expires_at": _iso(_now() + timedelta(minutes=OTP_VALID_MINUTES)),
                "otp_attempts": 0,
                "otp_sent_at": _iso(),
            },
        )
        conn.commit()
    finally:
        conn.close()
    message_id = ""
    try:
        _send_mail(
            entered_email,
            "[새담 인증전자계약] 이메일 인증번호",
            f"""
            <div style="font-family:Arial,'Malgun Gothic',sans-serif;line-height:1.7">
              <h2>이메일 인증번호</h2>
              <p>{escape(row['signer_name'])}님의 인증번호는 다음과 같습니다.</p>
              <div style="font-size:30px;font-weight:bold;letter-spacing:8px;color:#123b6d">{code}</div>
              <p>{OTP_VALID_MINUTES}분 안에 계약 화면에 입력해 주세요. 타인에게 알려주지 마세요.</p>
              <p>계약을 마치면 서명된 계약서 원본도 이 주소로 발송됩니다.</p>
            </div>
            """,
        )
    except Exception as exc:
        conn = get_db()
        try:
            update_verified_contract(
                conn,
                row["id"],
                {"otp_hash": None, "otp_expires_at": None, "otp_sent_at": None},
            )
            _record_event(
                conn,
                row,
                "OTP_SEND_FAILED",
                {"channel": "email", "recipient": masked_recipient, "error": str(exc)[:200]},
            )
            conn.commit()
        finally:
            conn.close()
        return jsonify({"status": "error", "message": f"인증번호 발송 실패: {str(exc)[:200]}"}), 500
    conn = get_db()
    try:
        update_verified_contract(
            conn,
            row["id"],
            {"otp_channel": "email", "otp_message_id": message_id},
        )
        _record_event(
            conn,
            row,
            "OTP_SENT",
            {"channel": "email", "recipient": masked_recipient, "message_id": message_id},
        )
        conn.commit()
    finally:
        conn.close()
    pending = session.get("verified_contract_email")
    if not isinstance(pending, dict):
        pending = {}
    pending[str(row["id"])] = entered_email
    session["verified_contract_email"] = pending
    return jsonify({
        "status": "success",
        "message": f"{masked_recipient}로 인증번호를 보냈습니다. {OTP_VALID_MINUTES}분 안에 입력해 주세요.",
        "valid_seconds": OTP_VALID_SECONDS,
    })


@verified_contract_bp.route("/sign/<string:token>/verify-code", methods=["POST"])
@_csrf_required
def verify_otp(token: str):
    code = str((request.get_json(silent=True) or {}).get("code", "")).strip()
    conn = get_db()
    try:
        row = _load_by_token(conn, token)
        available, message = _contract_available(row)
        if not available or row["status"] not in {"pending", "completed"}:
            return jsonify({"status": "error", "message": message or "인증할 수 없는 계약입니다."}), 410
        expiry = _parse_iso(row["otp_expires_at"])
        if not row["otp_hash"] or not expiry or expiry < _now():
            return jsonify({"status": "error", "message": f"인증번호가 만료되었습니다({OTP_VALID_MINUTES}분 경과). 새로 받아주세요."}), 400
        attempts = int(row["otp_attempts"] or 0)
        if attempts >= 5:
            return jsonify({"status": "error", "message": "입력 횟수를 초과했습니다. 인증번호를 새로 받아주세요."}), 429
        if not re.fullmatch(r"\d{6}", code) or not check_password_hash(row["otp_hash"], code):
            update_verified_contract(conn, row["id"], {"otp_attempts": attempts + 1})
            _record_event(conn, row, "OTP_FAILED", {"attempt": attempts + 1})
            conn.commit()
            return jsonify({"status": "error", "message": "인증번호가 일치하지 않습니다."}), 400
        verified_at = _iso()
        updates = {"verified_at": verified_at, "otp_hash": None, "otp_expires_at": None}
        # 인증번호를 받은 주소가 곧 최종 계약서를 받을 주소다. 인증에 성공한 지금
        # 계약에 반영해야 완료 메일이 그 주소로 나간다.
        pending = session.get("verified_contract_email")
        entered_email = _normalized_email(
            pending.get(str(row["id"])) if isinstance(pending, dict) else ""
        )
        if entered_email and entered_email != str(row["signer_email"] or "").strip().lower():
            updates["signer_email"] = entered_email
        update_verified_contract(conn, row["id"], updates)
        _record_event(conn, row, "OTP_VERIFIED")
        if updates.get("signer_email"):
            _record_event(
                conn, row, "SIGNER_EMAIL_UPDATED", {"email": _mask_email(entered_email)}
            )
        conn.commit()
    finally:
        conn.close()
    if isinstance(pending, dict) and pending.pop(str(row["id"]), None):
        session["verified_contract_email"] = pending
    access = session.get("verified_contract_access", {})
    if not isinstance(access, dict):
        access = {}
    access[str(row["id"])] = _token_hash(token)[:24]
    session["verified_contract_access"] = access
    return jsonify({"status": "success", "message": "본인 인증이 완료되었습니다."})


def _public_values(row, contract_data: dict, company: dict) -> dict[str, str]:
    values = {
        key: escape(str(value or ""))
        for key, value in contract_data.items()
    }
    values.update(
        {
            "성명": escape(row["signer_name"]),
            "email": escape(row["signer_email"]),
            "회사명": escape(company.get("company_name", "")),
            "대표직함": escape(company.get("representative_title", "")),
            "대표자명": escape(company.get("representative_name", "")),
            "계약서제목": escape(row["title_snapshot"]),
        }
    )
    return values


def _decode_signature(data_url: str) -> bytes:
    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=\r\n]+)", str(data_url or ""))
    if not match:
        raise ValueError("서명 형식이 올바르지 않습니다.")
    raw = base64.b64decode(match.group(1), validate=True)
    if not 200 <= len(raw) <= 1_500_000:
        raise ValueError("서명 데이터 크기가 올바르지 않습니다.")
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        if image.width > 2400 or image.height > 1200:
            raise ValueError("서명 이미지가 너무 큽니다.")
        alpha = image.getchannel("A")
        if alpha.getbbox() is None:
            raise ValueError("서명란이 비어 있습니다.")
        colors = image.getcolors(maxcolors=image.width * image.height)
        if colors is not None and len(colors) < 2:
            raise ValueError("서명란이 비어 있습니다.")
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("서명 이미지를 확인할 수 없습니다.") from exc
    return raw


_PDF_FONT_CSS = None
# 미리보기 화면과 PDF 가 같은 글꼴로 렌더링되어야 줄바꿈과 글자 크기가 일치한다.
PDF_FONT_FILES = {
    "regular": (
        "NanumGothic-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/nanumgothic/NanumGothic-Regular.ttf",
    ),
    "bold": (
        "NanumGothic-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/nanumgothic/NanumGothic-Bold.ttf",
    ),
}


def _pdf_font_path(weight: str):
    """계약서 전용 글꼴을 확보한다. 내려받지 못하면 None 을 돌려준다."""
    entry = PDF_FONT_FILES.get(weight)
    if not entry:
        return None
    filename, url = entry
    path = VERIFIED_PDF_FONT_ROOT / filename
    if not path.is_file():
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as response:
                content = response.read()
            if len(content) > 100_000:
                path.write_bytes(content)
        except Exception:
            pass
    return path if path.is_file() else None


def _pdf_font_css() -> str:
    global _PDF_FONT_CSS
    if _PDF_FONT_CSS is not None:
        return _PDF_FONT_CSS
    encoded = {}
    for weight in PDF_FONT_FILES:
        path = _pdf_font_path(weight)
        if path:
            encoded[weight] = base64.b64encode(path.read_bytes()).decode("ascii")
    rules = []
    if encoded.get("regular"):
        rules.append(
            "@font-face{font-family:'VerifiedNanum';font-weight:400;"
            f"src:url(data:font/ttf;base64,{encoded['regular']}) format('truetype');}}"
        )
    if encoded.get("bold"):
        rules.append(
            "@font-face{font-family:'VerifiedNanum';font-weight:700;"
            f"src:url(data:font/ttf;base64,{encoded['bold']}) format('truetype');}}"
        )
    rules.append("html,body,body *{font-family:'VerifiedNanum','Malgun Gothic',sans-serif!important;}")
    _PDF_FONT_CSS = "\n".join(rules)
    return _PDF_FONT_CSS


def _preview_font_css() -> str:
    """미리보기 화면이 PDF 와 같은 글꼴을 쓰도록 @font-face 를 심는다."""
    rules = []
    for weight, css_weight in (("regular", 400), ("bold", 700)):
        if not _pdf_font_path(weight):
            continue
        url = url_for("verified_contract.pdf_font_file", weight=weight)
        rules.append(
            f"@font-face{{font-family:'VerifiedNanum';font-weight:{css_weight};"
            f"font-display:swap;src:url('{url}') format('truetype');}}"
        )
    return "\n".join(rules)


@verified_contract_bp.route("/admin/pdf-font/<string:weight>")
@menu_permission_required("verified_contract_admin")
def pdf_font_file(weight: str):
    path = _pdf_font_path(weight)
    if not path:
        return "글꼴을 찾을 수 없습니다.", 404
    response = send_file(path, mimetype="font/ttf", conditional=True)
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


def _pdf_configuration():
    candidates = [
        os.environ.get("VERIFIED_WKHTMLTOPDF_PATH", ""),
        "/usr/bin/wkhtmltopdf",
        "/usr/local/bin/wkhtmltopdf",
        "/opt/render/project/src/.apt/usr/bin/wkhtmltopdf",
        r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
        r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
        shutil.which("wkhtmltopdf") or "",
    ]
    path = next((candidate for candidate in candidates if candidate and os.path.exists(candidate)), None)
    return pdfkit.configuration(wkhtmltopdf=path) if path else None


_LOGO_DATA_URI = None


def _logo_data_uri() -> str:
    """계약서에 넣는 새담 로고. 외부 주소 대신 앱에 포함된 파일을 쓴다."""
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        path = APP_ROOT / "static" / "logo01.gif"
        if path.is_file():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            _LOGO_DATA_URI = f"data:image/gif;base64,{encoded}"
        else:
            _LOGO_DATA_URI = f"{CONTRACT_PUBLIC_ORIGIN}/static/logo01.gif"
    return _LOGO_DATA_URI


def _stamp_data_uri(company: dict) -> str:
    filename = os.path.basename(str(company.get("stamp_filename", "")))
    path = VERIFIED_STAMP_ROOT / filename if filename else VERIFIED_STAMP_ROOT / "verified_default_stamp.png"
    if not path.is_file():
        return ""
    mime, _ = mimetypes.guess_type(path.name)
    return f"data:{mime or 'image/png'};base64,{base64.b64encode(read_decrypted(path)).decode('ascii')}"


def _build_pdf(row, contract_data: dict, company: dict, signature_uri: str, signed_at: datetime):
    values = _public_values(row, contract_data, company)
    values["연락처"] = escape(str(contract_data.get("연락처", "")))
    values["거주지"] = escape(str(contract_data.get("거주지", "")))
    content1 = _render_terms(row["terms1_snapshot"], values)
    content2 = _render_terms(row["terms2_snapshot"], values)
    agreements = json.loads(row["agreement_snapshot_json"] or "[]")
    agreement_html = "".join(
        f"<li><b>{escape(item['title'])}</b>: {escape(item['text'])}</li>"
        for item in agreements
    )
    stamp = _stamp_data_uri(company)
    company_text = " ".join(
        escape(str(company.get(key, "")))
        for key in ("company_name", "representative_title", "representative_name")
        if company.get(key)
    )
    sign_block = f"""
      <div class="sign">
        <p class="sign-date">{signed_at.astimezone(KST).strftime('%Y년 %m월 %d일')}</p>
        <div class="party"><b>[위탁자]</b>
          <p class="party-line">{company_text}{f'<img class="stamp" src="{stamp}">' if stamp else ''}</p>
        </div>
        <div class="party"><b>[수탁자]</b>
          <p class="party-line">성명: {escape(row['signer_name'])}</p>
          <table class="sign-table"><tr>
            <td class="sign-label">서명:</td>
            <td class="sign-cell"><img class="sign-img" src="{signature_uri}"></td>
          </tr></table>
        </div>
      </div>
    """
    header_block = f"""
      <h1>{escape(row['title_snapshot'])}</h1>
      <table class="info">
        <tr><th>계약자</th><td>{escape(row['signer_name'])}</td><th>주민번호</th><td>{values.get('주민번호','')}</td></tr>
        <tr><th>학교</th><td>{values.get('수탁학교명','')}</td><th>부서</th><td>{values.get('부서명','')}</td></tr>
        <tr><th>연락처</th><td>{values.get('연락처','')}</td><th>이메일</th><td>{escape(row['signer_email'])}</td></tr>
        <tr><th>주소</th><td colspan="3">{values.get('거주지','')}</td></tr>
        <tr><th>은행</th><td>{values.get('은행','')}</td><th>계좌번호</th><td>{values.get('계좌번호','')}</td></tr>
      </table>
    """
    html = f"""
    <!doctype html><html><head><meta charset="utf-8"><style>
    {_pdf_font_css()}
    body{{color:#111;font-size:16px;line-height:1.72;word-break:keep-all}}
    h1{{font-size:24px;text-align:center;text-decoration:underline;margin:10px 0 28px}}
    .info{{width:100%;border-collapse:collapse;margin-bottom:24px;table-layout:fixed}}
    .info th,.info td{{border-bottom:1px solid #ccc;padding:8px;text-align:left}}
    .info th{{width:110px}}
    .terms table{{width:100%;border-collapse:collapse;margin:12px 0}}
    .terms th,.terms td{{border:1px solid #333;padding:7px}}
    .terms p{{margin:0 0 8px}}
    .terms h1,.terms h2,.terms h3,.terms h4,.terms h5,.terms h6{{font-size:17px;font-weight:700;margin:16px 0 8px}}
    .terms{{margin-bottom:48px}}
    .sign{{margin-top:48px;min-height:250px;page-break-inside:avoid}}
    .sign-date{{text-align:center;margin:40px 0 124px}}
    .party{{width:44%;display:inline-block;vertical-align:top}}
    .party+.party{{margin-left:8%}}
    .party-line{{margin:8px 0 0}}
    .stamp{{width:76px;vertical-align:middle;margin-left:8px}}
    .sign-table{{border-collapse:collapse;margin-top:6px}}
    .sign-label{{vertical-align:bottom;padding:0 6px 5px 0;white-space:nowrap}}
    .sign-cell{{width:225px;border-bottom:1px solid #222;padding:14px 0 5px}}
    .sign-img{{display:block;width:225px;height:auto;max-height:100px}}
    .evidence{{border:1px solid #9fb3c8;background:#f5f8fb;padding:14px;margin-top:25px;font-size:12px;page-break-inside:avoid}}
    .evidence li{{margin:5px 0}}
    .footer-logo{{text-align:center;margin-top:34px;page-break-inside:avoid}}
    .footer-logo img{{width:112px;height:auto}}
    </style></head><body>
      {header_block}
      <div class="terms">{content1}</div>
      {sign_block}
      {f'<div style="page-break-before:always"></div>{header_block}<div class="terms">{content2}</div>{sign_block}' if content2.strip() else ''}
      <div class="evidence"><b>전자계약 확인기록</b><ul>{agreement_html}</ul>
        <p>본인 인증 완료: {escape(_format_kst(row['verified_at']))}<br>
        전자서명 완료: {signed_at.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S KST')}</p>
      </div>
      <div class="footer-logo"><img src="{_logo_data_uri()}" alt="새담청소년교육문화원"></div>
    </body></html>
    """
    configuration = _pdf_configuration()
    if not configuration:
        raise RuntimeError("인증전자계약 전용 PDF 엔진(wkhtmltopdf)을 찾을 수 없습니다.")
    filename = f"verified_contract_{row['id']}_v{row['version']}_{signed_at.astimezone(KST).strftime('%Y%m%d_%H%M%S')}.pdf"
    path = VERIFIED_CONTRACTS_ROOT / filename
    descriptor, temporary_path = tempfile.mkstemp(prefix='saedam-verified-', suffix='.pdf')
    os.close(descriptor)
    try:
        pdfkit.from_string(
            html,
            temporary_path,
            configuration=configuration,
            options={
                "encoding": "UTF-8",
                "enable-local-file-access": None,
                "print-media-type": None,
                "page-size": "A4",
                # zoom 은 지정하지 않는다. 기본값(1.0)에서 wkhtmltopdf 가 CSS px 를
                # 미리보기 화면과 동일한 크기로 렌더링한다(본문 15px, 제목 24px).
                "margin-top": "18mm",
                "margin-right": "17mm",
                "margin-bottom": "18mm",
                "margin-left": "17mm",
            },
        )
        with open(temporary_path, 'rb') as source:
            pdf_hash = hashlib.sha256(source.read()).hexdigest()
            source.seek(0)
            encrypt_stream(source, path)
        return path, pdf_hash, temporary_path
    except Exception:
        delete_file(temporary_path)
        delete_file(path)
        raise


@verified_contract_bp.route("/sign/<string:token>/complete", methods=["POST"])
@_csrf_required
def complete_contract(token: str):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        row = _load_by_token(conn, token)
    finally:
        conn.close()
    available, message = _contract_available(row)
    if not available or row["status"] != "pending":
        return jsonify({"status": "error", "message": message or "완료할 수 없는 계약입니다."}), 410
    if not _public_verified(row, token):
        return jsonify({"status": "error", "message": "본인 인증을 먼저 완료해 주세요."}), 403

    agreement_keys = {item["key"] for item in json.loads(row["agreement_snapshot_json"] or "[]")}
    accepted = {
        str(key)
        for key, value in (data.get("agreements") or {}).items()
        if value is True
    }
    if agreement_keys != accepted:
        return jsonify({"status": "error", "message": "모든 확인사항에 동의해 주세요."}), 400
    confirmed_name = re.sub(r"\s+", "", str(data.get("confirmed_name", "")))
    expected_name = re.sub(r"\s+", "", str(row["signer_name"]))
    if not confirmed_name or not hmac.compare_digest(
        confirmed_name.encode("utf-8"),
        expected_name.encode("utf-8"),
    ):
        return jsonify({"status": "error", "message": "직접 입력한 성명이 계약자 성명과 일치하지 않습니다."}), 400
    try:
        phone = normalize_phone(data.get("phone"), required=True)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    registered_phone = normalize_phone(row["signer_phone"])
    if registered_phone and not hmac.compare_digest(registered_phone, phone):
        return jsonify(
            {"status": "error", "message": "계약 링크를 받은 휴대폰번호와 입력한 연락처가 일치하지 않습니다."}
        ), 400
    address = str(data.get("address", "")).strip()
    if not address or len(address) > 300:
        return jsonify({"status": "error", "message": "연락처와 주소를 정확히 입력해 주세요."}), 400
    try:
        resident_number = _normalize_rrn(data.get("resident_number"))
        bank_name = str(data.get("bank_name", "")).strip()
        if not bank_name or len(bank_name) > 50:
            raise ValueError("은행명을 정확히 입력해 주세요.")
        account_number = _normalize_account(data.get("account_number"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    try:
        signature_bytes = _decode_signature(str(data.get("signature", "")))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    signature_filename = f"signature_{row['id']}_{secrets.token_hex(8)}.png"
    signature_path = VERIFIED_SIGNATURE_ROOT / signature_filename
    encrypt_bytes(signature_bytes, signature_path)
    signature_uri = f"data:image/png;base64,{base64.b64encode(signature_bytes).decode('ascii')}"
    contract_data = json.loads(row["contract_data_json"] or "{}")
    contract_data["연락처"] = phone
    contract_data["거주지"] = address
    pdf_contract_data = {
        **contract_data,
        "주민번호": resident_number,
        "은행": bank_name,
        "계좌번호": account_number,
    }
    company = json.loads(row["company_snapshot_json"] or "{}")
    signed_at = _now()
    try:
        pdf_path, pdf_hash, temporary_pdf_path = _build_pdf(
            row, pdf_contract_data, company, signature_uri, signed_at
        )
    except Exception:
        delete_file(signature_path)
        raise

    agreement_evidence = [
        {**item, "accepted": True, "accepted_at": _iso(signed_at)}
        for item in json.loads(row["agreement_snapshot_json"] or "[]")
    ]
    conn = get_db()
    try:
        current = conn.execute(
            "SELECT status FROM verified_contracts WHERE id=?", (row["id"],)
        ).fetchone()
        if not current or current["status"] != "pending":
            delete_file(pdf_path)
            delete_file(signature_path)
            delete_file(temporary_pdf_path)
            return jsonify({"status": "error", "message": "이미 처리된 계약입니다."}), 409
        update_verified_contract(
            conn,
            row["id"],
            {
                "status": "completed",
                "signer_phone": phone,
                "signer_address": address,
                "signer_rrn_encrypted": _encrypt_sensitive(resident_number),
                "signer_bank_encrypted": _encrypt_sensitive(bank_name),
                "signer_account_encrypted": _encrypt_sensitive(account_number),
                "contract_data_json": json.dumps(contract_data, ensure_ascii=False),
                "agreement_snapshot_json": json.dumps(agreement_evidence, ensure_ascii=False),
                "confirmed_name": str(data.get("confirmed_name", "")).strip(),
                "signature_filename": signature_filename,
                "signed_at": _iso(signed_at),
                "ip_address": _client_ip(),
                "user_agent": _user_agent(),
                "pdf_filename": pdf_path.name,
                "pdf_sha256": pdf_hash,
                "completion_mail_status": "waiting",
            },
        )
        _record_event(
            conn,
            row,
            "COMPLETED",
            {
                "pdf_sha256": pdf_hash,
                "version": int(row["version"]),
                "agreement_keys": sorted(agreement_keys),
                "confirmed_name": str(data.get("confirmed_name", "")).strip(),
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        delete_file(pdf_path)
        delete_file(signature_path)
        delete_file(temporary_pdf_path)
        raise
    finally:
        conn.close()

    signer_email = str(row["signer_email"] or "").strip().lower()
    try:
        sender_conn = get_db()
        try:
            active_sender_row = _active_sender_row(sender_conn)
        finally:
            sender_conn.close()
        sender = str(active_sender_row["email"] or "").strip() if active_sender_row else ""
    except Exception:
        sender = ""
    recipients = [signer_email] if signer_email else []
    if sender and sender.lower() not in recipients:
        recipients.append(sender.lower())
    if not recipients:
        mail_status, mail_error = "not_applicable", ""
    else:
        try:
            with temporary_decrypted_path(pdf_path, pdf_path.name) as mail_pdf_path:
                _send_mail(
                    recipients,
                    f"[계약완료] {row['title_snapshot']}",
                    _completion_mail_html(row, pdf_hash, company.get("company_name")),
                    attachments=mail_pdf_path,
                )
            mail_status, mail_error = "sent", ""
        except Exception as exc:
            mail_status, mail_error = "failed", str(exc)[:500]
    conn = get_db()
    try:
        update_verified_contract(
            conn,
            row["id"],
            {
                "completion_mail_status": mail_status,
                "completion_mail_error": mail_error,
            },
        )
        _record_event(
            conn,
            row,
            (
                "COMPLETION_MAIL_SENT"
                if mail_status == "sent"
                else "COMPLETION_MAIL_SKIPPED"
                if mail_status == "not_applicable"
                else "COMPLETION_MAIL_FAILED"
            ),
            {"error": mail_error},
        )
        conn.commit()
    finally:
        conn.close()
        delete_file(temporary_pdf_path)
    return jsonify(
        {
            "status": "success",
            "message": (
                "계약이 완료되었고 최종본을 이메일로 발송했습니다."
                if mail_status == "sent" and signer_email
                else "계약이 완료되었습니다. 화면에서 최종 계약서를 내려받아 보관해 주세요."
                if mail_status == "not_applicable" or not signer_email
                else "계약은 완료되었지만 최종본 이메일 발송에 실패했습니다. 화면에서 내려받을 수 있습니다."
            ),
        }
    )


@verified_contract_bp.route("/sign/<string:token>/download")
def public_download(token: str):
    conn = get_db()
    try:
        row = _load_by_token(conn, token)
    finally:
        conn.close()
    if not row or row["status"] != "completed" or not _public_verified(row, token):
        return "이메일 인증을 완료한 계약자만 내려받을 수 있습니다.", 403
    path = VERIFIED_CONTRACTS_ROOT / os.path.basename(row["pdf_filename"] or "")
    if not path.is_file():
        return "계약서 파일을 찾을 수 없습니다.", 404
    return encrypted_response(path, path.name, as_attachment=True, mimetype='application/pdf')
