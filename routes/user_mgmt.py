from flask import Blueprint, render_template, request, jsonify, url_for, session, redirect, abort
from routes.db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd
import base64
import smtplib
import os
import re
import hashlib
import secrets
from datetime import datetime
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .database import get_db
from .security import admin_required, hash_password, is_admin_session, menu_permission_required
from .storage import DATA_ROOT, PROFILE_ROOT as _PROFILE_ROOT
from .secure_files import delete_file, encrypted_response, encrypted_storage_name, encrypt_upload, original_filename
from .organization import (
    DEPARTMENT_OPTIONS,
    classify_organization_group,
    normalize_department,
)
from .points import ensure_point_schema
from .payroll import (
    _ensure_sender_schema,
    _payroll_sender_dict,
    _sender_from_header,
    _smtp_login_for_sender,
    _verify_smtp_sender,
)

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# =====================================================================
# [사진 저장 경로 설정 복구]
# 윈도우는 현재폴더/id, 렌더 서버는 /mnt/data/id 에 영구 저장합니다.
# =====================================================================
BASE_DIR = str(DATA_ROOT)
PROFILE_ROOT = str(_PROFILE_ROOT)


def _profile_disk_path(profile_path):
    filename = os.path.basename(str(profile_path or '').replace('\\', '/'))
    return os.path.join(PROFILE_ROOT, filename) if filename else ''
# =====================================================================

LEVEL_MAP = {
    "최고관리자": 0, "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "계약직": 6, "센터장(팀장)": 7, "센터장": 8, "전담코디": 9, "보조코디": 10, "안전코디": 11,
    "방과후강사": 12, "맞춤형강사": 13, "임시회원": 14
}

GROUP_CODE_MAP = {
    "최고관리자": 0, "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "계약직": 6, "센터장(팀장)": 7, "센터장": 8, "전담코디": 9, "보조코디": 10, "안전코디": 11,
    "방과후강사": 12, "맞춤형강사": 13, "임시회원": 14
}

DEFAULT_POSITIONS = tuple(LEVEL_MAP.items())

INVITE_MAIL_SUBJECT_KEY = 'user_invite_mail_subject'
INVITE_MAIL_BODY_KEY = 'user_invite_mail_body'
INVITE_SENDER_ID_KEY = 'user_invite_sender_id'
INTRANET_DIRECT_URL = 'https://works.saedam.org'
INVITE_ACCESS_LINE = f'가입 승인 후 인트라넷 주소 {INTRANET_DIRECT_URL} 로 접속할 수 있습니다.'
# 예전 기본 문구. 이미 저장해 둔 초대 메일 내용에 남아 있으면 불러올 때 새 안내로 바꾼다.
LEGACY_INVITE_ACCESS_LINE = '가입 승인 후 새담 홈페이지 www.saedam.org를 통해 인트라넷에 접속할 수 있습니다.'
DEFAULT_INVITE_MAIL_SUBJECT = '[새담 인트라넷] 회원 가입 초대장'
DEFAULT_INVITE_MAIL_BODY = (
    '안녕하세요. (사)새담청소년교육문화원입니다.\n'
    '새담 인트라넷 가입 신청을 위한 초대 메일입니다.\n'
    '가입 신청하기 버튼을 눌러 본인 정보를 입력해 주세요.\n'
    f'{INVITE_ACCESS_LINE}'
)
PRIVACY_SECURITY_CONSENT_VERSION = '2026-08-10-v1'
PASSWORD_MAX_LENGTH = 12
PASSWORD_ALLOWED_RE = re.compile(
    r'^[A-Za-z0-9!@#$%^&*()_+~`\-={}\[\]:;"\'<>,.?/|\\]{1,12}$'
)


def _ensure_hr_schema(conn):
    """기존 DB에서도 인사관리 설정을 즉시 사용할 수 있도록 보강한다."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS hr_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 99),
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if 'custom_department' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN custom_department TEXT DEFAULT ''")
    if 'custom_team' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN custom_team TEXT DEFAULT ''")
    if 'privacy_security_consent' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN privacy_security_consent INTEGER NOT NULL DEFAULT 0")
    if 'privacy_security_consent_at' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN privacy_security_consent_at DATETIME")
    if 'privacy_security_consent_version' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN privacy_security_consent_version TEXT DEFAULT ''")
    if 'applied_at' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN applied_at DATETIME")
    if 'approved_at' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN approved_at DATETIME")
    if 'rejection_reason' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN rejection_reason TEXT DEFAULT ''")
    if 'rejected_at' not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN rejected_at DATETIME")
    conn.execute('''
        UPDATE users
        SET applied_at = CASE
            WHEN TRIM(COALESCE(join_date, '')) <> '' THEN join_date || ' 00:00:00'
            ELSE DATETIME('now', 'localtime')
        END
        WHERE applied_at IS NULL OR TRIM(applied_at) = ''
    ''')
    conn.execute('''
        UPDATE users
        SET approved_at = join_date || ' 00:00:00'
        WHERE status = '승인'
          AND TRIM(COALESCE(join_date, '')) <> ''
          AND (approved_at IS NULL OR TRIM(approved_at) = '')
    ''')
    conn.executemany('''
        INSERT OR IGNORE INTO hr_positions (name, level, sort_order)
        VALUES (?, ?, ?)
    ''', ((name, level, order) for order, (name, level) in enumerate(DEFAULT_POSITIONS, 1)))
    conn.commit()


def _position_rows(conn):
    _ensure_hr_schema(conn)
    return conn.execute('''
        SELECT id, name, level, sort_order,
               (SELECT COUNT(*) FROM users WHERE position = hr_positions.name) AS user_count
        FROM hr_positions
        ORDER BY level ASC, sort_order ASC, name ASC
    ''').fetchall()


def _position_level(conn, position, default=14):
    _ensure_hr_schema(conn)
    row = conn.execute("SELECT level FROM hr_positions WHERE name = ?", (str(position or '').strip(),)).fetchone()
    return int(row['level']) if row else default

def generate_sd_emp_no(conn, position):
    # 정수로 변경된 GROUP_CODE_MAP에 맞춰서 두 자리 문자열로 자동 변환 (예: 5 -> "05")
    # 등록되지 않은 직급일 경우 기본값을 14(임시회원)로 처리합니다.
    group_code = _position_level(conn, position, GROUP_CODE_MAP.get(position, 14))
    prefix = f"sd{int(group_code):02d}"
    
    row = conn.execute("SELECT emp_no FROM users WHERE emp_no LIKE ? ORDER BY emp_no DESC LIMIT 1", (f"{prefix}%",)).fetchone()
    if not row or not row['emp_no']: return f"{prefix}001"
    last_no_str = row['emp_no'][-3:]
    next_no = int(last_no_str) + 1
    return f"{prefix}{next_no:03d}"

def _ensure_admin_settings(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def _ensure_user_invite_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            sent_at DATETIME,
            used_at DATETIME,
            used_user_id INTEGER
        )
    ''')
    columns = {
        row['name'] if hasattr(row, 'keys') else row[1]
        for row in conn.execute('PRAGMA table_info(user_invites)').fetchall()
    }
    additions = {
        'sender_id': 'INTEGER',
        'sender_email': "TEXT NOT NULL DEFAULT ''",
        'sender_provider': "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE user_invites ADD COLUMN {name} {ddl}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_user_invites_email ON user_invites(email)')


def _invite_token_hash(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _normalize_email(value):
    return str(value or '').strip().lower()


def _invite_sender_setting_key(owner_emp_no):
    """관리자별 마지막 선택 발송계정 설정 키를 반환한다."""
    owner = str(owner_emp_no or '').strip().lower()
    return f'{INVITE_SENDER_ID_KEY}:{owner}'


def _is_valid_email(value):
    return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', str(value or '').strip()))


def _is_valid_signup_password(value):
    password = str(value or '')
    return bool(
        PASSWORD_ALLOWED_RE.fullmatch(password)
        and re.search(r'[A-Za-z]', password)
        and re.search(r'\d', password)
        and re.search(r'[^A-Za-z0-9]', password)
    )


def _normalize_rrn(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return digits


def _is_valid_rrn(value):
    digits = _normalize_rrn(value)
    if len(digits) != 13 or digits[6] not in '123490':
        return False

    century = {'1': 1900, '2': 1900, '3': 2000, '4': 2000, '9': 1800, '0': 1800}[digits[6]]
    try:
        datetime.strptime(f'{century + int(digits[:2]):04d}{digits[2:6]}', '%Y%m%d')
    except ValueError:
        return False

    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    checksum = (11 - sum(int(number) * weight for number, weight in zip(digits[:12], weights)) % 11) % 10
    return checksum == int(digits[-1])


def _format_rrn(value):
    digits = _normalize_rrn(value)
    return f'{digits[:6]}-{digits[6:]}'


def _duplicate_rrn_user(conn, rrn_digits, exclude_user_id=None):
    if not rrn_digits:
        return None
    query = 'SELECT id, name, status, rrn FROM users'
    params = []
    if exclude_user_id is not None:
        query += ' WHERE id != ?'
        params.append(int(exclude_user_id))
    for row in conn.execute(query, params).fetchall():
        if _normalize_rrn(row['rrn']) == rrn_digits:
            return row
    return None


def _normalize_invite_mail_template(subject, body):
    clean_subject = re.sub(r'[\r\n]+', ' ', str(subject or '')).strip()
    clean_body = str(body or '').strip()
    if not clean_subject:
        raise ValueError('메일 제목을 입력해주세요.')
    if not clean_body:
        raise ValueError('메일 내용을 입력해주세요.')
    if len(clean_subject) > 200:
        raise ValueError('메일 제목은 200자 이내로 입력해주세요.')
    if len(clean_body) > 5000:
        raise ValueError('메일 내용은 5,000자 이내로 입력해주세요.')
    return clean_subject, clean_body


def _load_invite_mail_template(conn):
    _ensure_admin_settings(conn)
    rows = conn.execute(
        'SELECT key, value FROM admin_settings WHERE key IN (?, ?)',
        (INVITE_MAIL_SUBJECT_KEY, INVITE_MAIL_BODY_KEY),
    ).fetchall()
    settings = {row['key']: row['value'] for row in rows}
    body = settings.get(INVITE_MAIL_BODY_KEY) or DEFAULT_INVITE_MAIL_BODY
    return {
        'subject': settings.get(INVITE_MAIL_SUBJECT_KEY) or DEFAULT_INVITE_MAIL_SUBJECT,
        'body': body.replace(LEGACY_INVITE_ACCESS_LINE, INVITE_ACCESS_LINE),
    }


def _save_invite_mail_template(conn, subject, body):
    clean_subject, clean_body = _normalize_invite_mail_template(subject, body)
    _ensure_admin_settings(conn)
    conn.executemany('''
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=CURRENT_TIMESTAMP
    ''', (
        (INVITE_MAIL_SUBJECT_KEY, clean_subject),
        (INVITE_MAIL_BODY_KEY, clean_body),
    ))
    return clean_subject, clean_body


def send_real_email(target_email, invite_link, subject, content, sender=None):
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SENDER_EMAIL = os.environ.get('MAIL_USERNAME') or "saedam2025@gmail.com"
    SENDER_PASSWORD = os.environ.get('MAIL_PASSWORD') or "wjuybedxstdmszdt"

    sender_data = dict(sender) if sender is not None else None
    if sender_data:
        SENDER_EMAIL = str(sender_data.get('email') or '').strip().lower()
    if not SENDER_EMAIL or (not sender_data and not SENDER_PASSWORD): return False

    msg = MIMEMultipart('alternative')
    msg['From'] = _sender_from_header(sender_data) if sender_data else f"새담 인트라넷 <{SENDER_EMAIL}>"
    msg['To'] = target_email
    msg['Subject'] = subject

    safe_content = '<br>'.join(escape(content).splitlines()).replace(
        escape(INTRANET_DIRECT_URL),
        f'<a href="{INTRANET_DIRECT_URL}" target="_blank" style="color:#2563eb; font-weight:bold;">{INTRANET_DIRECT_URL}</a>',
    )
    # 본문에 인트라넷 주소가 없으면 접속 안내를 따로 덧붙인다.
    has_access_guide = 'works.saedam.org' in str(content).lower()
    access_guide_html = '' if has_access_guide else f'''
                <p style="margin: 18px 0 0; padding: 12px 14px; background: #f0f7ff; border-radius: 8px; color: #334155; font-size: 14px; line-height: 1.7;">
                    인트라넷 접속 방법: 가입 승인 후 인트라넷 주소 <a href="{INTRANET_DIRECT_URL}" target="_blank" style="color:#2563eb; font-weight:bold;">{INTRANET_DIRECT_URL}</a> 로 접속해 주세요.
                </p>
    '''
    access_guide_plain = '' if has_access_guide else f'\n\n{INVITE_ACCESS_LINE}'

    # [수정됨] 이메일 클라이언트(아웃룩, 지메일 등)에서 호환성이 높은 테이블을 사용한 가로 배열 디자인
    body = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-family: sans-serif; max-width: 700px; margin: 0 auto; border: 1px solid #ddd; border-radius: 12px; background-color: #ffffff; border-collapse: separate; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
        <tr>
            <td style="padding: 30px; vertical-align: middle;">
                <h2 style="color: #4a90e2; margin: 0 0 10px 0; font-size: 22px;">새담 인트라넷 초대</h2>
                <p style="margin: 0; color: #555; font-size: 15px; line-height: 1.7;">{safe_content}</p>
                {access_guide_html}
            </td>
            <td style="padding: 30px; text-align: right; vertical-align: middle; width: 160px; background-color: #f8fbff; border-left: 1px solid #eee;">
                <a href="{invite_link}" target="_blank" style="display: inline-block; background: #4a90e2; color: white; padding: 14px 24px; text-decoration: none; border-radius: 8px; font-weight: bold; white-space: nowrap; font-size: 15px; box-shadow: 0 2px 4px rgba(74, 144, 226, 0.3);">가입 신청하기</a>
            </td>
        </tr>
    </table>
    """
    msg.attach(MIMEText(
        content + access_guide_plain + f'\n\n가입 신청: {invite_link}',
        'plain',
        'utf-8',
    ))
    msg.attach(MIMEText(body, 'html', 'utf-8'))
    try:
        if sender_data:
            server = _smtp_login_for_sender(sender_data)
            try:
                _verify_smtp_sender(server, sender_data)
                server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
            return True
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, target_email, msg.as_string())
        server.quit()
        return True
    except:
        return False


def send_membership_result_email(
    target_email, applicant_name, approved, emp_no='', position='', department='',
    rejection_reason=''
):
    """가입 승인 또는 거부 결과를 신청자에게 안내한다."""
    target_email = _normalize_email(target_email)
    if not _is_valid_email(target_email):
        return False

    smtp_server = "smtp.gmail.com"
    smtp_port = 587
    sender_email = os.environ.get('MAIL_USERNAME') or "lunch9797@gmail.com"
    sender_password = os.environ.get('MAIL_PASSWORD') or "txnbofpijgysjpfq"
    if not sender_email or not sender_password:
        return False

    safe_name = str(applicant_name or '신청자').strip() or '신청자'
    if approved:
        subject = '[새담 인트라넷] 가입 승인 완료'
        heading = '가입 신청이 승인되었습니다.'
        accent_color = '#16a34a'
        detail_lines = [
            f'{safe_name}님, 새담 인트라넷 가입 신청이 승인되었습니다.',
            f'사번: {emp_no}',
            f'소속부서: {department}',
            f'직급: {position}',
            '이제 새담 인트라넷에 로그인하여 이용하실 수 있습니다.',
            f'접속방법: 인트라넷 주소 {INTRANET_DIRECT_URL} 로 접속해 주세요.',
        ]
    else:
        subject = '[새담 인트라넷] 가입 승인 거부 안내'
        heading = '가입 신청이 승인되지 않았습니다.'
        accent_color = '#dc2626'
        detail_lines = [
            f'{safe_name}님, 새담 인트라넷 가입 신청이 승인되지 않았습니다.',
            f'거부 사유: {str(rejection_reason or "").strip()}',
            '관련 문의가 필요한 경우 새담 인트라넷 관리자에게 연락해 주세요.',
        ]

    plain_content = '\n'.join(detail_lines)
    safe_content = '<br>'.join(escape(line) for line in detail_lines)
    if approved:
        safe_content = safe_content.replace(
            escape(INTRANET_DIRECT_URL),
            f'<a href="{INTRANET_DIRECT_URL}" target="_blank" style="color:#2563eb;">{INTRANET_DIRECT_URL}</a>',
        )
    message = MIMEMultipart('alternative')
    message['From'] = f"새담 인트라넷 <{sender_email}>"
    message['To'] = target_email
    message['Subject'] = subject
    message.attach(MIMEText(plain_content, 'plain', 'utf-8'))
    message.attach(MIMEText(f'''
        <table width="100%" cellpadding="0" cellspacing="0" style="font-family:sans-serif;max-width:640px;margin:0 auto;border:1px solid #e5e7eb;border-radius:14px;background:#ffffff;border-collapse:separate;overflow:hidden;">
            <tr><td style="height:7px;background:{accent_color};font-size:0;">&nbsp;</td></tr>
            <tr>
                <td style="padding:32px;">
                    <h2 style="margin:0 0 18px;color:{accent_color};font-size:22px;">{escape(heading)}</h2>
                    <p style="margin:0;color:#374151;font-size:15px;line-height:1.8;">{safe_content}</p>
                </td>
            </tr>
        </table>
    ''', 'html', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=20) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, target_email, message.as_string())
        return True
    except Exception as exc:
        print(f"가입 처리 결과 메일 발송 실패: {exc}")
        return False


def send_account_recovery_email(target_email, member_name, emp_no, temporary_password):
    """사번과 새 임시 비밀번호를 가입 이메일로 안내한다."""
    target_email = _normalize_email(target_email)
    if not _is_valid_email(target_email):
        return False

    sender_email = os.environ.get('MAIL_USERNAME') or "lunch9797@gmail.com"
    sender_password = os.environ.get('MAIL_PASSWORD') or "txnbofpijgysjpfq"
    if not sender_email or not sender_password:
        return False

    safe_name = str(member_name or '회원').strip() or '회원'
    safe_emp_no = str(emp_no or '').strip()
    safe_password = str(temporary_password or '')
    subject = '[새담 인트라넷] 사번 및 임시 비밀번호 안내'
    password_warning = '로그인 후 개인 프로필 수정에서 비밀번호를 반드시 변경해주세요.'
    identity_lines = [
        f'{safe_name}님의 로그인 정보입니다.',
        f'사번: {safe_emp_no}',
        f'임시 비밀번호: {safe_password}',
    ]
    detail_lines = [*identity_lines, password_warning, f'인트라넷 주소: {INTRANET_DIRECT_URL}']
    plain_content = '\n'.join(detail_lines)
    safe_identity = '<br>'.join(escape(line) for line in identity_lines)
    intranet_link = f'<a href="{INTRANET_DIRECT_URL}" target="_blank" style="color:#2563eb;">{escape(INTRANET_DIRECT_URL)}</a>'
    message = MIMEMultipart('alternative')
    message['From'] = f"새담 인트라넷 <{sender_email}>"
    message['To'] = target_email
    message['Subject'] = subject
    message.attach(MIMEText(plain_content, 'plain', 'utf-8'))
    message.attach(MIMEText(f'''
        <style>
            @keyframes recoveryPasswordWarningBlink {{
                0%, 100% {{ opacity:1; text-shadow:0 0 0 rgba(220,38,38,0); }}
                50% {{ opacity:.28; text-shadow:0 0 9px rgba(220,38,38,.8); }}
            }}
            .recovery-password-warning {{
                color:#dc2626 !important;
                font-weight:900 !important;
                animation:recoveryPasswordWarningBlink 1s ease-in-out infinite;
            }}
        </style>
        <table width="100%" cellpadding="0" cellspacing="0" style="font-family:sans-serif;max-width:640px;margin:0 auto;border:1px solid #e5e7eb;border-radius:14px;background:#ffffff;border-collapse:separate;overflow:hidden;">
            <tr><td style="height:7px;background:#2563eb;font-size:0;">&nbsp;</td></tr>
            <tr>
                <td style="padding:32px 20px 32px 32px;vertical-align:middle;">
                    <h2 style="margin:0 0 18px;color:#2563eb;font-size:22px;">사번 및 임시 비밀번호 안내</h2>
                    <p style="margin:0;color:#374151;font-size:15px;line-height:1.9;">{safe_identity}</p>
                    <p class="recovery-password-warning" style="margin:16px 0 12px;color:#dc2626;font-size:15px;line-height:1.7;font-weight:900;animation:recoveryPasswordWarningBlink 1s ease-in-out infinite;">{escape(password_warning)}</p>
                    <p style="margin:0;color:#374151;font-size:14px;line-height:1.8;">인트라넷 주소: {intranet_link}</p>
                </td>
                <td width="155" style="padding:28px 28px 28px 8px;text-align:center;vertical-align:middle;background:#f8fbff;border-left:1px solid #eef2f7;">
                    <img src="https://www.saedam.org/img/logo01.gif" width="115" alt="새담청소년교육문화원" style="display:block;width:115px;max-width:100%;height:auto;margin:0 auto;border:0;">
                </td>
            </tr>
        </table>
    ''', 'html', 'utf-8'))

    try:
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=20) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, target_email, message.as_string())
        return True
    except Exception as exc:
        print(f"로그인 정보 메일 발송 실패: {exc}")
        return False

@user_mgmt_bp.route('/')
@admin_required
def index():
    try:
        conn = get_db()
        # 1. 사번 admin인 계정이 있는지 확인
        admin = conn.execute("SELECT id FROM users WHERE emp_no = 'admin'").fetchone()
        conn.close()
        
        # 관리자 계정이 없다면 최초 설정 모드로 렌더링
        if not admin:
            return render_template('user_list.html', mode='admin_setup')
            
    except Exception as e:
        print(f"Admin 자동 생성 오류: {e}")

    return render_template('user_list.html')


@user_mgmt_bp.route('/invite-sender')
@menu_permission_required('organization_invite')
def invite_sender_page():
    """조직관리에서 가입초대 메일만 독립적으로 발송하는 화면."""
    return render_template('organization_invite.html')


@user_mgmt_bp.route('/invite_senders')
@menu_permission_required('organization_invite')
def invite_senders():
    """가입초대 화면에서 기존 공용 발송계정을 선택할 수 있게 제공한다."""
    owner = str(session.get('emp_no') or '').strip()
    conn = get_db()
    try:
        _ensure_sender_schema(conn)
        _ensure_admin_settings(conn)
        rows = conn.execute('''
            SELECT * FROM ai_mail_senders
            WHERE owner_emp_no=? AND is_active=1
            ORDER BY CASE WHEN last_test_status='success' THEN 0 ELSE 1 END,
                     updated_at DESC, id DESC
        ''', (owner,)).fetchall()
        setting_key = _invite_sender_setting_key(owner)
        saved = conn.execute(
            'SELECT value FROM admin_settings WHERE key=?',
            (setting_key,),
        ).fetchone()
        sender_ids = {int(row['id']) for row in rows}
        try:
            active_sender_id = int(saved['value']) if saved else None
        except (TypeError, ValueError):
            active_sender_id = None
        if active_sender_id not in sender_ids:
            active_sender_id = int(rows[0]['id']) if rows else None
        return jsonify({
            'status': 'success',
            'senders': [_payroll_sender_dict(row) for row in rows],
            'active_sender_id': active_sender_id,
        })
    finally:
        conn.close()


@user_mgmt_bp.route('/positions', methods=['GET', 'POST'])
@admin_required
def manage_positions():
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = _position_rows(conn)
            return jsonify([
                {
                    'id': row['id'], 'name': row['name'], 'level': row['level'],
                    'sort_order': row['sort_order'], 'user_count': row['user_count'],
                }
                for row in rows
            ])

        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        if not name or len(name) > 40:
            return jsonify({'status': 'error', 'message': '직급명은 1~40자로 입력해주세요.'}), 400
        try:
            level = int(data.get('level'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': '레벨을 숫자로 입력해주세요.'}), 400
        if level < 0 or level > 99:
            return jsonify({'status': 'error', 'message': '레벨은 0~99 범위로 입력해주세요.'}), 400

        _ensure_hr_schema(conn)
        sort_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM hr_positions").fetchone()[0]
        try:
            conn.execute(
                "INSERT INTO hr_positions (name, level, sort_order) VALUES (?, ?, ?)",
                (name, level, sort_order),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if 'UNIQUE' in str(exc).upper():
                return jsonify({'status': 'error', 'message': '이미 등록된 직급입니다.'}), 409
            raise
        return jsonify({'status': 'success', 'message': '직급을 생성했습니다.'}), 201
    finally:
        conn.close()


@user_mgmt_bp.route('/positions/<int:position_id>', methods=['PUT', 'DELETE'])
@admin_required
def update_position(position_id):
    conn = get_db()
    try:
        _ensure_hr_schema(conn)
        row = conn.execute("SELECT id, name, level FROM hr_positions WHERE id = ?", (position_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '직급을 찾을 수 없습니다.'}), 404

        if request.method == 'DELETE':
            used_count = conn.execute("SELECT COUNT(*) FROM users WHERE position = ?", (row['name'],)).fetchone()[0]
            if used_count:
                return jsonify({
                    'status': 'error',
                    'message': f"{used_count}명의 구성원이 사용 중인 직급은 삭제할 수 없습니다.",
                }), 409
            conn.execute("DELETE FROM hr_positions WHERE id = ?", (position_id,))
            conn.commit()
            return jsonify({'status': 'success', 'message': '직급을 삭제했습니다.'})

        data = request.get_json(silent=True) or {}
        name = str(data.get('name') or '').strip()
        if not name or len(name) > 40:
            return jsonify({'status': 'error', 'message': '직급명은 1~40자로 입력해주세요.'}), 400
        try:
            level = int(data.get('level'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': '레벨을 숫자로 입력해주세요.'}), 400
        if level < 0 or level > 99:
            return jsonify({'status': 'error', 'message': '레벨은 0~99 범위로 입력해주세요.'}), 400

        duplicate = conn.execute(
            "SELECT id FROM hr_positions WHERE name = ? AND id != ?", (name, position_id)
        ).fetchone()
        if duplicate:
            return jsonify({'status': 'error', 'message': '이미 등록된 직급입니다.'}), 409

        # 직급명/레벨 변경은 해당 직급을 사용하는 구성원에게 함께 반영한다.
        conn.execute(
            "UPDATE hr_positions SET name = ?, level = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, level, position_id),
        )
        conn.execute("UPDATE users SET position = ?, level = ? WHERE position = ?", (name, level, row['name']))
        conn.commit()
        return jsonify({'status': 'success', 'message': '직급과 레벨을 수정했습니다.'})
    finally:
        conn.close()

# 🚀 신규 추가: 인트라넷 최초 구동 시 관리자 비밀번호 입력 라우트
@user_mgmt_bp.route('/setup_admin', methods=['POST'])
@admin_required
def setup_admin():
    try:
        data = request.json
        password = data.get('password')
        if not password:
            return jsonify({"status": "error", "message": "비밀번호를 입력해주세요."}), 400
            
        conn = get_db()
        _ensure_hr_schema(conn)
        existing = conn.execute(
            "SELECT id FROM users WHERE emp_no='admin' LIMIT 1"
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({
                "status": "error",
                "message": "관리자 계정이 이미 설정되어 있습니다."
            }), 409
        today = datetime.now().strftime('%Y-%m-%d')
        conn.execute('''
            INSERT INTO users (emp_no, name, password, position, level, rrn, email, status,
                               join_date, profile_icon, department, applied_at, approved_at)
            VALUES ('admin', 'admin', ?, '최고관리자', 1, '-', 'admin@admin.com',
                    '승인', ?, '👑', '본부', ?, ?)
        ''', (hash_password(password), today, f'{today} 00:00:00', f'{today} 00:00:00'))
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "message": "최고관리자 계정이 성공적으로 설정되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/invite_page/<token>')
def invite_page(token):
    conn = get_db()
    try:
        _ensure_user_invite_schema(conn)
        token_hash = _invite_token_hash(token)
        invite = conn.execute(
            'SELECT * FROM user_invites WHERE token_hash=?',
            (token_hash,),
        ).fetchone()

        # 기존 방식으로 이미 발송된 Base64 링크도 최초 접근 시 일회용 초대로 전환한다.
        if not invite:
            try:
                legacy_email = _normalize_email(base64.b64decode(token).decode('utf-8'))
            except Exception:
                legacy_email = ''
            if _is_valid_email(legacy_email):
                conn.execute('''
                    INSERT OR IGNORE INTO user_invites
                        (token_hash, email, status, sent_at)
                    VALUES (?, ?, 'sent', CURRENT_TIMESTAMP)
                ''', (token_hash, legacy_email))
                conn.commit()
                invite = conn.execute(
                    'SELECT * FROM user_invites WHERE token_hash=?',
                    (token_hash,),
                ).fetchone()

        if not invite:
            return '유효하지 않은 가입초대 링크입니다.', 403

        existing_user = conn.execute(
            'SELECT id FROM users WHERE LOWER(TRIM(COALESCE(email, \'\'))) = ? LIMIT 1',
            (_normalize_email(invite['email']),),
        ).fetchone()
        if invite['status'] != 'sent' or existing_user:
            if invite['status'] == 'sent':
                conn.execute('''
                    UPDATE user_invites
                    SET status='used', used_at=CURRENT_TIMESTAMP, used_user_id=?
                    WHERE id=?
                ''', (existing_user['id'] if existing_user else None, invite['id']))
                conn.commit()
            return render_template(
                'user_list.html',
                mode='invite_expired',
                invite_message='이미 가입신청된 링크입니다.',
            ), 410

        return render_template(
            'user_list.html',
            invite_email=invite['email'],
            invite_token=token,
            mode='invite',
        )
    finally:
        conn.close()

@user_mgmt_bp.route('/send_invite', methods=['POST'])
@menu_permission_required('organization_invite')
def send_invite():
    try:
        data = request.get_json(silent=True) or {}
        email = _normalize_email(data.get('email'))
        if not _is_valid_email(email):
            return jsonify({"status": "error", "message": "올바른 이메일 주소를 입력해주세요."}), 400
        try:
            sender_id = int(data.get('sender_id'))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "발송메일 계정을 선택해주세요."}), 400

        conn = get_db()
        try:
            _ensure_user_invite_schema(conn)
            _ensure_sender_schema(conn)
            _ensure_admin_settings(conn)
            sender_row = conn.execute('''
                SELECT * FROM ai_mail_senders
                WHERE id=? AND owner_emp_no=? AND is_active=1
            ''', (sender_id, str(session.get('emp_no') or '').strip())).fetchone()
            if not sender_row:
                return jsonify({
                    "status": "error",
                    "message": "선택한 발송메일 계정을 사용할 수 없습니다. 계정 상태를 확인해주세요.",
                }), 400
            sender = dict(sender_row)
            existing_user = conn.execute(
                'SELECT id FROM users WHERE LOWER(TRIM(COALESCE(email, \'\'))) = ? LIMIT 1',
                (email,),
            ).fetchone()
            if existing_user:
                return jsonify({
                    'status': 'error',
                    'message': '이미 가입 신청했거나 가입된 이메일입니다.',
                }), 409

            current_template = _load_invite_mail_template(conn)
            subject, body = _save_invite_mail_template(
                conn,
                data.get('subject', current_template['subject']),
                data.get('body', current_template['body']),
            )
            token = secrets.token_urlsafe(32)
            cursor = conn.execute('''
                INSERT INTO user_invites (
                    token_hash, email, status, sent_at,
                    sender_id, sender_email, sender_provider
                ) VALUES (?, ?, 'sent', CURRENT_TIMESTAMP, ?, ?, ?)
            ''', (
                _invite_token_hash(token), email, sender_id,
                str(sender.get('email') or ''), str(sender.get('provider') or 'gmail'),
            ))
            invite_id = cursor.lastrowid
            conn.execute('''
                INSERT INTO admin_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=CURRENT_TIMESTAMP
            ''', (_invite_sender_setting_key(session.get('emp_no')), str(sender_id)))
            conn.commit()
        finally:
            conn.close()

        invite_link = url_for('user_mgmt.invite_page', token=token, _external=True)
        if send_real_email(email, invite_link, subject, body, sender=sender):
            return jsonify({"status": "success", "message": "메일 내용을 저장하고 초대 메일을 발송했습니다."})
        failed_conn = get_db()
        try:
            failed_conn.execute(
                "UPDATE user_invites SET status='failed' WHERE id=?",
                (invite_id,),
            )
            failed_conn.commit()
        finally:
            failed_conn.close()
        return jsonify({"status": "error", "message": "발송 실패 (서버 설정을 확인하세요)"}), 500
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@user_mgmt_bp.route('/invite_mail_template', methods=['GET', 'POST'])
@menu_permission_required('organization_invite')
def invite_mail_template():
    conn = get_db()
    try:
        if request.method == 'GET':
            template = _load_invite_mail_template(conn)
            conn.commit()
            return jsonify({
                'status': 'success',
                **template,
                'default_subject': DEFAULT_INVITE_MAIL_SUBJECT,
                'default_body': DEFAULT_INVITE_MAIL_BODY,
            })

        data = request.get_json(silent=True) or {}
        subject, body = _save_invite_mail_template(
            conn,
            data.get('subject'),
            data.get('body'),
        )
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': '가입 초대 메일 내용을 저장했습니다.',
            'subject': subject,
            'body': body,
        })
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400
    finally:
        conn.close()

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    conn = None
    upload_path = None
    try:
        data = request.form
        profile_file = request.files.get('profile_image')
        is_admin_direct = is_admin_session()
        
        password = data.get('password')
        password_confirm = data.get('password_confirm')
        if not password:
            return jsonify({"status": "error", "message": "비밀번호를 입력해주세요."}), 400
        if password != password_confirm:
            return jsonify({"status": "error", "message": "비밀번호가 일치하지 않습니다."}), 400
        if not _is_valid_signup_password(password):
            return jsonify({
                "status": "error",
                "message": "비밀번호는 영문, 숫자, 특수문자를 포함하여 12자 이내로 입력해주세요.",
            }), 400
        consent_given = str(data.get('privacy_security_consent') or '').strip() == '1'
        if not consent_given and not is_admin_direct:
            return jsonify({
                "status": "error",
                "message": "개인정보 활용·개인정보 보호·영업비밀 보안 동의가 필요합니다."
            }), 400
        rrn_digits = _normalize_rrn(data.get('rrn'))
        if not is_admin_direct and not _is_valid_rrn(rrn_digits):
            return jsonify({
                'status': 'error',
                'message': '유효한 주민등록번호를 입력해주세요.',
            }), 400
        email = _normalize_email(data.get('email'))
        if (not is_admin_direct or email) and not _is_valid_email(email):
            return jsonify({
                'status': 'error',
                'message': '유효한 이메일 주소를 입력해주세요.',
            }), 400
        department = str(data.get('department', '')).strip()
        if department not in DEPARTMENT_OPTIONS:
            return jsonify({
                "status": "error",
                "message": "소속부서를 선택해주세요."
            }), 400

        conn = get_db()
        _ensure_hr_schema(conn)
        _ensure_user_invite_schema(conn)
        invite = None
        if not is_admin_direct:
            invite_token = str(data.get('invite_token') or '').strip()
            invite = conn.execute(
                'SELECT * FROM user_invites WHERE token_hash=?',
                (_invite_token_hash(invite_token),),
            ).fetchone() if invite_token else None
            if not invite or invite['status'] != 'sent':
                conn.close()
                return jsonify({
                    'status': 'error',
                    'message': '이미 가입신청된 링크입니다.' if invite else '유효하지 않은 가입초대 링크입니다.',
                }), 410
            if _normalize_email(invite['email']) != email:
                conn.close()
                return jsonify({
                    'status': 'error',
                    'message': '초대받은 이메일과 가입 이메일이 일치하지 않습니다.',
                }), 400

        # 주민번호(RRN)는 민감 정보 보호 원칙에 따라 digits를 출력하지 않고 generic placeholder를 사용하거나 처리를 우회합니다.
        dup = _duplicate_rrn_user(conn, rrn_digits)
        if dup:
            conn.close()
            return jsonify({"status": "error", "message": "동일한 주민등록번호로 가입 신청한 사용자가 있습니다."}), 409
        if not is_admin_direct:
            email_dup = conn.execute(
                'SELECT id FROM users WHERE LOWER(TRIM(COALESCE(email, \'\'))) = ? LIMIT 1',
                (email,),
            ).fetchone()
            if email_dup:
                conn.execute('''
                    UPDATE user_invites
                    SET status='used', used_at=CURRENT_TIMESTAMP, used_user_id=?
                    WHERE email=? AND status='sent'
                ''', (email_dup['id'], email))
                conn.commit()
                conn.close()
                return jsonify({'status': 'error', 'message': '이미 가입신청된 링크입니다.'}), 410

        profile_path = None
        if profile_file and profile_file.filename != '':
            os.makedirs(PROFILE_ROOT, exist_ok=True)
            display_name = original_filename(profile_file.filename, 'profile-image')
            safe_filename = encrypted_storage_name(display_name)
            upload_path = os.path.join(PROFILE_ROOT, safe_filename)
            encrypt_upload(profile_file, upload_path)
            
            # HTML에서 이미지를 불러올 라우트 주소
            profile_path = f"/user/profile_img/{safe_filename}"

        icon = data.get('profile_icon', '👤')
        requested_position = str(data.get('position') or '미지정').strip()
        requested_level = _position_level(conn, requested_position, 14)
        consent_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if consent_given else None
        applied_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        consent_version = PRIVACY_SECURITY_CONSENT_VERSION if consent_given else ''
        stored_rrn = _format_rrn(rrn_digits) if _is_valid_rrn(rrn_digits) else str(data.get('rrn') or '').strip()
        cursor = conn.execute('''
            INSERT INTO users (name, password, position, level, rrn, email, phone,
                               address, department, bank_account, profile_path, status, profile_icon,
                               custom_department, custom_team, privacy_security_consent,
                               privacy_security_consent_at, privacy_security_consent_version,
                               applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '대기', ?, ?, ?, ?, ?, ?, ?)
        ''', (data.get('name'), hash_password(password), requested_position, requested_level, stored_rrn,
              email, data.get('phone', ''), data.get('address', ''), 
              department, data.get('bank_account', ''), profile_path, icon,
              str(data.get('custom_department') or '').strip()[:100],
              str(data.get('custom_team') or '').strip()[:100],
              1 if consent_given else 0, consent_at, consent_version, applied_at))

        if invite:
            updated = conn.execute('''
                UPDATE user_invites
                SET status='used', used_at=CURRENT_TIMESTAMP, used_user_id=?
                WHERE email=? AND status='sent'
            ''', (cursor.lastrowid, email))
            if updated.rowcount < 1:
                conn.rollback()
                conn.close()
                delete_file(upload_path)
                return jsonify({'status': 'error', 'message': '이미 가입신청된 링크입니다.'}), 410
        
        conn.commit()
        conn.close()
        upload_path = None
        return jsonify({"status": "success", "message": "가입 신청이 완료되었습니다."})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        delete_file(upload_path)
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/approve', methods=['POST'])
@admin_required
def approve():
    try:
        data = request.get_json(silent=True) or {}
        user_id = int(data['user_idx'])
        pos = str(data.get('approved_position') or '').strip()
        department = str(data.get('approved_department') or '').strip()
        custom_department = str(data.get('custom_department') or '').strip()[:100]
        custom_team = str(data.get('custom_team') or '').strip()[:100]
        if department not in DEPARTMENT_OPTIONS:
            return jsonify({'status': 'error', 'message': '배정할 소속부서를 선택해주세요.'}), 400
        
        conn = get_db()
        _ensure_hr_schema(conn)
        user = conn.execute(
            "SELECT id, name, email, rrn, status FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({'status': 'error', 'message': '승인할 사용자를 찾을 수 없습니다.'}), 404
        if user['status'] != '대기':
            conn.close()
            return jsonify({'status': 'error', 'message': '이미 처리된 가입 신청입니다.'}), 409

        rrn_digits = _normalize_rrn(user['rrn'])
        duplicate_user = _duplicate_rrn_user(conn, rrn_digits, exclude_user_id=user_id)
        if duplicate_user:
            conn.close()
            return jsonify({
                'status': 'error',
                'message': '동일한 주민등록번호로 가입된 회원이 있어 승인할 수 없습니다.',
            }), 409

        position_row = conn.execute("SELECT level FROM hr_positions WHERE name = ?", (pos,)).fetchone()
        if not position_row:
            conn.close()
            return jsonify({"status": "error", "message": "등록된 직급을 선택해주세요."}), 400
        emp_no = generate_sd_emp_no(conn, pos)
        join_date = datetime.now().strftime('%Y-%m-%d')
        approved_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        level = int(position_row['level'])
        
        conn.execute('''
            UPDATE users
            SET emp_no=?, position=?, level=?, status='승인', join_date=?,
                department=?, custom_department=?, custom_team=?, approved_at=?
            WHERE id=?
        ''', (emp_no, pos, level, join_date, department, custom_department, custom_team,
              approved_at, user_id))
        conn.commit()
        conn.close()

        mail_sent = send_membership_result_email(
            user['email'], user['name'], True,
            emp_no=emp_no, position=pos, department=department,
        )
        message = f"승인 완료! (사번: {emp_no})"
        message += "\n승인 완료 메일을 발송했습니다." if mail_sent else "\n승인은 완료됐지만 결과 메일 발송에 실패했습니다."
        return jsonify({"status": "success", "message": message, "mail_sent": mail_sent})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@user_mgmt_bp.route('/reject', methods=['POST'])
@admin_required
def reject_user():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        user_id = int(data['user_idx'])
        rejection_reason = str(data.get('rejection_reason') or '').strip()
        if not rejection_reason:
            return jsonify({'status': 'error', 'message': '가입 승인 거부 사유를 입력해주세요.'}), 400
        if len(rejection_reason) > 500:
            return jsonify({'status': 'error', 'message': '거부 사유는 500자 이내로 입력해주세요.'}), 400
        conn = get_db()
        _ensure_hr_schema(conn)
        user = conn.execute(
            "SELECT id, name, email, status FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
        if not user:
            return jsonify({'status': 'error', 'message': '거부할 가입 신청을 찾을 수 없습니다.'}), 404
        if user['status'] != '대기':
            return jsonify({'status': 'error', 'message': '이미 처리된 가입 신청입니다.'}), 409

        conn.execute('''
            UPDATE users
            SET status='거부', rejection_reason=?, rejected_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='대기'
        ''', (rejection_reason, user_id))
        conn.commit()
        applicant_name = user['name']
        applicant_email = user['email']
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        if conn:
            conn.close()

    mail_sent = send_membership_result_email(
        applicant_email, applicant_name, False, rejection_reason=rejection_reason
    )
    message = '가입 승인을 거부했습니다.'
    message += '\n승인 거부 메일을 발송했습니다.' if mail_sent else '\n거부 처리는 완료됐지만 결과 메일 발송에 실패했습니다.'
    return jsonify({'status': 'success', 'message': message, 'mail_sent': mail_sent})


@user_mgmt_bp.route('/delete_pending', methods=['POST'])
@admin_required
def delete_pending_user():
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        user_id = int(data['user_idx'])
        conn = get_db()
        user = conn.execute("SELECT id, status, profile_path FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return jsonify({'status': 'error', 'message': '삭제할 가입 신청을 찾을 수 없습니다.'}), 404
        if user['status'] != '대기':
            return jsonify({'status': 'error', 'message': '승인 대기 중인 가입 신청만 삭제할 수 있습니다.'}), 409

        conn.execute("DELETE FROM users WHERE id=? AND status='대기'", (user_id,))
        conn.commit()
        delete_file(_profile_disk_path(user['profile_path']))
        return jsonify({'status': 'success', 'message': '가입 신청을 영구 삭제했습니다.'})
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        if conn:
            conn.close()

@user_mgmt_bp.route('/retire', methods=['POST'])
@admin_required
def retire_user():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        retire_date = datetime.now().strftime('%Y-%m-%d')
        
        conn = get_db()
        conn.execute("UPDATE users SET retire_date=? WHERE id=?", (retire_date, user_id))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "퇴사 처리가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/update', methods=['POST'])
@admin_required
def update_user():
    conn = None
    upload_path = None
    try:
        data = request.form
        user_id = int(data.get('user_idx', 0))
        profile_file = request.files.get('profile_image')
        department = str(data.get('department', '')).strip()
        if department not in DEPARTMENT_OPTIONS:
            return jsonify({
                "status": "error",
                "message": "소속부서는 본부, 북부지점, 파견, 기타 중에서 선택해주세요."
            }), 400
        
        conn = get_db()
        _ensure_hr_schema(conn)
        position = str(data.get('position') or '').strip()
        position_row = conn.execute("SELECT level FROM hr_positions WHERE name = ?", (position,)).fetchone()
        if not position_row:
            conn.close()
            return jsonify({"status": "error", "message": "등록된 직급을 선택해주세요."}), 400
        level = int(position_row['level'])
        custom_department = str(data.get('custom_department') or '').strip()[:100]
        custom_team = str(data.get('custom_team') or '').strip()[:100]
        
        remove_profile_image = str(data.get('remove_profile_image', '')).strip().lower() in ('1', 'true', 'on')
        old_profile = None

        if profile_file and profile_file.filename != '':
            os.makedirs(PROFILE_ROOT, exist_ok=True)
            display_name = original_filename(profile_file.filename, 'profile-image')
            safe_filename = encrypted_storage_name(display_name)
            upload_path = os.path.join(PROFILE_ROOT, safe_filename)
            encrypt_upload(profile_file, upload_path)
            profile_path = f"/user/profile_img/{safe_filename}"
            old_profile = conn.execute(
                "SELECT profile_path FROM users WHERE id=?", (user_id,)
            ).fetchone()
            conn.execute("UPDATE users SET profile_path=? WHERE id=?", (profile_path, user_id))
        elif remove_profile_image:
            old_profile = conn.execute(
                "SELECT profile_path FROM users WHERE id=?", (user_id,)
            ).fetchone()
            conn.execute("UPDATE users SET profile_path=NULL WHERE id=?", (user_id,))

        # 새로 입력받은 비밀번호 (앞뒤 공백 제거)
        new_password = data.get('password', '').strip()
        
        if new_password:
            # 1. 새 비밀번호가 입력된 경우 (비밀번호 포함 전체 업데이트)
            conn.execute("""
                UPDATE users 
                SET password=?, position=?, level=?, phone=?, email=?,
                    address=?, department=?, bank_account=?, profile_icon=?,
                    custom_department=?, custom_team=?
                WHERE id=?
            """, (
                hash_password(new_password), position, level,
                data.get('phone', ''), data.get('email', ''), data.get('address', ''), 
                department, data.get('bank_account', ''), data.get('profile_icon', '👤'),
                custom_department, custom_team, user_id
            ))
        else:
            # 2. 비밀번호 칸을 비워둔 경우 (기존 비밀번호는 유지하고 나머지만 업데이트)
            conn.execute("""
                UPDATE users 
                SET position=?, level=?, phone=?, email=?,
                    address=?, department=?, bank_account=?, profile_icon=?,
                    custom_department=?, custom_team=?
                WHERE id=?
            """, (
                position, level,
                data.get('phone', ''), data.get('email', ''), data.get('address', ''), 
                department, data.get('bank_account', ''), data.get('profile_icon', '👤'),
                custom_department, custom_team, user_id
            ))
        
        conn.commit()
        if (profile_file and profile_file.filename != '' or remove_profile_image) and old_profile:
            delete_file(_profile_disk_path(old_profile['profile_path']))
        conn.close()
        upload_path = None
        
        return jsonify({"status": "success", "message": "정보 수정 완료"})
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        delete_file(upload_path)
        return jsonify({"status": "error", "message": str(e)}), 500


@user_mgmt_bp.route('/bulk_update', methods=['POST'])
@admin_required
def bulk_update_users():
    """선택한 승인 회원의 조직 정보를 한 번에 변경한다."""
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        raw_user_ids = data.get('user_ids')
        field = str(data.get('field') or '').strip()
        value = str(data.get('value') or '').strip()

        if not isinstance(raw_user_ids, list) or not raw_user_ids:
            return jsonify({'status': 'error', 'message': '수정할 회원을 선택해주세요.'}), 400

        user_ids = []
        for raw_user_id in raw_user_ids:
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                return jsonify({'status': 'error', 'message': '회원 선택 정보가 올바르지 않습니다.'}), 400
            if user_id > 0 and user_id not in user_ids:
                user_ids.append(user_id)

        if not user_ids or len(user_ids) > 500:
            return jsonify({'status': 'error', 'message': '한 번에 수정할 회원은 1명 이상 500명 이하로 선택해주세요.'}), 400

        allowed_fields = {
            'department': 'department',
            'custom_department': 'custom_department',
            'custom_team': 'custom_team',
        }
        column = allowed_fields.get(field)
        if not column:
            return jsonify({'status': 'error', 'message': '일괄수정 항목이 올바르지 않습니다.'}), 400
        if field == 'department' and value not in DEPARTMENT_OPTIONS:
            return jsonify({'status': 'error', 'message': '소속부서를 선택해주세요.'}), 400
        if field != 'department' and len(value) > 100:
            return jsonify({'status': 'error', 'message': '별도 소속과 별도 팀은 100자 이내로 입력해주세요.'}), 400

        conn = get_db()
        _ensure_hr_schema(conn)
        placeholders = ','.join('?' for _ in user_ids)
        approved_rows = conn.execute(
            f"SELECT id FROM users WHERE status='승인' AND id IN ({placeholders})",
            user_ids,
        ).fetchall()
        if len(approved_rows) != len(user_ids):
            return jsonify({'status': 'error', 'message': '승인된 회원만 일괄수정할 수 있습니다.'}), 400

        cursor = conn.execute(
            f"UPDATE users SET {column}=? WHERE status='승인' AND id IN ({placeholders})",
            [value, *user_ids],
        )
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': f'{cursor.rowcount}명의 정보를 일괄수정했습니다.',
            'updated_count': cursor.rowcount,
        })
    except Exception as e:
        if conn is not None:
            conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        if conn is not None:
            conn.close()

@user_mgmt_bp.route('/delete', methods=['POST'])
@admin_required
def delete_user():
    try:
        data = request.json
        user_id = int(data['user_idx'])
        
        conn = get_db()
        user = conn.execute("SELECT profile_path FROM users WHERE id=?", (user_id,)).fetchone()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        conn.close()
        if user:
            delete_file(_profile_disk_path(user['profile_path']))
        
        return jsonify({"status": "success", "message": "삭제 완료"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
@admin_required
def get_user_list():
    conn = get_db()
    _ensure_hr_schema(conn)
    ensure_point_schema(conn)
    conn.commit()
    approved_only = request.args.get('approved_only') == '1'
    query = '''
        SELECT u.*, COALESCE(point_totals.balance, 0) AS point_balance
        FROM users u
        LEFT JOIN (
            SELECT user_name, SUM(points_delta) AS balance
            FROM point_transactions
            GROUP BY user_name
        ) point_totals ON point_totals.user_name=u.name
    '''
    params = []
    if approved_only:
        query += " WHERE u.status = ?"
        params.append('승인')
    query += " ORDER BY u.level ASC, u.id ASC"
    users = conn.execute(query, params).fetchall()
    conn.close()
    
    # 🚀 수정 포인트: 세션을 통해 현재 로그인한 사용자가 '최고관리자'인지 판별
    current_emp_no = session.get('emp_no', '') 
    is_admin_logged_in = (current_emp_no == 'admin')
    
    result = []
    for u in users:
        # 🚀 수정 포인트: Admin 계정은 최고관리자로 로그인했을 때만 명단에 포함
        if u['emp_no'] == 'admin' and not is_admin_logged_in:
            continue
            
        icon = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        profile_path = u['profile_path'] if 'profile_path' in u.keys() else None
        department = u['department'] if 'department' in u.keys() else ''
        custom_department = u['custom_department'] if 'custom_department' in u.keys() else ''
        custom_team = u['custom_team'] if 'custom_team' in u.keys() else ''
        position = u['position'] or ''
        result.append({
            "id": u['id'], "사번": u['emp_no'] or '', "이름": u['name'] or '',
            "직급": position, "레벨": u['level'] if u['level'] is not None else 10, "주민번호": u['rrn'] or '',
            "이메일": u['email'] or '', "전화번호": u['phone'] or '', 
            "주소": u['address'] if 'address' in u.keys() else '',
            "소속": normalize_department(department),
            "별도소속": custom_department or '', "별도팀": custom_team or '',
            "조직그룹": classify_organization_group(department, position),
            "계좌": u['bank_account'] if 'bank_account' in u.keys() else '',
            "가입신청일": u['applied_at'] if 'applied_at' in u.keys() and u['applied_at'] else '',
            "가입승인일": u['approved_at'] if 'approved_at' in u.keys() and u['approved_at'] else '',
            "입사일": u['join_date'] or '', "퇴사일": u['retire_date'] or '', 
            "승인상태": u['status'] or '', "아이콘": icon, "profile_path": profile_path,
            "포인트": int(u['point_balance'] or 0),
        })
    return jsonify(result)

# ==============================================================================
# [필수 라우트 복구] 외부 폴더(/mnt/data/id/)에 저장된 이미지를 불러옵니다.
# ==============================================================================
@user_mgmt_bp.route('/profile_img/<filename>')
def serve_profile_image(filename):
    path = os.path.join(PROFILE_ROOT, os.path.basename(filename))
    if not os.path.isfile(path):
        abort(404)
    return encrypted_response(path, filename.removesuffix('.sdf'), as_attachment=False)
