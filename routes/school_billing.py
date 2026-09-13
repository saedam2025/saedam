"""청구업무 — 학교 담당자와 본사가 강사료·교구재비 청구, 자료요청을 웹으로 처리한다.

구성
- 본사 직원용  : /billing  (학교관리 > 청구업무, 메뉴 권한으로 보호)
- 학교회원 포털: /portal   (인트라넷 계정과 완전히 분리된 별도 로그인)

학교회원 세션에는 인트라넷의 emp_no를 절대 넣지 않는다. 그래서 학교회원은
인트라넷 화면(check_login)에서는 항상 비로그인으로 취급되고 포털만 이용한다.

업무 1건은 시작 → 진행 → 확인 → 완료 순서로만 넘어가며, 완료되면 잠긴다.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from html import escape

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .database import get_db
from .secure_files import (
    delete_file,
    encrypt_stream,
    encrypt_upload,
    encrypted_response,
    encrypted_storage_name,
    original_filename,
)
from flask_socketio import join_room

from .security import has_menu_permission, hash_password, menu_permission_required, verify_password
from .socketio_ext import socketio
from .solapi_settings import format_phone, get_settings as get_solapi_settings, normalize_phone, send_bulk_text
from .storage import DATA_ROOT

billing_bp = Blueprint('school_billing', __name__)
portal_bp = Blueprint('school_portal', __name__)

BILLING_MENU_KEY = 'school_billing'
BILLING_UPLOADS = DATA_ROOT / 'billing_uploads'

TASK_TYPES = {
    'lecture_fee': '강사료청구',
    'material_fee': '교구재비청구',
    'data_request': '자료요청',
}
TASK_TYPE_ICONS = {
    'lecture_fee': 'fa-chalkboard-user',
    'material_fee': 'fa-boxes-stacked',
    'data_request': 'fa-folder-open',
}
# 학교가 업무를 시작할 때 고르는 처리 요청 — 금일 이내 · 긴급은 당일이 처리 기한이 된다.
TASK_PRIORITIES = {
    'normal': {'label': '순차적', 'icon': 'fa-list-ol',
               'message': '특별히 시한을 두지 않고 접수 순서대로 처리합니다.'},
    'today': {'label': '금일 이내', 'icon': 'fa-calendar-day',
              'message': '담당자가 오늘 안에 처리합니다. 처리가 어려우면 전화로 연락드립니다.'},
    'urgent': {'label': '긴급', 'icon': 'fa-bolt',
               'message': '긴급 전화로 연락드리고 빠르게 처리합니다.'},
}

STAGES = ('start', 'progress', 'review', 'done')
STAGE_LABELS = {'start': '시작', 'progress': '진행', 'review': '확인', 'done': '완료'}
# 현재 단계 → (다음 단계, 넘길 수 있는 쪽, 버튼 문구)
STAGE_FLOW = {
    'start': ('progress', ('staff',), '접수하고 진행 시작'),
    'progress': ('review', ('staff',), '처리 완료 · 학교 확인 요청'),
    'review': ('done', ('member', 'staff'), '확인 완료'),
}

INVITE_VALID_DAYS = 7
OTP_VALID_SECONDS = 5 * 60
OTP_RESEND_SECONDS = 60
OTP_MAX_SENDS = 5
OTP_MAX_ATTEMPTS = 5
MAX_FILES_PER_POST = 10
MAX_FILE_BYTES = 30 * 1024 * 1024
# 대용량 첨부는 브라우저가 조각으로 나눠 보내고, 끊기면 받은 곳부터 이어서 보낸다.
UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024
UPLOAD_CHUNK_MAX_BYTES = 4 * 1024 * 1024
UPLOAD_STALE_HOURS = 24
UPLOAD_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{20,64}$')
NOTIFY_PHONE_SETTING = 'billing_notify_phone'   # 지금 문자를 받는 번호(아래 3개 중 선택한 것)
NOTIFY_CONTACTS_SETTING = 'billing_notify_contacts'
NOTIFY_CONTACT_SLOTS = 3
# 학교회원 [이용안내] 탭에 보이는 본사 연락처(본사 화면에서 수정 가능)
CONTACT_DEFAULTS = {
    'billing_contact_phone': '031-8016-1900',
    'billing_contact_manager': '봉성옥 팀장',
    'billing_contact_email': 'edu197@naver.com',
}
# 공지사항은 최근 3개(새 공지 1개 + 이전 공지 2개)만 남기고 나머지는 자동 삭제한다.
NOTICE_KEEP_COUNT = 3

# 본사 담당자 상태 — [기본설정]에서 고르면 학교회원 전용페이지 [업무 시작하기] 오른쪽 끝에 보인다.
STAFF_STATUS_SETTING = 'billing_staff_status'
STAFF_STATUS_UPDATED_SETTING = 'billing_staff_status_updated'   # '변경시각|변경한 직원'
STAFF_STATUSES = [
    {'key': 'away', 'label': '부재중', 'emoji': '🐾', 'color': '#64748b',
     'message': '잠시 자리를 비웠어요. 급한 일은 [이용안내]의 대표전화로 연락해 주세요.'},
    {'key': 'vacation', 'label': '휴가중', 'emoji': '🏖️', 'color': '#0e7490',
     'message': '휴가 중이에요. 복귀하는 대로 차례차례 처리해 드릴게요.'},
    {'key': 'morning_shift', 'label': '오전근무', 'emoji': '🐣', 'color': '#b45309',
     'message': '오늘은 오전만 근무해요. 오후에 주신 요청은 다음 근무일에 확인할게요.'},
    {'key': 'afternoon_shift', 'label': '오후근무', 'emoji': '🌻', 'color': '#a16207',
     'message': '오늘은 오후부터 근무해요. 오전에 주신 요청도 오후에 꼼꼼히 확인할게요.'},
    {'key': 'working', 'label': '업무중', 'emoji': '🐝', 'color': '#2563eb',
     'message': '열심히 업무 중이에요! 요청은 들어온 순서대로 확인하고 있어요.'},
    {'key': 'busy', 'label': '업무폭주중', 'emoji': '🌋', 'color': '#dc2626',
     'message': '요청이 한꺼번에 몰려 처리가 조금 늦어질 수 있어요. 순서대로 꼭 처리해 드릴게요!'},
    {'key': 'fast', 'label': '빠른 처리가능', 'emoji': '🐰', 'color': '#15803d',
     'message': '지금 바로 처리할 수 있어요. 편하게 요청해 주세요!'},
    {'key': 'morning', 'label': '오전내 처리가능', 'emoji': '☀️', 'color': '#c2410c',
     'message': '오늘 오전 안에 처리해 드릴게요.'},
    {'key': 'afternoon', 'label': '오후에 처리가능', 'emoji': '⛅', 'color': '#7c3aed',
     'message': '오늘 오후 중에 처리해 드릴게요.'},
    {'key': 'tomorrow', 'label': '내일 처리가능', 'emoji': '🌙', 'color': '#4338ca',
     'message': '내일 순서대로 처리해 드릴게요. 조금만 기다려 주세요.'},
]
STAFF_STATUS_MAP = {item['key']: item for item in STAFF_STATUSES}
DEFAULT_STAFF_STATUS = 'working'

LOGIN_ID_PATTERN = re.compile(r'^[a-z0-9_]{4,20}$')
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_login_failures = {}
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5

# 장기 미접속 잠금 · 처리 지연 알림 · 아이디/비밀번호 찾기
DORMANT_DAYS = 30            # 이 기간 동안 접속하지 않으면 잠그고, 본사가 허용해야 다시 쓸 수 있다
MEMBER_IDLE_SECONDS = 6 * 60 * 60   # 학교회원이 이 시간 동안 아무 작업도 하지 않으면 자동 로그아웃
INACTIVE_WARN_DAYS = 14      # 본사 화면에 '며칠째 미접속'을 띄우기 시작하는 기준
OVERDUE_DAYS_SETTING = 'billing_overdue_days'
DEFAULT_OVERDUE_DAYS = 3
RECOVERY_VALID_SECONDS = 5 * 60
RESET_TOKEN_VALID_SECONDS = 10 * 60

# 올릴 수 없는 첨부 형식 — 실행 · 스크립트 · 바로가기 · 웹페이지 · 매크로 문서 · 디스크 이미지 (billing.js BLOCKED_EXT와 같게)
BLOCKED_EXTENSIONS = frozenset({
    'exe', 'com', 'bat', 'cmd', 'msi', 'msp', 'mst', 'scr', 'pif', 'cpl', 'dll', 'sys', 'drv', 'ocx',
    'app', 'apk', 'deb', 'rpm', 'dmg', 'pkg', 'gadget', 'application', 'appref-ms', 'msc',
    'ps1', 'psm1', 'psd1', 'vbs', 'vbe', 'js', 'jse', 'mjs', 'wsf', 'wsh', 'wsc', 'hta', 'jar',
    'sh', 'bash', 'py', 'pyw', 'pl', 'rb', 'php', 'asp', 'aspx', 'jsp', 'cgi',
    'lnk', 'url', 'website', 'scf', 'reg', 'inf', 'ins', 'isp',
    'html', 'htm', 'xhtml', 'shtml', 'mht', 'mhtml', 'svg', 'chm',
    'docm', 'dotm', 'xlsm', 'xltm', 'xlam', 'pptm', 'potm', 'ppam', 'ppsm', 'sldm',
    'iso', 'img', 'vhd', 'vhdx',
})


# =====================================================================
# 스키마
# =====================================================================

def ensure_billing_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login_id TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            name TEXT NOT NULL,
            school_name TEXT NOT NULL,
            school_id INTEGER,
            phone TEXT NOT NULL,
            tel TEXT,
            email TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            invite_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            last_login_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            school_name TEXT,
            school_id INTEGER,
            status TEXT NOT NULL DEFAULT 'sent',
            sent_by TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            expires_at TEXT,
            used_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_otps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invite_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            amount TEXT,
            stage TEXT NOT NULL DEFAULT 'start',
            assignee_emp_no TEXT,
            assignee_name TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            completed_at TEXT
        )
    """)
    task_columns = {row[1] for row in conn.execute('PRAGMA table_info(billing_tasks)').fetchall()}
    for column in ('cancel_requested_at', 'cancel_requested_by', 'cancel_reason', 'cancel_requested_side', 'due_date', 'priority'):
        if column not in task_columns:
            conn.execute(f'ALTER TABLE billing_tasks ADD COLUMN {column} TEXT')
    # 담당자 교체: 전 담당자는 status='replaced'로 즉시 정지, 새 담당자 초대에는 교체 대상을 기록한다.
    member_columns = {row[1] for row in conn.execute('PRAGMA table_info(billing_members)').fetchall()}
    for column in ('replaced_at', 'approved_at', 'last_active_at', 'dormant_at', 'unlock_requested_at', 'privacy_agreed_at'):
        if column not in member_columns:
            conn.execute(f'ALTER TABLE billing_members ADD COLUMN {column} TEXT')
            if column == 'approved_at':
                # 승인 기능 전에 가입해 이미 쓰던 계정은 승인된 것으로 보고, 오늘부터 미접속 기간을 센다.
                conn.execute("UPDATE billing_members SET approved_at=? WHERE status='active'", (_now_text(),))
    invite_columns = {row[1] for row in conn.execute('PRAGMA table_info(billing_invites)').fetchall()}
    if 'replaces_member_id' not in invite_columns:
        conn.execute('ALTER TABLE billing_invites ADD COLUMN replaces_member_id INTEGER')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            author_type TEXT NOT NULL,
            author_name TEXT,
            body TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            message_id INTEGER,
            author_type TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            size INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_stage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            from_stage TEXT,
            to_stage TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_name TEXT,
            notified INTEGER NOT NULL DEFAULT 0,
            notify_result TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT,
            is_pinned INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_emergency_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            school_name TEXT,
            member_name TEXT,
            phone TEXT,
            ok INTEGER NOT NULL DEFAULT 0,
            result TEXT,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_upload_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            owner_type TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            original_name TEXT NOT NULL,
            size INTEGER NOT NULL,
            received INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'uploading',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    # 처리 · 삭제 기록 — 청구 다툼이나 개인정보 처리 확인용. 화면에서 지울 수 없다.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            task_id INTEGER,
            member_id INTEGER,
            school_name TEXT,
            member_name TEXT,
            title TEXT,
            task_type TEXT,
            amount TEXT,
            stage TEXT,
            detail TEXT,
            actor_type TEXT,
            actor_name TEXT,
            actor_emp_no TEXT,
            created_at TEXT
        )
    """)
    # 아이디 찾기 · 비밀번호 재설정 · 휴대폰번호 변경 문자 인증
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_recovery_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT NOT NULL,
            member_id INTEGER,
            phone TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            reset_token_hash TEXT,
            used INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    # 본사 직원별로 업무를 마지막에 연 시각 — 학교에서 새 글이 오면 [새 소식]으로 표시한다.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS billing_task_views (
            task_id INTEGER NOT NULL,
            emp_no TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (task_id, emp_no)
        )
    """)
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_billing_tasks_member ON billing_tasks(member_id, stage)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_billing_messages_task ON billing_messages(task_id, id)'
    )
    conn.commit()


# =====================================================================
# 공통 유틸
# =====================================================================

def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _time12(text):
    """화면 · 기록에 보이는 시간은 12시간제로: '2026-09-13 14:24:05' → '2026-09-13 pm 02:24'"""
    try:
        dt = datetime.strptime(str(text or '')[:16], '%Y-%m-%d %H:%M')
    except ValueError:
        return str(text or '')[:16]
    return f"{dt:%Y-%m-%d} {'am' if dt.hour < 12 else 'pm'} {dt.hour % 12 or 12:02d}:{dt:%M}"


def _json_error(message, status=400, **extra):
    payload = {'status': 'error', 'message': message}
    payload.update(extra)
    return jsonify(payload), status


def _token_hash(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _otp_hash(invite_id, code):
    return hashlib.sha256(f'billing-otp:{invite_id}:{code}'.encode('utf-8')).hexdigest()


def _normalize_email(value):
    return str(value or '').strip().lower()


def _client_key():
    forwarded = request.headers.get('X-Forwarded-For', '')
    return (forwarded.split(',')[0].strip() or request.remote_addr or 'unknown')


def _get_setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM admin_settings WHERE key=?', (key,)).fetchone()
    return (row['value'] if row else default) or default


def _set_setting(conn, key, value):
    conn.execute("""
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
    """, (key, value))


def _portal_url(path=''):
    return url_for('school_portal.portal_home', _external=True).rstrip('/') + path


def _send_sms(phone, text):
    """문자 1건을 보내고 (성공 여부, 안내 문구)를 돌려준다. 실패해도 예외를 던지지 않는다."""
    try:
        settings = get_solapi_settings()
        if not settings.get('sms_configured'):
            return False, '솔라피 문자 설정이 완료되지 않아 문자를 보내지 못했습니다.'
        result = send_bulk_text([{'to': phone, 'text': text, 'subject': '새담 청구업무'}],
                                settings=settings)
        row = result[0] if result else {}
        if row.get('ok'):
            return True, '문자를 보냈습니다.'
        return False, row.get('error') or '문자 발송에 실패했습니다.'
    except Exception as exc:  # 문자 실패가 업무 처리를 막지 않게 한다.
        return False, f'문자 발송 오류: {exc}'


# =====================================================================
# 업무 조회/직렬화 (본사·포털 공용)
# =====================================================================

def _task_dict(row):
    task = dict(row)
    task['cancel_requested'] = bool(task.get('cancel_requested_at'))
    task['cancel_requested_side'] = (
        (task.get('cancel_requested_side') or 'member') if task['cancel_requested'] else None
    )
    stage = task.get('stage') or 'start'
    task['type_label'] = TASK_TYPES.get(task.get('task_type'), task.get('task_type'))
    task['type_icon'] = TASK_TYPE_ICONS.get(task.get('task_type'), 'fa-file')
    task['priority_label'] = TASK_PRIORITIES.get(task.get('priority') or '', {}).get('label', '')
    task['stage_label'] = STAGE_LABELS.get(stage, stage)
    task['stage_index'] = STAGES.index(stage) if stage in STAGES else 0
    flow = STAGE_FLOW.get(stage)
    task['next_stage'] = flow[0] if flow else None
    task['next_stage_label'] = STAGE_LABELS.get(flow[0]) if flow else None
    task['next_actors'] = list(flow[1]) if flow else []
    task['next_action_label'] = flow[2] if flow else None
    for flag in ('unread', 'staff_turn', 'overdue'):
        if flag in task:
            task[flag] = bool(task[flag])
    try:
        task['member_phone_display'] = format_phone(task.get('member_phone')) or task.get('member_phone')
    except ValueError:
        task['member_phone_display'] = task.get('member_phone')
    return task


# 본사가 처리해야 할 차례: 취소요청 없는 [시작]·[진행] 업무, 또는 학교가 보낸 취소요청
STAFF_TODO_SQL = (
    "((COALESCE(t.cancel_requested_at, '') = '' AND t.stage IN ('start', 'progress'))"
    " OR (COALESCE(t.cancel_requested_at, '') != '' AND COALESCE(t.cancel_requested_side, 'member') = 'member'))"
)
# 학교 쪽에서 마지막으로 움직인 시각(글 · 학교가 남긴 단계/취소 기록 · 업무 시작)
LAST_MEMBER_SQL = (
    "COALESCE((SELECT MAX(x.created_at) FROM billing_messages x WHERE x.task_id = t.id"
    " AND (x.author_type = 'member' OR (x.author_type = 'system' AND x.body LIKE '학교 %'))), t.created_at)"
)


def _unread_expr(emp_no):
    """(SQL, params) — 이 직원이 업무를 마지막으로 연 뒤 학교에서 새로 올린 것이 있는지."""
    if not emp_no:
        return '0', []
    return (f"({LAST_MEMBER_SQL} > COALESCE((SELECT v.seen_at FROM billing_task_views v"
            f" WHERE v.task_id = t.id AND v.emp_no = ?), ''))"), [emp_no]


def _task_select(extra_columns=''):
    return f"""
    SELECT t.*, m.name AS member_name, m.school_name AS school_name, m.status AS member_status,
           m.phone AS member_phone, m.tel AS member_tel, m.email AS member_email,
           (SELECT COUNT(*) FROM billing_messages x
             WHERE x.task_id = t.id AND x.author_type != 'system') AS message_count,
           (SELECT COUNT(*) FROM billing_files f WHERE f.task_id = t.id) AS file_count{extra_columns}
    FROM billing_tasks t
    JOIN billing_members m ON m.id = t.member_id
"""


TASK_SELECT = _task_select()


def _summary(conn, member_id=None):
    where, params = '', []
    if member_id is not None:
        where, params = 'WHERE member_id = ?', [member_id]
    rows = conn.execute(
        f'SELECT task_type, stage, COUNT(*) AS c FROM billing_tasks {where} '
        'GROUP BY task_type, stage',
        params,
    ).fetchall()
    by_stage = {stage: 0 for stage in STAGES}
    by_type = {key: {'label': label, 'total': 0, 'done': 0} for key, label in TASK_TYPES.items()}
    total = 0
    for row in rows:
        count = int(row['c'])
        total += count
        if row['stage'] in by_stage:
            by_stage[row['stage']] += count
        if row['task_type'] in by_type:
            by_type[row['task_type']]['total'] += count
            if row['stage'] == 'done':
                by_type[row['task_type']]['done'] += count
    done = by_stage['done']
    cancel_sql = ("SELECT COALESCE(cancel_requested_side, 'member') AS side, COUNT(*) AS c FROM billing_tasks "
                  "WHERE COALESCE(cancel_requested_at, '') != ''")
    if member_id is not None:
        cancel_sql += ' AND member_id = ?'
    cancel_by = {'member': 0, 'staff': 0}
    for row in conn.execute(cancel_sql + ' GROUP BY side', params).fetchall():
        cancel_by[row['side'] if row['side'] in cancel_by else 'member'] += int(row['c'])
    return {
        'total': total,
        'cancel_requests': cancel_by['member'] + cancel_by['staff'],
        'cancel_by_member': cancel_by['member'],
        'cancel_by_staff': cancel_by['staff'],
        'open': total - done,
        'by_stage': by_stage,
        'stage_labels': STAGE_LABELS,
        'by_type': by_type,
        'completion_rate': round(done * 100 / total, 1) if total else 0,
    }


def _list_tasks(conn, member_id=None, staff_emp=None):
    """staff_emp를 넘기면 본사 화면용 — 새 소식·처리 차례 표시와 학교·담당자 필터가 더해진다."""
    staff = staff_emp is not None
    unread_sql, unread_params = _unread_expr(staff_emp) if staff else ('0', [])
    overdue_sql, overdue_params = _overdue_expr(conn)
    clauses, params = [], []
    if member_id is not None:
        clauses.append('t.member_id = ?')
        params.append(member_id)
    stage = request.args.get('stage', '').strip()
    if stage in STAGES:
        clauses.append('t.stage = ?')
        params.append(stage)
    elif stage == 'open':
        clauses.append("t.stage != 'done'")
    elif stage == 'cancel':
        clauses.append("COALESCE(t.cancel_requested_at, '') != ''")
    elif staff and stage == 'todo':
        clauses.append(STAFF_TODO_SQL)
    elif staff and stage == 'unread':
        clauses.append(unread_sql)
        params.extend(unread_params)
    elif stage == 'overdue':
        clauses.append(overdue_sql)
        params.extend(overdue_params)
    task_type = request.args.get('type', '').strip()
    if task_type in TASK_TYPES:
        clauses.append('t.task_type = ?')
        params.append(task_type)
    if staff:
        member_filter = request.args.get('member', '').strip()
        if member_filter.isdigit():
            clauses.append('t.member_id = ?')
            params.append(int(member_filter))
        assignee = request.args.get('assignee', '').strip()
        if assignee == 'me':
            clauses.append('t.assignee_emp_no = ?')
            params.append(staff_emp)
        elif assignee == 'none':
            clauses.append("COALESCE(t.assignee_emp_no, '') = ''")
    keyword = request.args.get('q', '').strip()
    if keyword:
        # 검색창 하나로 제목 · 학교명 · 학교 담당자 · 본사 담당자 · 업무 종류(예: '강사료')를 함께 찾는다.
        matches = ['t.title LIKE ?', 'm.school_name LIKE ?', 'm.name LIKE ?', "COALESCE(t.assignee_name, '') LIKE ?"]
        match_params = [f'%{keyword}%'] * 4
        type_keys = [key for key, label in TASK_TYPES.items() if keyword in label]
        if type_keys:
            matches.append(f"t.task_type IN ({', '.join('?' * len(type_keys))})")
            match_params.extend(type_keys)
        clauses.append('(' + ' OR '.join(matches) + ')')
        params.extend(match_params)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    if staff:
        select = _task_select(f', {unread_sql} AS unread, {STAFF_TODO_SQL} AS staff_turn, {overdue_sql} AS overdue')
        select_params = list(unread_params) + list(overdue_params)
        order = (" ORDER BY CASE WHEN COALESCE(t.cancel_requested_at, '') != '' THEN 0 ELSE 1 END,"
                 " CASE WHEN t.stage != 'done' AND t.priority = 'urgent' THEN 0"
                 " WHEN t.stage != 'done' AND t.priority = 'today' THEN 1 ELSE 2 END,"
                 " unread DESC, overdue DESC, staff_turn DESC,"
                 " CASE t.stage WHEN 'done' THEN 1 ELSE 0 END, t.updated_at DESC, t.id DESC")
    else:
        select, select_params = _task_select(f', {overdue_sql} AS overdue'), list(overdue_params)
        order = (" ORDER BY CASE WHEN COALESCE(t.cancel_requested_at, '') != '' THEN 0 ELSE 1 END,"
                 " CASE t.stage WHEN 'done' THEN 1 ELSE 0 END, t.updated_at DESC, t.id DESC")
    rows = conn.execute(select + where + order, select_params + params).fetchall()
    return [_task_dict(row) for row in rows]


def _load_task(conn, task_id, member_id=None):
    sql = TASK_SELECT + ' WHERE t.id = ?'
    params = [task_id]
    if member_id is not None:
        sql += ' AND t.member_id = ?'
        params.append(member_id)
    row = conn.execute(sql, params).fetchone()
    return _task_dict(row) if row else None


def _task_detail_payload(conn, task, file_endpoint):
    messages = [dict(r) for r in conn.execute(
        'SELECT * FROM billing_messages WHERE task_id = ? ORDER BY id', (task['id'],)
    ).fetchall()]
    files = [dict(r) for r in conn.execute(
        'SELECT id, message_id, author_type, original_name, size, created_at '
        'FROM billing_files WHERE task_id = ? ORDER BY id', (task['id'],)
    ).fetchall()]
    by_message = {}
    for item in files:
        item['url'] = url_for(file_endpoint, file_id=item['id'])
        by_message.setdefault(item['message_id'], []).append(item)
    for message in messages:
        message['files'] = by_message.get(message['id'], [])
    logs = [dict(r) for r in conn.execute(
        'SELECT from_stage, to_stage, actor_type, actor_name, notified, notify_result, created_at '
        'FROM billing_stage_logs WHERE task_id = ? ORDER BY id', (task['id'],)
    ).fetchall()]
    stage_times = {'start': task.get('created_at')}
    for log in logs:
        stage_times[log['to_stage']] = log['created_at']
    return {
        'status': 'success',
        'task': task,
        'messages': messages,
        'logs': logs,
        'stage_times': stage_times,
        'stages': [{'key': s, 'label': STAGE_LABELS[s]} for s in STAGES],
    }


def _save_message(conn, task_id, author_type, author_name, body, uploads,
                  upload_tokens=None, owner=None):
    """본문과 첨부를 한 메시지로 저장한다. 첨부 저장에 실패하면 이미 쓴 파일을 지운다.

    uploads       : 한 번에 올린 파일(multipart)
    upload_tokens : 조각 전송으로 미리 올려 둔 파일의 전송 번호
    owner         : (owner_type, owner_key) — 본인이 올린 전송만 첨부할 수 있다
    """
    body = str(body or '').strip()
    uploads = [f for f in uploads if f and f.filename]
    tokens = []
    for token in (upload_tokens or []):
        token = str(token or '').strip()
        if token and token not in tokens:
            tokens.append(token)
    if not body and not uploads and not tokens:
        raise ValueError('내용을 입력하거나 파일을 첨부해 주세요.')
    if len(body) > 5000:
        raise ValueError('내용은 5,000자 이내로 입력해 주세요.')
    if len(uploads) + len(tokens) > MAX_FILES_PER_POST:
        raise ValueError(f'파일은 한 번에 {MAX_FILES_PER_POST}개까지 올릴 수 있습니다.')
    for upload in uploads:
        blocked = _blocked_file_reason(original_filename(upload.filename))
        if blocked:
            raise ValueError(blocked)
        upload.stream.seek(0, 2)
        size = upload.stream.tell()
        upload.stream.seek(0)
        if size > MAX_FILE_BYTES:
            raise ValueError(f'"{original_filename(upload.filename)}" 파일이 30MB를 넘습니다.')

    staged = []
    for token in tokens:
        session_row = _load_upload(conn, token, *owner) if owner else None
        if not session_row:
            raise ValueError('첨부 파일의 전송 정보를 찾을 수 없습니다. 파일을 다시 올려 주세요.')
        if session_row['status'] != 'ready':
            raise ValueError(f'"{session_row["original_name"]}" 파일 전송이 아직 끝나지 않았습니다.')
        part = _upload_part_path(token)
        if not part.is_file() or part.stat().st_size != int(session_row['size']):
            raise ValueError(f'"{session_row["original_name"]}" 파일이 온전히 전송되지 않았습니다. 다시 올려 주세요.')
        staged.append((session_row, part))

    cur = conn.execute(
        'INSERT INTO billing_messages (task_id, author_type, author_name, body) VALUES (?, ?, ?, ?)',
        (task_id, author_type, author_name, body),
    )
    message_id = cur.lastrowid
    BILLING_UPLOADS.mkdir(parents=True, exist_ok=True)
    written = []
    try:
        for upload in uploads:
            name = original_filename(upload.filename)
            stored = encrypted_storage_name(name)
            path = BILLING_UPLOADS / stored
            size = encrypt_upload(upload, path)
            written.append(path)
            conn.execute(
                'INSERT INTO billing_files (task_id, message_id, author_type, original_name, stored_name, size) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (task_id, message_id, author_type, name, stored, size),
            )
        for session_row, part in staged:
            name = session_row['original_name']
            stored = encrypted_storage_name(name)
            path = BILLING_UPLOADS / stored
            with open(part, 'rb') as handle:
                size = encrypt_stream(handle, path)
            written.append(path)
            conn.execute(
                'INSERT INTO billing_files (task_id, message_id, author_type, original_name, stored_name, size) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (task_id, message_id, author_type, name, stored, size),
            )
            conn.execute('DELETE FROM billing_upload_sessions WHERE token=?', (session_row['token'],))
    except Exception:
        for path in written:
            delete_file(path)
        raise
    # 암호화 저장까지 끝난 조각 원본만 지운다.
    for _session_row, part in staged:
        delete_file(part)
    conn.execute('UPDATE billing_tasks SET updated_at = ? WHERE id = ?', (_now_text(), task_id))
    return message_id


def _stage_sms_text(task, to_stage, audience):
    school = task.get('school_name') or ''
    type_label = task.get('type_label') or ''
    title = str(task.get('title') or '')[:24]
    stage_label = STAGE_LABELS.get(to_stage, to_stage)
    if audience == 'member':
        guide = {
            'progress': '본사에서 업무를 접수해 진행을 시작했습니다.',
            'review': '본사 처리가 끝났습니다. 내용을 확인하고 [확인 완료]를 눌러 주세요.',
            'done': '업무가 완료 처리되었습니다.',
        }.get(to_stage, '업무 단계가 변경되었습니다.')
        return (f'[새담 청구업무] {school} {type_label}\n"{title}"\n'
                f'단계: {stage_label}\n{guide}\n{_portal_url("/")}')
    guide = {
        'done': '학교에서 확인을 마쳐 업무가 완료되었습니다.',
    }.get(to_stage, '업무 단계가 변경되었습니다.')
    return f'[새담 청구업무] {school} {type_label}\n"{title}"\n단계: {stage_label}\n{guide}'


def _staff_notify_phone(conn, task):
    # [기본설정]에서 고른 '문자 받는 사람'이 우선 — 담당자가 오후 출근·휴가일 때 다른 사람이 받게 한다.
    selected = _get_setting(conn, NOTIFY_PHONE_SETTING, '')
    if selected:
        return selected
    if task.get('assignee_emp_no'):
        row = conn.execute(
            'SELECT phone FROM users WHERE emp_no = ? LIMIT 1', (task['assignee_emp_no'],)
        ).fetchone()
        if row and row['phone']:
            return row['phone']
    return _get_setting(conn, NOTIFY_PHONE_SETTING, '')


REPLACED_TASK_MESSAGE = '학교 담당자가 교체된 업무라 더 진행할 수 없습니다. 필요 없으면 [업무 취소]로 정리해 주세요.'


def _advance_task(conn, task, actor_type, actor_name, notify, emp_no=None):
    flow = STAGE_FLOW.get(task['stage'])
    if not flow:
        raise ValueError('이미 완료된 업무입니다.')
    if task.get('member_status') == 'replaced':
        raise ValueError(REPLACED_TASK_MESSAGE)
    if task.get('cancel_requested'):
        raise ValueError('업무 취소 요청이 들어와 있어 단계를 넘길 수 없습니다. 먼저 취소 요청을 처리해 주세요.')
    to_stage, actors, _label = flow
    if actor_type not in actors:
        who = '본사' if actors == ('staff',) else '학교'
        raise PermissionError(f'이 단계는 {who}에서 넘길 수 있습니다.')

    now = _now_text()
    updates = ['stage = ?', 'updated_at = ?']
    params = [to_stage, now]
    if to_stage == 'done':
        updates.append('completed_at = ?')
        params.append(now)
    # 본사에서 처음 접수한 사람을 담당자로 지정한다.
    if actor_type == 'staff' and not task.get('assignee_emp_no') and emp_no:
        updates.extend(['assignee_emp_no = ?', 'assignee_name = ?'])
        params.extend([emp_no, actor_name])
        task['assignee_emp_no'] = emp_no
        task['assignee_name'] = actor_name
    params.append(task['id'])
    conn.execute(f'UPDATE billing_tasks SET {", ".join(updates)} WHERE id = ?', params)

    who = '본사' if actor_type == 'staff' else '학교'
    conn.execute(
        "INSERT INTO billing_messages (task_id, author_type, author_name, body) VALUES (?, 'system', ?, ?)",
        (task['id'], actor_name,
         f"{who} {actor_name}님이 [{STAGE_LABELS[task['stage']]}] → [{STAGE_LABELS[to_stage]}] 단계로 넘겼습니다."),
    )

    notified, notify_result = 0, ''
    if notify:
        if actor_type == 'staff':
            phone, audience = task.get('member_phone'), 'member'
        else:
            phone, audience = _staff_notify_phone(conn, task), 'staff'
        if not phone:
            notify_result = '문자를 받을 번호가 없어 보내지 못했습니다.'
        else:
            ok, notify_result = _send_sms(phone, _stage_sms_text(task, to_stage, audience))
            notified = 1 if ok else 0
    conn.execute(
        'INSERT INTO billing_stage_logs (task_id, from_stage, to_stage, actor_type, actor_name, notified, notify_result) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (task['id'], task['stage'], to_stage, actor_type, actor_name, notified, notify_result),
    )
    conn.commit()
    _push('billing_task', member_id=task['member_id'], task_id=task['id'], kind='stage', by=actor_type)
    return to_stage, bool(notified), notify_result


def _serve_file(conn, file_id, member_id=None):
    sql = ('SELECT f.*, t.member_id FROM billing_files f '
           'JOIN billing_tasks t ON t.id = f.task_id WHERE f.id = ?')
    row = conn.execute(sql, (file_id,)).fetchone()
    if not row or (member_id is not None and int(row['member_id']) != int(member_id)):
        abort(404)
    path = BILLING_UPLOADS / row['stored_name']
    try:
        return encrypted_response(path, row['original_name'])
    except FileNotFoundError:
        abort(404)


def _delete_task_files(conn, task_id):
    for row in conn.execute('SELECT stored_name FROM billing_files WHERE task_id = ?', (task_id,)).fetchall():
        delete_file(BILLING_UPLOADS / row['stored_name'])
    for table in ('billing_files', 'billing_messages', 'billing_stage_logs', 'billing_task_views'):
        conn.execute(f'DELETE FROM {table} WHERE task_id = ?', (task_id,))
    conn.execute('DELETE FROM billing_tasks WHERE id = ?', (task_id,))


# ---- 처리 · 삭제 기록 --------------------------------------------------------------

AUDIT_ACTION_LABELS = {
    'task_delete': '업무 삭제',
    'task_cancel': '업무 취소(요청 승인)',
    'task_cancel_replaced': '업무 취소(담당자 교체)',
    'task_cancel_contract': '업무 삭제(계약 종료)',
    'member_approve': '가입 승인',
    'member_reject': '가입 거절',
    'member_dormant': '장기 미접속 잠금',
    'member_unlock': '접속 허용(잠금 해제)',
    'member_suspend': '이용 정지',
    'member_allow': '이용 허용',
    'member_replace': '담당자 교체',
    'member_delete': '회원 삭제',
    'contract_end': '계약 종료 · 정보 삭제',
}


def _audit(conn, action, actor_type, actor_name, *, task=None, member=None, detail='', emp_no=''):
    task = task or {}
    member = member or {}
    conn.execute(
        'INSERT INTO billing_audit_logs (action, target_type, task_id, member_id, school_name, member_name, title, '
        'task_type, amount, stage, detail, actor_type, actor_name, actor_emp_no, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (action, 'task' if task else 'member', task.get('id'), task.get('member_id') or member.get('id'),
         task.get('school_name') or member.get('school_name'), task.get('member_name') or member.get('name'),
         task.get('title'), task.get('type_label') or task.get('task_type'), task.get('amount'),
         task.get('stage_label') or task.get('stage'), str(detail or '')[:2000],
         actor_type, actor_name, emp_no or '', _now_text()),
    )


def _delete_task_logged(conn, task, action, actor_type, actor_name, reason='', emp_no=''):
    """업무를 지우기 전에 누가 · 언제 · 무엇을(제목 · 금액 · 단계 · 첨부 목록) 지웠는지 기록을 남긴다."""
    files = [r['original_name'] for r in conn.execute(
        'SELECT original_name FROM billing_files WHERE task_id=? ORDER BY id', (task['id'],)
    ).fetchall()]
    messages = conn.execute(
        "SELECT COUNT(*) FROM billing_messages WHERE task_id=? AND author_type != 'system'", (task['id'],)
    ).fetchone()[0]
    parts = [f'시작 {_time12(task.get("created_at"))}', f'글 {messages}건', f'첨부 {len(files)}개']
    if files:
        parts.append('첨부: ' + ', '.join(files[:20]) + (' 외' if len(files) > 20 else ''))
    if task.get('cancel_reason'):
        parts.append(f'취소 사유: {task["cancel_reason"]}')
    if reason:
        parts.append(reason)
    _audit(conn, action, actor_type, actor_name, task=task, detail=' · '.join(parts), emp_no=emp_no)
    _delete_task_files(conn, task['id'])


# ---- 장기 미접속 잠금 · 처리 지연 · 첨부 형식 · 비밀번호 규칙 -------------------------

def _days_since(text):
    try:
        return max(0, (datetime.now() - datetime.strptime(str(text)[:19], '%Y-%m-%d %H:%M:%S')).days)
    except ValueError:
        return None


def _activity_basis_sql(alias='m'):
    """마지막으로 계정을 쓴(또는 본사가 승인 · 허용한) 시각."""
    return (f"MAX(COALESCE({alias}.created_at, ''), COALESCE({alias}.approved_at, ''), "
            f"COALESCE({alias}.last_login_at, ''), COALESCE({alias}.last_active_at, ''))")


def _lock_dormant_members(conn):
    """DORMANT_DAYS 이상 접속하지 않은 이용중 계정을 잠근다. 본사가 [접속 허용]해야 다시 쓸 수 있다."""
    limit = (datetime.now() - timedelta(days=DORMANT_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        f"SELECT * FROM billing_members m WHERE status='active' AND {_activity_basis_sql()} < ?", (limit,)
    ).fetchall()
    if not rows:
        return 0
    now = _now_text()
    for row in rows:
        conn.execute("UPDATE billing_members SET status='dormant', dormant_at=? WHERE id=?", (now, row['id']))
        _audit(conn, 'member_dormant', 'system', '자동', member=dict(row), detail=f'{DORMANT_DAYS}일 이상 미접속')
    conn.commit()
    for row in rows:
        _kick_member(row['id'])
    _push('billing_members')
    return len(rows)


def _request_unlock(conn, member):
    """잠긴 계정으로 로그인하면 본사에 접속 허용 요청을 남긴다. 문자는 하루 한 번만 보낸다."""
    if (member.get('unlock_requested_at') or '')[:10] == _now_text()[:10]:
        return '오늘 이미 본사에 접속 허용을 요청했습니다.'
    conn.execute('UPDATE billing_members SET unlock_requested_at=? WHERE id=?', (_now_text(), member['id']))
    conn.commit()
    _push('billing_members')
    phone = _get_setting(conn, NOTIFY_PHONE_SETTING, '')
    if phone:
        _send_sms(phone, f'[새담 청구업무] 장기 미접속 계정 접속 허용 요청\n{member["school_name"]} {member["name"]} 선생님\n'
                         f'본사 청구업무 > 학교회원 관리에서 허용해 주세요.')
    return '본사에 접속 허용을 요청했습니다.'


def _overdue_days(conn):
    try:
        return max(1, min(60, int(_get_setting(conn, OVERDUE_DAYS_SETTING, str(DEFAULT_OVERDUE_DAYS)))))
    except ValueError:
        return DEFAULT_OVERDUE_DAYS


def _overdue_expr(conn):
    """(SQL, params) — 완료되지 않았고, 처리 희망일이 지났거나(희망일이 없으면) 시작한 지 기준 일수가 지난 업무."""
    limit = (datetime.now() - timedelta(days=_overdue_days(conn))).strftime('%Y-%m-%d %H:%M:%S')
    return ("(t.stage != 'done' AND CASE WHEN COALESCE(t.due_date, '') != '' "
            "THEN t.due_date < ? ELSE t.created_at < ? END)"), [_now_text()[:10], limit]


def _blocked_file_reason(name):
    base = str(name or '').strip().lower().rstrip('. ')
    if '.' not in base:
        return ''
    ext = base.rsplit('.', 1)[1]
    if ext in BLOCKED_EXTENSIONS:
        return f'"{name}" 파일은 보안상 올릴 수 없는 형식(.{ext})입니다. 실행파일 · 스크립트 · 매크로 문서 · 웹페이지 파일은 첨부할 수 없습니다.'
    return ''


def _password_problem(password, confirm):
    if len(password) < 8 or not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return '비밀번호는 영문과 숫자를 섞어 8자 이상으로 입력해 주세요.'
    if password != confirm:
        return '비밀번호 확인이 일치하지 않습니다.'
    return ''


# =====================================================================
# 본사 직원용 /billing
# =====================================================================

def _staff_name():
    return str(session.get('user_name') or '본사')


@billing_bp.route('/')
@menu_permission_required(BILLING_MENU_KEY)
def staff_page():
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        schools = [r['school_name'] for r in conn.execute(
            "SELECT DISTINCT school_name FROM schools WHERE COALESCE(is_active,1)=1 "
            "AND COALESCE(school_name,'') != '' ORDER BY school_name"
        ).fetchall()]
        _lock_dormant_members(conn)
        notify_contacts, notify_selected = _notify_contacts(conn)
        contact = _contact_info(conn)
        staff_status = _staff_status(conn)
        overdue_days = _overdue_days(conn)
        preview_members = [dict(r) for r in conn.execute(
            "SELECT id, name, school_name FROM billing_members WHERE status='active' ORDER BY school_name, name"
        ).fetchall()]
    finally:
        conn.close()
    return render_template(
        'school_billing/staff.html',
        task_types=TASK_TYPES,
        stage_labels=STAGE_LABELS,
        schools=schools,
        notify_contacts=notify_contacts,
        notify_selected=notify_selected,
        contact=contact,
        staff_statuses=STAFF_STATUSES,
        staff_status=staff_status,
        overdue_days=overdue_days,
        dormant_days=DORMANT_DAYS,
        inactive_warn_days=INACTIVE_WARN_DAYS,
        preview_members=preview_members,
        sms_ready=bool(get_solapi_settings().get('sms_configured')),
        portal_login_url=url_for('school_portal.portal_login', _external=True),
    )


@billing_bp.route('/api/summary')
@menu_permission_required(BILLING_MENU_KEY)
def staff_summary():
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        data = _summary(conn)
        data['status'] = 'success'
        data['member_count'] = conn.execute(
            "SELECT COUNT(*) FROM billing_members WHERE status='active'"
        ).fetchone()[0]

        # 여러 학교 요청을 한눈에 — 처리 차례 · 새 소식 · 담당 현황
        emp = _staff_owner_key()
        unread_sql, unread_params = _unread_expr(emp)
        queue = conn.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN {STAFF_TODO_SQL} THEN 1 ELSE 0 END), 0) AS todo,
                   COALESCE(SUM(CASE WHEN {unread_sql} THEN 1 ELSE 0 END), 0) AS unread,
                   COALESCE(SUM(CASE WHEN t.stage != 'done' AND COALESCE(t.assignee_emp_no, '') = ''
                                     THEN 1 ELSE 0 END), 0) AS unassigned,
                   COALESCE(SUM(CASE WHEN t.stage != 'done' AND t.assignee_emp_no = ?
                                     THEN 1 ELSE 0 END), 0) AS mine,
                   MAX({LAST_MEMBER_SQL}) AS last_member_at
            FROM billing_tasks t
        """, unread_params + [emp]).fetchone()
        data['queue'] = dict(queue)
        data['staff_status'] = _staff_status(conn)   # 오른쪽 위 상태 상자를 다른 직원의 변경·날짜 바뀜에 맞춰 갱신
        overdue_sql, overdue_params = _overdue_expr(conn)
        data['queue']['overdue'] = conn.execute(
            f'SELECT COUNT(*) FROM billing_tasks t WHERE {overdue_sql}', overdue_params
        ).fetchone()[0]
        _lock_dormant_members(conn)
        data['queue'].update(dict(conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END), 0) AS pending, "
            "COALESCE(SUM(CASE WHEN status='dormant' AND COALESCE(unlock_requested_at, '') != '' "
            "THEN 1 ELSE 0 END), 0) AS unlock_requests FROM billing_members"
        ).fetchone()))

        # 학교별 현황 — 처리할 것·새 소식이 있는 학교가 앞에 온다.
        data['schools'] = [dict(r) for r in conn.execute(f"""
            SELECT * FROM (
                SELECT m.id AS member_id, m.school_name, m.name AS member_name, m.status AS member_status,
                       SUM(CASE WHEN t.stage != 'done' THEN 1 ELSE 0 END) AS open_count,
                       SUM(CASE WHEN {STAFF_TODO_SQL} THEN 1 ELSE 0 END) AS todo,
                       SUM(CASE WHEN {unread_sql} THEN 1 ELSE 0 END) AS unread,
                       MAX(t.updated_at) AS last_at
                FROM billing_tasks t
                JOIN billing_members m ON m.id = t.member_id
                GROUP BY m.id
            )
            ORDER BY CASE WHEN todo + unread > 0 THEN 0 ELSE 1 END,
                     CASE WHEN open_count > 0 THEN 0 ELSE 1 END, last_at DESC
        """, unread_params).fetchall()]
        return jsonify(data)
    finally:
        conn.close()


@billing_bp.route('/api/tasks')
@menu_permission_required(BILLING_MENU_KEY)
def staff_tasks():
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        return jsonify({'status': 'success', 'tasks': _list_tasks(conn, staff_emp=_staff_owner_key())})
    finally:
        conn.close()


BULK_MAX_TASKS = 100


@billing_bp.route('/api/tasks/bulk', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_bulk_tasks():
    """여러 학교 업무를 한 번에 처리한다. 업무마다 따로 처리하고, 결과를 학교별로 돌려준다.

    action: advance(다음 단계) · message(같은 안내글 등록) · assign(내 담당으로 지정)
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '')
    if action not in ('advance', 'message', 'assign'):
        return _json_error('처리할 작업을 알 수 없습니다.')
    ids = []
    for value in data.get('ids') or []:
        try:
            task_id = int(value)
        except (TypeError, ValueError):
            continue
        if task_id not in ids:
            ids.append(task_id)
    if not ids:
        return _json_error('처리할 업무를 선택해 주세요.')
    if len(ids) > BULK_MAX_TASKS:
        return _json_error(f'한 번에 {BULK_MAX_TASKS}건까지 처리할 수 있습니다.')
    body = str(data.get('body') or '').strip()
    if action == 'message' and not body:
        return _json_error('등록할 내용을 입력해 주세요.')
    from_stage = str(data.get('from_stage') or '')
    name, emp = _staff_name(), _staff_owner_key()
    if action == 'assign' and not emp:
        return _json_error('로그인 정보를 확인할 수 없어 담당자를 지정하지 못했습니다.')

    results = []
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        for task_id in ids:
            task = _load_task(conn, task_id)
            item = {'id': task_id, 'ok': False,
                    'school_name': task['school_name'] if task else '',
                    'member_name': task['member_name'] if task else '',
                    'title': task['title'] if task else ''}
            try:
                if not task:
                    raise ValueError('업무를 찾을 수 없습니다.')
                if action == 'advance':
                    # 화면을 연 사이 다른 직원이 이미 넘긴 업무는 한 단계 더 넘어가지 않게 막는다.
                    if from_stage and task['stage'] != from_stage:
                        raise ValueError(f"이미 [{task['stage_label']}] 단계라 넘기지 않았습니다.")
                    to_stage, _notified, notify_result = _advance_task(
                        conn, task, 'staff', name, bool(data.get('notify')), emp_no=emp)
                    item['message'] = f'[{STAGE_LABELS[to_stage]}] 단계로 넘김' + (
                        f' · {notify_result}' if notify_result else '')
                elif task['stage'] == 'done':
                    raise ValueError('완료된 업무라 처리하지 않았습니다.')
                elif action == 'message':
                    if task.get('member_status') == 'replaced':
                        raise ValueError('학교 담당자가 교체된 업무라 글을 등록하지 않았습니다.')
                    _save_message(conn, task_id, 'staff', name, body, [])
                    conn.commit()
                    _push('billing_task', member_id=task['member_id'], task_id=task_id, kind='message', by='staff')
                    item['message'] = '안내글 등록'
                else:
                    conn.execute('UPDATE billing_tasks SET assignee_emp_no = ?, assignee_name = ? WHERE id = ?',
                                 (emp, name, task_id))
                    conn.commit()
                    _push('billing_task', task_id=task_id, kind='assign', by='staff')
                    item['message'] = f'담당자 {name} 지정'
                item['ok'] = True
            except (ValueError, PermissionError) as exc:
                conn.rollback()
                item['message'] = str(exc)
            results.append(item)
    finally:
        conn.close()
    done = sum(1 for r in results if r['ok'])
    message = f'{len(results)}건 중 {done}건을 처리했습니다.'
    if done < len(results):
        message += f' ({len(results) - done}건은 처리하지 못했습니다)'
    return jsonify({'status': 'success', 'message': message, 'results': results})


@billing_bp.route('/api/tasks/<int:task_id>')
@menu_permission_required(BILLING_MENU_KEY)
def staff_task_detail(task_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        payload = _task_detail_payload(conn, task, 'school_billing.staff_file')
        payload['viewer'] = 'staff'
        emp = _staff_owner_key()
        if emp:
            conn.execute(
                'INSERT INTO billing_task_views (task_id, emp_no, seen_at) VALUES (?, ?, ?) '
                'ON CONFLICT(task_id, emp_no) DO UPDATE SET seen_at = excluded.seen_at',
                (task_id, emp, _now_text()),
            )
            conn.commit()
        return jsonify(payload)
    finally:
        conn.close()


@billing_bp.route('/api/tasks/<int:task_id>/messages', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_post_message(task_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        if task['stage'] == 'done':
            return _json_error('완료된 업무에는 더 이상 글을 남길 수 없습니다.')
        if task.get('member_status') == 'replaced':
            return _json_error(REPLACED_TASK_MESSAGE)
        try:
            _save_message(conn, task_id, 'staff', _staff_name(), request.form.get('body'),
                          request.files.getlist('files'), request.form.getlist('upload_ids'),
                          ('staff', _staff_owner_key()))
        except ValueError as exc:
            conn.rollback()
            return _json_error(str(exc))
        conn.commit()
        _push('billing_task', member_id=task['member_id'], task_id=task_id, kind='message', by='staff')
        return jsonify({'status': 'success', 'message': '등록했습니다.'})
    finally:
        conn.close()


@billing_bp.route('/api/tasks/<int:task_id>/advance', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_advance(task_id):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        try:
            to_stage, notified, notify_result = _advance_task(
                conn, task, 'staff', _staff_name(), bool(data.get('notify')),
                emp_no=str(session.get('emp_no') or ''),
            )
        except PermissionError as exc:
            return _json_error(str(exc), 403)
        except ValueError as exc:
            return _json_error(str(exc))
        return jsonify({
            'status': 'success',
            'message': f'[{STAGE_LABELS[to_stage]}] 단계로 넘겼습니다.',
            'notified': notified,
            'notify_result': notify_result,
        })
    finally:
        conn.close()


@billing_bp.route('/api/tasks/<int:task_id>/delete', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_delete_task(task_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        reason = str((request.get_json(silent=True) or {}).get('reason') or '').strip()[:300]
        _delete_task_logged(conn, task, 'task_delete', 'staff', _staff_name(),
                            reason=f'삭제 사유: {reason}' if reason else '', emp_no=_staff_owner_key())
        conn.commit()
        _push('billing_task', member_id=task['member_id'], task_id=task_id, kind='deleted', by='staff')
        return jsonify({'status': 'success',
                        'message': '업무와 첨부파일을 삭제했습니다. 삭제 기록은 [학교관리 > 처리 · 삭제 기록]에 남습니다.'})
    finally:
        conn.close()


@billing_bp.route('/files/<int:file_id>')
@menu_permission_required(BILLING_MENU_KEY)
def staff_file(file_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        return _serve_file(conn, file_id)
    finally:
        conn.close()


# ---- 학교회원 관리 ---------------------------------------------------------

@billing_bp.route('/api/members')
@menu_permission_required(BILLING_MENU_KEY)
def staff_members():
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _lock_dormant_members(conn)
        members = []
        for row in conn.execute(f"""
            SELECT m.id, m.login_id, m.name, m.school_name, m.phone, m.tel, m.email, m.status,
                   m.created_at, m.last_login_at, m.approved_at, m.unlock_requested_at,
                   {_activity_basis_sql()} AS last_seen_at,
                   (SELECT COUNT(*) FROM billing_tasks t WHERE t.member_id = m.id) AS task_count,
                   (SELECT COUNT(*) FROM billing_tasks t WHERE t.member_id = m.id AND t.stage != 'done') AS open_count
            FROM billing_members m
            ORDER BY CASE m.status WHEN 'pending' THEN 0 WHEN 'dormant' THEN 1 ELSE 2 END, m.school_name, m.name
        """).fetchall():
            item = dict(row)
            try:
                item['phone_display'] = format_phone(item['phone'])
            except ValueError:
                item['phone_display'] = item['phone']
            item['inactive_days'] = _days_since(item['last_seen_at'])
            members.append(item)
        invites = [dict(r) for r in conn.execute("""
            SELECT id, email, school_name, status, sent_by, created_at, expires_at, used_at
            FROM billing_invites ORDER BY id DESC LIMIT 200
        """).fetchall()]
        now = _now_text()
        for invite in invites:
            if invite['status'] == 'sent' and invite['expires_at'] and invite['expires_at'] < now:
                invite['status'] = 'expired'
        return jsonify({'status': 'success', 'members': members, 'invites': invites})
    finally:
        conn.close()


@billing_bp.route('/api/members/<int:member_id>/status', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_member_status(member_id):
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in ('active', 'suspended'):
        return _json_error('상태 값이 올바르지 않습니다.')
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        row = conn.execute('SELECT * FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not row:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        if row['status'] == 'replaced':
            return _json_error('담당자 교체로 종료된 계정은 다시 허용할 수 없습니다. 필요하면 새로 초대해 주세요.')
        if row['status'] == 'pending':
            return _json_error('가입 승인 대기 중인 계정입니다. [승인] 버튼으로 처리해 주세요.')
        if status == 'active':
            # 허용한 날부터 미접속 기간을 다시 센다.
            conn.execute(
                "UPDATE billing_members SET status='active', approved_at=?, dormant_at=NULL, unlock_requested_at=NULL "
                "WHERE id=?", (_now_text(), member_id),
            )
            action = 'member_unlock' if row['status'] == 'dormant' else 'member_allow'
        else:
            conn.execute("UPDATE billing_members SET status='suspended' WHERE id=?", (member_id,))
            action = 'member_suspend'
        _audit(conn, action, 'staff', _staff_name(), member=dict(row), emp_no=_staff_owner_key())
        conn.commit()
        if action == 'member_suspend':
            _kick_member(member_id)
        _push('billing_members')
        label = '이용을 다시 허용했습니다.' if status == 'active' else '이용을 정지했습니다. 이 회원은 로그인할 수 없습니다.'
        return jsonify({'status': 'success', 'message': label})
    finally:
        conn.close()


@billing_bp.route('/api/members/<int:member_id>/delete', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_member_delete(member_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        member = conn.execute('SELECT * FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not member:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        task_count = conn.execute(
            'SELECT COUNT(*) FROM billing_tasks WHERE member_id=?', (member_id,)
        ).fetchone()[0]
        if task_count:
            return _json_error(
                f'이 회원이 진행한 업무가 {task_count}건 있어 삭제할 수 없습니다. 대신 [정지]를 사용해 주세요.', 409)
        _audit(conn, 'member_delete', 'staff', _staff_name(), member=dict(member), emp_no=_staff_owner_key())
        conn.execute('DELETE FROM billing_recovery_codes WHERE member_id=?', (member_id,))
        conn.execute('DELETE FROM billing_members WHERE id=?', (member_id,))
        conn.commit()
        _kick_member(member_id)
        _push('billing_members')
        return jsonify({'status': 'success', 'message': '회원을 삭제했습니다.'})
    finally:
        conn.close()


@billing_bp.route('/api/senders')
@menu_permission_required(BILLING_MENU_KEY)
def staff_senders():
    from .payroll import _ensure_sender_schema, _payroll_sender_dict

    conn = get_db()
    try:
        _ensure_sender_schema(conn)
        rows = conn.execute("""
            SELECT * FROM ai_mail_senders WHERE owner_emp_no=? AND is_active=1
            ORDER BY CASE WHEN last_test_status='success' THEN 0 ELSE 1 END, updated_at DESC, id DESC
        """, (str(session.get('emp_no') or ''),)).fetchall()
        return jsonify({'status': 'success', 'senders': [_payroll_sender_dict(r) for r in rows]})
    finally:
        conn.close()


def _send_invite_mail(sender, to_email, school_name, link, staff_name):
    from .payroll import _sender_from_header, _smtp_login_for_sender, _verify_smtp_sender

    subject = '[새담] 청구업무 학교회원 가입 안내'
    school_text = f'{school_name} ' if school_name else ''
    plain = (
        f'안녕하세요. 사단법인 새담청소년교육문화원입니다.\n\n'
        f'{school_text}담당 선생님께 새담 청구업무 학교회원 가입을 안내드립니다.\n'
        f'강사료·교구재비 청구와 자료요청을 전화 대신 웹에서 편하게 처리하실 수 있습니다.\n\n'
        f'아래 주소에서 가입해 주세요. (링크는 {INVITE_VALID_DAYS}일 동안 유효합니다)\n{link}\n\n'
        f'가입할 때 휴대폰으로 인증번호가 발송됩니다.\n\n안내: 새담 본사 {staff_name}'
    )
    html = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-family:sans-serif;max-width:640px;margin:0 auto;border:1px solid #dde5ef;border-radius:14px;background:#fff;border-collapse:separate;overflow:hidden">
      <tr><td style="padding:26px 30px;background:#1f3a5f;color:#fff">
        <div style="font-size:13px;opacity:.8">사단법인 새담청소년교육문화원</div>
        <div style="font-size:21px;font-weight:bold;margin-top:6px">청구업무 학교회원 가입 안내</div>
      </td></tr>
      <tr><td style="padding:26px 30px;color:#334155;font-size:15px;line-height:1.75">
        {escape(school_text)}담당 선생님, 안녕하세요.<br>
        강사료·교구재비 청구와 자료요청을 <b>전화 대신 웹에서</b> 편하게 처리하실 수 있도록
        새담 청구업무 학교회원 가입을 안내드립니다.
        <div style="margin:22px 0;text-align:center">
          <a href="{escape(link)}" target="_blank" style="display:inline-block;background:#2563eb;color:#fff;padding:14px 30px;border-radius:9px;text-decoration:none;font-weight:bold">학교회원 가입하기</a>
        </div>
        <div style="font-size:13px;color:#64748b">
          · 링크는 {INVITE_VALID_DAYS}일 동안 유효합니다.<br>
          · 가입할 때 휴대폰으로 인증번호가 발송됩니다.<br>
          · 안내: 새담 본사 {escape(staff_name)}
        </div>
      </td></tr>
    </table>"""
    msg = MIMEMultipart('alternative')
    msg['From'] = _sender_from_header(dict(sender))
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(plain, 'plain', 'utf-8'))
    msg.attach(MIMEText(html, 'html', 'utf-8'))
    server = _smtp_login_for_sender(dict(sender))
    try:
        _verify_smtp_sender(server, dict(sender))
        server.sendmail(str(sender['email']).strip().lower(), to_email, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            try:
                server.close()
            except Exception:
                pass


def _issue_invite(raw_email, school_name, raw_sender_id, replaces=None):
    """가입 초대를 만들고 메일을 보낸다. 돌려주는 값: (오류 응답 또는 None, 이메일)

    replaces: 담당자 교체일 때 전 담당자 회원 정보 — 새 담당자는 같은 학교명을 이어받는다.
    """
    from .payroll import _ensure_sender_schema

    email = _normalize_email(raw_email)
    school_name = str(school_name or '').strip()[:60]
    if not EMAIL_PATTERN.match(email):
        return _json_error('올바른 이메일 주소를 입력해 주세요.'), email
    if not school_name:
        return _json_error('학교명을 입력해 주세요.'), email
    try:
        sender_id = int(raw_sender_id)
    except (TypeError, ValueError):
        return _json_error('초대 메일을 보낼 발송계정을 선택해 주세요.'), email

    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _ensure_sender_schema(conn)
        sender = conn.execute(
            'SELECT * FROM ai_mail_senders WHERE id=? AND owner_emp_no=? AND is_active=1',
            (sender_id, str(session.get('emp_no') or '')),
        ).fetchone()
        if not sender:
            return _json_error('선택한 발송계정을 사용할 수 없습니다. 계정 상태를 확인해 주세요.'), email
        if conn.execute('SELECT id FROM billing_members WHERE LOWER(email)=?', (email,)).fetchone():
            return _json_error('이미 가입한 학교회원의 이메일입니다.', 409), email
        if replaces:
            school_id = replaces.get('school_id')
        else:
            school_row = conn.execute(
                'SELECT id FROM schools WHERE school_name=? ORDER BY year DESC, id DESC LIMIT 1', (school_name,)
            ).fetchone()
            school_id = school_row['id'] if school_row else None
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now() + timedelta(days=INVITE_VALID_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        # 같은 이메일로 보낸 이전 초대, 같은 담당자 교체로 보낸 이전 초대는 새 초대로 대체한다.
        conn.execute(
            "UPDATE billing_invites SET status='revoked' WHERE LOWER(email)=? AND status='sent'", (email,)
        )
        if replaces:
            conn.execute(
                "UPDATE billing_invites SET status='revoked' WHERE replaces_member_id=? AND status='sent'",
                (replaces['id'],),
            )
        cur = conn.execute(
            'INSERT INTO billing_invites (token_hash, email, school_name, school_id, status, sent_by, expires_at, '
            "replaces_member_id) VALUES (?, ?, ?, ?, 'sent', ?, ?, ?)",
            (_token_hash(token), email, school_name, school_id, _staff_name(), expires_at,
             replaces['id'] if replaces else None),
        )
        invite_id = cur.lastrowid
        conn.commit()
        sender = dict(sender)
    finally:
        conn.close()

    link = url_for('school_portal.portal_join', token=token, _external=True)
    try:
        _send_invite_mail(sender, email, school_name, link, _staff_name())
    except (smtplib.SMTPException, OSError, RuntimeError, ValueError) as exc:
        fail = get_db()
        try:
            fail.execute("UPDATE billing_invites SET status='failed' WHERE id=?", (invite_id,))
            fail.commit()
        finally:
            fail.close()
        return _json_error(f'초대 메일 발송에 실패했습니다: {exc}', 502), email
    return None, email


@billing_bp.route('/api/invites', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_send_invite():
    data = request.get_json(silent=True) or {}
    error, email = _issue_invite(data.get('email'), data.get('school_name'), data.get('sender_id'))
    if error:
        return error
    return jsonify({'status': 'success', 'message': f'{email} 로 가입 초대 메일을 보냈습니다.'})


# ---- 학교관리 · 담당자 교체 -------------------------------------------------

def _has_successor(conn, member_id):
    return bool(conn.execute(
        'SELECT 1 FROM billing_members n JOIN billing_invites i ON i.id = n.invite_id '
        'WHERE i.replaces_member_id = ? LIMIT 1', (member_id,)
    ).fetchone())


@billing_bp.route('/api/members/<int:member_id>/replace', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_replace_member(member_id):
    """학교 담당자 교체 — 새 담당자에게 초대 메일을 보내고 전 담당자 계정은 즉시 정지한다.

    업무는 전 담당자 계정에 그대로 남으므로 새 담당자에게는 지난 기록·진행 중 업무가 보이지 않는다(개인정보 보호).
    """
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        member = conn.execute('SELECT * FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not member:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        if member['status'] == 'replaced' and _has_successor(conn, member_id):
            return _json_error('이미 새 담당자가 가입했습니다. 새 담당자 계정에서 [담당자 교체]를 진행해 주세요.')
        member = dict(member)
    finally:
        conn.close()

    error, email = _issue_invite(data.get('email'), member['school_name'], data.get('sender_id'), replaces=member)
    if error:
        return error

    conn = get_db()
    try:
        was_replaced = member['status'] == 'replaced'
        conn.execute(
            "UPDATE billing_members SET status='replaced', replaced_at=COALESCE(replaced_at, ?) WHERE id=?",
            (_now_text(), member_id),
        )
        _audit(conn, 'member_replace', 'staff', _staff_name(), member=member,
               detail=f'새 담당자 초대 {email}', emp_no=_staff_owner_key())
        conn.commit()
        _kick_member(member_id)
        _push('billing_members')
        open_count = conn.execute(
            "SELECT COUNT(*) FROM billing_tasks WHERE member_id=? AND stage != 'done'", (member_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    message = f'{email} 로 새 담당자 가입 초대 메일을 보냈습니다.'
    if not was_replaced:
        message += f'\n전 담당자 {member["name"]} 선생님 계정은 바로 정지했습니다.'
    if open_count:
        message += f'\n전 담당자가 완료하지 못한 업무 {open_count}건은 [학교관리]에서 취소할 수 있습니다.'
    return jsonify({'status': 'success', 'message': message})


@billing_bp.route('/api/schools')
@menu_permission_required(BILLING_MENU_KEY)
def staff_schools():
    """학교관리 — 학교별 담당자(현재·정지·교체됨), 초대 대기, 업무 처리 현황, 본사 담당."""
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _lock_dormant_members(conn)
        members = [dict(r) for r in conn.execute(f"""
            SELECT m.id, m.name, m.school_name, m.phone, m.email, m.status, m.created_at, m.last_login_at,
                   m.replaced_at, m.unlock_requested_at, {_activity_basis_sql()} AS last_seen_at,
                   COUNT(t.id) AS total,
                   COALESCE(SUM(CASE WHEN t.stage != 'done' THEN 1 ELSE 0 END), 0) AS open_count,
                   COALESCE(SUM(CASE WHEN t.stage = 'done' THEN 1 ELSE 0 END), 0) AS done_count,
                   COALESCE(SUM(CASE WHEN t.id IS NOT NULL AND {STAFF_TODO_SQL} THEN 1 ELSE 0 END), 0) AS todo,
                   MAX(t.updated_at) AS last_task_at,
                   EXISTS(SELECT 1 FROM billing_members n JOIN billing_invites i ON i.id = n.invite_id
                          WHERE i.replaces_member_id = m.id) AS has_successor
            FROM billing_members m
            LEFT JOIN billing_tasks t ON t.member_id = m.id
            GROUP BY m.id
            ORDER BY m.school_name, m.id
        """).fetchall()]
        assignees = conn.execute("""
            SELECT m.school_name, COALESCE(t.assignee_name, '') AS name, COUNT(*) AS c
            FROM billing_tasks t JOIN billing_members m ON m.id = t.member_id
            WHERE t.stage != 'done'
            GROUP BY m.school_name, COALESCE(t.assignee_name, '')
        """).fetchall()
        invites = conn.execute(
            "SELECT id, email, school_name, created_at, expires_at, replaces_member_id "
            "FROM billing_invites WHERE status = 'sent' ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()

    now = _now_text()
    schools = {}

    def school_of(name):
        name = name or '(학교명 없음)'
        return schools.setdefault(name, {
            'school_name': name, 'active': [], 'suspended': [], 'replaced': [], 'invites': [], 'assignees': [],
            'total': 0, 'open_count': 0, 'done_count': 0, 'todo': 0, 'orphan_open': 0,
        })

    for member in members:
        try:
            member['phone_display'] = format_phone(member['phone']) or member['phone']
        except ValueError:
            member['phone_display'] = member['phone']
        member['has_successor'] = bool(member['has_successor'])
        member['inactive_days'] = _days_since(member['last_seen_at'])
        entry = school_of(member['school_name'])
        entry[member['status'] if member['status'] in ('active', 'replaced') else 'suspended'].append(member)
        for key in ('total', 'open_count', 'done_count', 'todo'):
            entry[key] += int(member[key] or 0)
        if member['status'] == 'replaced':
            entry['orphan_open'] += int(member['open_count'] or 0)
    for row in assignees:
        school_of(row['school_name'])['assignees'].append({'name': row['name'], 'count': row['c']})
    for row in invites:
        item = dict(row)
        item['expired'] = bool(item['expires_at'] and item['expires_at'] < now)
        school_of(item['school_name'])['invites'].append(item)
    result = sorted(schools.values(), key=lambda s: s['school_name'])
    for entry in result:
        entry['replaced'].sort(key=lambda m: m.get('replaced_at') or '', reverse=True)
    return jsonify({'status': 'success', 'schools': result})


@billing_bp.route('/api/tasks/<int:task_id>/replaced-cancel', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_cancel_replaced_task(task_id):
    """담당자가 교체된 학교의 미완료 업무는 학교 승인 없이 본사가 바로 취소(삭제)한다."""
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        if task.get('member_status') != 'replaced':
            return _json_error('담당자가 교체된 학교의 업무만 본사에서 바로 취소할 수 있습니다.')
        if task['stage'] == 'done':
            return _json_error('완료된 업무는 취소할 수 없습니다.')
        _delete_task_logged(conn, task, 'task_cancel_replaced', 'staff', _staff_name(), emp_no=_staff_owner_key())
        conn.commit()
        _push('billing_task', task_id=task_id, kind='deleted', by='staff')
        return jsonify({'status': 'success', 'message': '업무를 취소했습니다. 업무와 첨부파일을 삭제했습니다.'})
    finally:
        conn.close()


@billing_bp.route('/api/members/<int:member_id>/cancel-open', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_cancel_replaced_open(member_id):
    """교체된 전 담당자가 완료하지 못한 업무를 한 번에 취소(삭제)한다."""
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        member = conn.execute('SELECT status FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not member:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        if member['status'] != 'replaced':
            return _json_error('담당자가 교체된 전 담당자의 업무만 한 번에 취소할 수 있습니다.')
        ids = [r['id'] for r in conn.execute(
            "SELECT id FROM billing_tasks WHERE member_id=? AND stage != 'done'", (member_id,)
        ).fetchall()]
        for task_id in ids:
            _delete_task_logged(conn, _load_task(conn, task_id), 'task_cancel_replaced', 'staff', _staff_name(),
                                reason='전 담당자 미완료 업무 일괄 취소', emp_no=_staff_owner_key())
        conn.commit()
        _push('billing_task', kind='deleted', by='staff')
        return jsonify({'status': 'success', 'message': f'미완료 업무 {len(ids)}건을 취소했습니다. 업무와 첨부파일을 삭제했습니다.'})
    finally:
        conn.close()


# ---- 가입 승인 · 접속 허용 · 계약 종료 · 처리 기록 · 처리 지연 기준 ----------------------

@billing_bp.route('/api/members/<int:member_id>/approve', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_member_approve(member_id):
    """가입 승인(승인 대기) 또는 장기 미접속 잠금 해제 — 승인한 날부터 미접속 기간을 다시 센다."""
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        row = conn.execute('SELECT * FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not row:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        if row['status'] not in ('pending', 'dormant'):
            return _json_error('승인할 계정이 아닙니다. 새로고침한 뒤 다시 확인해 주세요.')
        action = 'member_approve' if row['status'] == 'pending' else 'member_unlock'
        conn.execute(
            "UPDATE billing_members SET status='active', approved_at=?, dormant_at=NULL, unlock_requested_at=NULL "
            "WHERE id=?", (_now_text(), member_id),
        )
        _audit(conn, action, 'staff', _staff_name(), member=dict(row), emp_no=_staff_owner_key())
        conn.commit()
    finally:
        conn.close()
    _push('billing_members')
    word = '가입이 승인' if action == 'member_approve' else '접속이 다시 허용'
    _ok, sms_result = _send_sms(
        row['phone'], f'[새담 청구업무] {row["school_name"]} {row["name"]} 선생님, 학교회원 {word}되었습니다.\n'
                      f'{_portal_url("/login")}')
    return jsonify({'status': 'success', 'message': f'{row["name"]} 선생님 {word}되었습니다.\n{sms_result}'})


@billing_bp.route('/api/members/<int:member_id>/reject', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_member_reject(member_id):
    """가입 거절 — 가입 정보는 지우고 거절 기록만 남긴다."""
    reason = str((request.get_json(silent=True) or {}).get('reason') or '').strip()[:200]
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        row = conn.execute('SELECT * FROM billing_members WHERE id=?', (member_id,)).fetchone()
        if not row:
            return _json_error('회원을 찾을 수 없습니다.', 404)
        if row['status'] != 'pending':
            return _json_error('승인 대기 중인 계정만 거절할 수 있습니다.')
        _audit(conn, 'member_reject', 'staff', _staff_name(), member=dict(row),
               detail=f'거절 사유: {reason}' if reason else '', emp_no=_staff_owner_key())
        conn.execute('DELETE FROM billing_recovery_codes WHERE member_id=?', (member_id,))
        conn.execute('DELETE FROM billing_members WHERE id=?', (member_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': '가입을 거절하고 가입 정보를 삭제했습니다. 거절 기록은 남겨 두었습니다.'})


@billing_bp.route('/api/schools/contract-end', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_school_contract_end():
    """학교와의 계약 종료 — 가입 때 안내한 대로 그 학교의 회원 정보 · 업무 · 대화 · 첨부 · 초대 기록을 모두 지운다.

    누가 언제 무엇을 지웠는지는 처리 기록에 남긴다.
    """
    data = request.get_json(silent=True) or {}
    school_name = str(data.get('school_name') or '').strip()
    if not school_name or str(data.get('confirm') or '').strip() != school_name:
        return _json_error('확인을 위해 학교명을 정확히 입력해 주세요.')
    name, emp = _staff_name(), _staff_owner_key()
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        members = [dict(r) for r in conn.execute(
            'SELECT * FROM billing_members WHERE school_name=?', (school_name,)
        ).fetchall()]
        invite_ids = [r['id'] for r in conn.execute(
            'SELECT id FROM billing_invites WHERE school_name=?', (school_name,)
        ).fetchall()]
        if not members and not invite_ids:
            return _json_error('해당 학교의 회원이나 초대 기록을 찾을 수 없습니다.', 404)
        task_count = 0
        for member in members:
            for row in conn.execute('SELECT id FROM billing_tasks WHERE member_id=?', (member['id'],)).fetchall():
                _delete_task_logged(conn, _load_task(conn, row['id']), 'task_cancel_contract', 'staff', name,
                                    reason='학교 계약 종료', emp_no=emp)
                task_count += 1
            conn.execute('DELETE FROM billing_recovery_codes WHERE member_id=?', (member['id'],))
            conn.execute('DELETE FROM billing_members WHERE id=?', (member['id'],))
        for invite_id in invite_ids:
            conn.execute('DELETE FROM billing_otps WHERE invite_id=?', (invite_id,))
        conn.execute('DELETE FROM billing_invites WHERE school_name=?', (school_name,))
        conn.execute('DELETE FROM billing_emergency_logs WHERE school_name=?', (school_name,))
        _audit(conn, 'contract_end', 'staff', name,
               member={'school_name': school_name, 'name': ', '.join(m['name'] for m in members)},
               detail=f'학교회원 {len(members)}명 · 업무 {task_count}건 · 초대 {len(invite_ids)}건 삭제', emp_no=emp)
        conn.commit()
    finally:
        conn.close()
    for member in members:
        _kick_member(member['id'])
    _push('billing_task', kind='deleted', by='staff')
    _push('billing_members')
    return jsonify({
        'status': 'success',
        'message': f'[{school_name}] 계약 종료 처리를 마쳤습니다.\n'
                   f'학교회원 {len(members)}명 · 업무 {task_count}건 · 초대 {len(invite_ids)}건을 삭제하고 처리 기록을 남겼습니다.',
    })


@billing_bp.route('/api/audit-logs')
@menu_permission_required(BILLING_MENU_KEY)
def staff_audit_logs():
    clauses, params = [], []
    kind = request.args.get('kind', '').strip()
    if kind in ('task', 'member'):
        clauses.append('target_type = ?')
        params.append(kind)
    keyword = request.args.get('q', '').strip()
    if keyword:
        clauses.append('(school_name LIKE ? OR member_name LIKE ? OR title LIKE ? OR actor_name LIKE ? OR detail LIKE ?)')
        params.extend([f'%{keyword}%'] * 5)
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        logs = [dict(r) for r in conn.execute(
            f'SELECT * FROM billing_audit_logs {where} ORDER BY id DESC LIMIT 300', params
        ).fetchall()]
    finally:
        conn.close()
    for log in logs:
        log['action_label'] = AUDIT_ACTION_LABELS.get(log['action'], log['action'])
    return jsonify({'status': 'success', 'logs': logs})


@billing_bp.route('/api/overdue-days', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_save_overdue_days():
    try:
        days = int((request.get_json(silent=True) or {}).get('days'))
    except (TypeError, ValueError):
        return _json_error('일수를 숫자로 입력해 주세요.')
    if not 1 <= days <= 60:
        return _json_error('처리 지연 기준은 1일에서 60일 사이로 정해 주세요.')
    conn = get_db()
    try:
        _set_setting(conn, OVERDUE_DAYS_SETTING, str(days))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'days': days,
                    'message': f'시작한 지 {days}일이 지나도 완료되지 않은 업무를 [처리 지연]으로 표시합니다.'})


@billing_bp.route('/api/invites/<int:invite_id>/revoke', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_revoke_invite(invite_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        cur = conn.execute(
            "UPDATE billing_invites SET status='revoked' WHERE id=? AND status='sent'", (invite_id,)
        )
        conn.commit()
        if not cur.rowcount:
            return _json_error('취소할 수 있는 초대가 아닙니다.')
        return jsonify({'status': 'success', 'message': '초대를 취소했습니다. 링크가 더 이상 열리지 않습니다.'})
    finally:
        conn.close()


def _notify_contacts(conn):
    """문자 알림 받을 연락처 3칸과 지금 받는 칸 번호(-1이면 없음)."""
    try:
        saved = json.loads(_get_setting(conn, NOTIFY_CONTACTS_SETTING, '') or '[]')
    except ValueError:
        saved = []
    if not isinstance(saved, list):
        saved = []
    contacts = []
    for index in range(NOTIFY_CONTACT_SLOTS):
        item = saved[index] if index < len(saved) and isinstance(saved[index], dict) else {}
        contacts.append({'label': str(item.get('label') or '')[:20], 'phone': str(item.get('phone') or '')})
    current = _get_setting(conn, NOTIFY_PHONE_SETTING, '')
    if not saved and current:
        contacts[0] = {'label': '', 'phone': current}   # 예전 단일 번호 설정을 첫 칸으로 보여준다
    selected = next((i for i, c in enumerate(contacts) if c['phone'] and c['phone'] == current), -1)
    for contact in contacts:
        try:
            contact['phone_display'] = format_phone(contact['phone']) if contact['phone'] else ''
        except ValueError:
            contact['phone_display'] = contact['phone']
    return contacts, selected


def _notify_payload(conn, message):
    contacts, selected = _notify_contacts(conn)
    return jsonify({'status': 'success', 'message': message, 'contacts': contacts, 'selected': selected})


def _contact_title(contacts, index):
    return contacts[index]['label'] or f'{index + 1}번 연락처'


@billing_bp.route('/api/notify-contacts', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_save_notify_contacts():
    data = request.get_json(silent=True) or {}
    rows = data.get('contacts') if isinstance(data.get('contacts'), list) else []
    contacts = []
    for index in range(NOTIFY_CONTACT_SLOTS):
        item = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
        label = str(item.get('label') or '').strip()[:20]
        raw = str(item.get('phone') or '').strip()
        try:
            phone = normalize_phone(raw) if raw else ''
        except ValueError as exc:
            return _json_error(f'{index + 1}번 연락처: {exc}')
        if label and not phone:
            return _json_error(f'{index + 1}번 연락처({label})의 휴대폰번호를 입력해 주세요.')
        contacts.append({'label': label, 'phone': phone})
    if not any(c['phone'] for c in contacts):
        return _json_error('문자 알림 받을 휴대폰번호를 1개 이상 입력해 주세요.')
    try:
        selected = int(data.get('selected'))
    except (TypeError, ValueError):
        selected = -1
    if selected not in range(NOTIFY_CONTACT_SLOTS) or not contacts[selected]['phone']:
        selected = next(i for i, c in enumerate(contacts) if c['phone'])
    conn = get_db()
    try:
        _set_setting(conn, NOTIFY_CONTACTS_SETTING, json.dumps(contacts, ensure_ascii=False))
        _set_setting(conn, NOTIFY_PHONE_SETTING, contacts[selected]['phone'])
        conn.commit()
        return _notify_payload(conn, f'연락처를 저장했습니다. 지금은 {_contact_title(contacts, selected)}에게 문자 알림이 갑니다.')
    finally:
        conn.close()


@billing_bp.route('/api/notify-contacts/select', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_select_notify_contact():
    """오후 출근·휴가 등으로 문자 받을 사람을 바로 바꾼다."""
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get('index'))
    except (TypeError, ValueError):
        index = -1
    conn = get_db()
    try:
        contacts, _selected = _notify_contacts(conn)
        if index not in range(NOTIFY_CONTACT_SLOTS) or not contacts[index]['phone']:
            return _json_error('번호가 저장된 연락처만 선택할 수 있습니다. 번호를 입력하고 [연락처 저장]을 먼저 눌러 주세요.')
        _set_setting(conn, NOTIFY_CONTACTS_SETTING,
                     json.dumps([{'label': c['label'], 'phone': c['phone']} for c in contacts], ensure_ascii=False))
        _set_setting(conn, NOTIFY_PHONE_SETTING, contacts[index]['phone'])
        conn.commit()
        return _notify_payload(
            conn, f'지금부터 {_contact_title(contacts, index)}({contacts[index]["phone_display"]})에게 문자 알림이 갑니다.')
    finally:
        conn.close()


# =====================================================================
# 학교회원 포털 /portal
# =====================================================================

MEMBER_SESSION_KEYS = ('billing_member_id', 'billing_member_name', 'billing_school_name', 'billing_preview_by',
                       'billing_last_seen')


def _portal_csrf():
    token = session.get('portal_csrf')
    if not token:
        token = secrets.token_urlsafe(24)
        session['portal_csrf'] = token
    return token


def _portal_csrf_ok():
    expected = str(session.get('portal_csrf') or '')
    supplied = request.headers.get('X-Portal-CSRF') or request.form.get('csrf_token') or ''
    return bool(expected) and hmac.compare_digest(expected, str(supplied))


def _current_member(conn):
    member_id = session.get('billing_member_id')
    if not member_id:
        return None
    _lock_dormant_members(conn)
    row = conn.execute(
        "SELECT * FROM billing_members WHERE id=? AND status='active'", (member_id,)
    ).fetchone()
    if not row:
        for key in MEMBER_SESSION_KEYS:
            session.pop(key, None)
        return None
    member = dict(row)
    # 로그인한 채로 계속 쓰는 경우도 '접속'으로 친다(한 시간에 한 번만 기록, 본사 미리보기는 제외).
    now = _now_text()
    if not session.get('billing_preview_by') and (member.get('last_active_at') or '')[:13] != now[:13]:
        conn.execute('UPDATE billing_members SET last_active_at=? WHERE id=?', (now, member['id']))
        conn.commit()
        member['last_active_at'] = now
    return member


def member_required(view):
    """학교회원 로그인 + (POST일 때) CSRF 확인."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        wants_json = request.path.startswith('/portal/api/') or request.is_json
        conn = get_db()
        try:
            ensure_billing_schema(conn)
            member = _current_member(conn)
        finally:
            conn.close()
        # 6시간 동안 아무 작업이 없으면 자동 로그아웃. 화면이 스스로 보내는 주기적 확인(X-Portal-Background)은 작업으로 치지 않는다.
        if member:
            now = time.time()
            last_seen = session.get('billing_last_seen')
            if last_seen and now - float(last_seen) > MEMBER_IDLE_SECONDS:
                for key in MEMBER_SESSION_KEYS:
                    session.pop(key, None)
                login_url = url_for('school_portal.portal_login', expired=1)
                if wants_json:
                    return _json_error('오랫동안 사용하지 않아 자동 로그아웃되었습니다. 다시 로그인해 주세요.', 401,
                                       login_url=login_url)
                return redirect(login_url)
            if not last_seen or not request.headers.get('X-Portal-Background'):
                session['billing_last_seen'] = now
        # 본사 미리보기: 인트라넷에서 로그아웃했으면 미리보기도 끝내고, 저장·처리 요청은 막는다.
        if member and session.get('billing_preview_by'):
            if not session.get('emp_no'):
                for key in MEMBER_SESSION_KEYS:
                    session.pop(key, None)
                member = None
            elif request.method == 'POST':
                return _json_error('본사 미리보기 화면에서는 저장하거나 처리할 수 없습니다. (읽기 전용)', 403)
        if not member:
            if wants_json:
                return _json_error('로그인이 필요합니다.', 401, login_url=url_for('school_portal.portal_login'))
            return redirect(url_for('school_portal.portal_login'))
        if request.method == 'POST' and not _portal_csrf_ok():
            return _json_error('요청을 확인할 수 없습니다. 화면을 새로고침한 뒤 다시 시도해 주세요.', 403)
        request.billing_member = member
        return view(*args, **kwargs)
    return wrapped


def _login_rate_limited(key):
    now = time.monotonic()
    recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_failures[key] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


def _record_login_failure(key):
    _login_failures.setdefault(key, []).append(time.monotonic())


@portal_bp.route('/login', methods=['GET'])
def portal_login():
    return render_template('school_billing/portal_login.html', csrf_token=_portal_csrf(), dormant_days=DORMANT_DAYS,
                           expired=request.args.get('expired') == '1',
                           idle_hours=MEMBER_IDLE_SECONDS // 3600)


@portal_bp.route('/login', methods=['POST'])
def portal_login_submit():
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    login_id = str(data.get('login_id') or '').strip().lower()
    password = str(data.get('password') or '')
    limit_key = f'{_client_key()}:{login_id}'
    if _login_rate_limited(limit_key):
        return _json_error('로그인 실패가 여러 번 반복되었습니다. 15분 후 다시 시도해 주세요.', 429)
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _lock_dormant_members(conn)
        row = conn.execute('SELECT * FROM billing_members WHERE login_id=?', (login_id,)).fetchone()
        if not row or not verify_password(row['password'], password):
            _record_login_failure(limit_key)
            return _json_error('아이디 또는 비밀번호가 올바르지 않습니다.', 401)
        if row['status'] == 'replaced':
            return _json_error('학교 담당자가 교체되어 이용이 종료된 계정입니다. 새담 본사로 문의해 주세요.', 403)
        if row['status'] == 'pending':
            return _json_error('가입 승인 대기 중입니다. 새담 본사에서 승인하면 문자로 알려 드립니다.', 403)
        if row['status'] == 'dormant':
            notice = _request_unlock(conn, dict(row))
            return _json_error(f'{DORMANT_DAYS}일 이상 접속하지 않아 계정이 잠겼습니다.\n'
                               f'새담 본사에서 접속을 허용하면 다시 이용할 수 있습니다. ({notice})', 403)
        if row['status'] != 'active':
            return _json_error('이용이 정지된 계정입니다. 새담 본사로 문의해 주세요.', 403)
        now = _now_text()
        conn.execute('UPDATE billing_members SET last_login_at=?, last_active_at=? WHERE id=?', (now, now, row['id']))
        conn.commit()
    finally:
        conn.close()
    _login_failures.pop(limit_key, None)
    for key in MEMBER_SESSION_KEYS:
        session.pop(key, None)
    session['billing_member_id'] = row['id']
    session['billing_member_name'] = row['name']
    session['billing_school_name'] = row['school_name']
    session['billing_last_seen'] = time.time()
    session['portal_csrf'] = secrets.token_urlsafe(24)
    return jsonify({'status': 'success', 'redirect': url_for('school_portal.portal_home')})


@portal_bp.route('/logout')
def portal_logout():
    for key in MEMBER_SESSION_KEYS + ('portal_csrf',):
        session.pop(key, None)
    return redirect(url_for('school_portal.portal_login'))


# ---- 가입 (초대 링크 + 문자 인증) -------------------------------------------

def _valid_invite(conn, token):
    row = conn.execute('SELECT * FROM billing_invites WHERE token_hash=?', (_token_hash(token),)).fetchone()
    if not row:
        return None, '유효하지 않은 가입 링크입니다.'
    if row['status'] == 'used':
        return None, '이미 가입이 완료된 링크입니다. 로그인해 주세요.'
    if row['status'] != 'sent':
        return None, '취소되었거나 사용할 수 없는 가입 링크입니다. 새담 본사에 다시 요청해 주세요.'
    if row['expires_at'] and row['expires_at'] < _now_text():
        return None, '가입 링크의 유효기간이 지났습니다. 새담 본사에 다시 요청해 주세요.'
    return dict(row), None


@portal_bp.route('/join/<token>')
def portal_join(token):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        invite, error = _valid_invite(conn, token)
    finally:
        conn.close()
    return render_template(
        'school_billing/portal_join.html',
        invite=invite, error=error, token=token, csrf_token=_portal_csrf(), dormant_days=DORMANT_DAYS,
    )


@portal_bp.route('/join/<token>/otp', methods=['POST'])
def portal_join_send_otp(token):
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    try:
        phone = normalize_phone(data.get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    name = str(data.get('name') or '').strip()[:30] or '선생님'

    conn = get_db()
    try:
        ensure_billing_schema(conn)
        invite, error = _valid_invite(conn, token)
        if error:
            return _json_error(error, 410)
        now = time.time()
        recent = conn.execute(
            'SELECT created_at FROM billing_otps WHERE invite_id=? ORDER BY id DESC LIMIT 1', (invite['id'],)
        ).fetchone()
        if recent and now - float(recent['created_at']) < OTP_RESEND_SECONDS:
            wait = int(OTP_RESEND_SECONDS - (now - float(recent['created_at']))) + 1
            return _json_error(f'{wait}초 후에 다시 요청해 주세요.', 429)
        sends = conn.execute('SELECT COUNT(*) FROM billing_otps WHERE invite_id=?', (invite['id'],)).fetchone()[0]
        if sends >= OTP_MAX_SENDS:
            return _json_error('인증번호 요청 횟수를 초과했습니다. 새담 본사에 문의해 주세요.', 429)

        code = f'{secrets.randbelow(1000000):06d}'
        ok, result = _send_sms(
            phone, f'[새담 청구업무] {name}님 학교회원 가입 인증번호는 {code} 입니다. 5분 안에 입력해 주세요.'
        )
        if not ok:
            return _json_error(result, 502)
        conn.execute(
            'INSERT INTO billing_otps (invite_id, phone, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)',
            (invite['id'], phone, _otp_hash(invite['id'], code), now, now + OTP_VALID_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': '인증번호를 문자로 보냈습니다. 5분 안에 입력해 주세요.'})


@portal_bp.route('/join/<token>/complete', methods=['POST'])
def portal_join_complete(token):
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    login_id = str(data.get('login_id') or '').strip().lower()
    password = str(data.get('password') or '')
    password_confirm = str(data.get('password_confirm') or '')
    name = str(data.get('name') or '').strip()
    school_name = str(data.get('school_name') or '').strip()
    tel = str(data.get('tel') or '').strip()
    code = re.sub(r'\D', '', str(data.get('code') or ''))

    if not LOGIN_ID_PATTERN.match(login_id):
        return _json_error('아이디는 영문 소문자·숫자·밑줄(_)로 4~20자여야 합니다.')
    if len(password) < 8 or not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return _json_error('비밀번호는 영문과 숫자를 섞어 8자 이상으로 입력해 주세요.')
    if password != password_confirm:
        return _json_error('비밀번호 확인이 일치하지 않습니다.')
    if not name or len(name) > 30:
        return _json_error('성명을 입력해 주세요.')
    if not school_name or len(school_name) > 60:
        return _json_error('학교명을 입력해 주세요.')
    if len(tel) > 20:
        return _json_error('전화번호를 확인해 주세요.')
    try:
        phone = normalize_phone(data.get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    if len(code) != 6:
        return _json_error('문자로 받은 인증번호 6자리를 입력해 주세요.')
    if not data.get('agree_privacy'):
        return _json_error('개인정보 수집 · 이용 안내를 확인하고 동의해 주세요.')

    conn = get_db()
    try:
        ensure_billing_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        invite, error = _valid_invite(conn, token)
        if error:
            conn.rollback()
            return _json_error(error, 410)
        otp = conn.execute(
            'SELECT * FROM billing_otps WHERE invite_id=? ORDER BY id DESC LIMIT 1', (invite['id'],)
        ).fetchone()
        if not otp:
            conn.rollback()
            return _json_error('먼저 [인증번호 받기]를 눌러 주세요.')
        if otp['phone'] != phone:
            conn.rollback()
            return _json_error('인증번호를 받은 휴대폰번호와 입력한 번호가 다릅니다.')
        if time.time() > float(otp['expires_at']):
            conn.rollback()
            return _json_error('인증번호 유효시간(5분)이 지났습니다. 다시 받아 주세요.')
        if int(otp['attempts']) >= OTP_MAX_ATTEMPTS:
            conn.rollback()
            return _json_error('인증번호를 여러 번 틀렸습니다. 인증번호를 다시 받아 주세요.', 429)
        if not hmac.compare_digest(otp['code_hash'], _otp_hash(invite['id'], code)):
            conn.execute('UPDATE billing_otps SET attempts = attempts + 1 WHERE id=?', (otp['id'],))
            conn.commit()
            return _json_error('인증번호가 올바르지 않습니다.')
        if conn.execute('SELECT id FROM billing_members WHERE login_id=?', (login_id,)).fetchone():
            conn.rollback()
            return _json_error('이미 사용 중인 아이디입니다. 다른 아이디를 입력해 주세요.', 409)
        if invite.get('replaces_member_id') and invite.get('school_name'):
            school_name = invite['school_name']   # 담당자 교체 초대는 기존 학교명을 그대로 이어받는다

        # 가입하면 본사 승인 전까지 '승인 대기'로 두고 로그인을 막는다.
        conn.execute(
            'INSERT INTO billing_members (login_id, password, name, school_name, school_id, phone, tel, email, invite_id, '
            "status, privacy_agreed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (login_id, hash_password(password), name, school_name, invite['school_id'], phone, tel,
             invite['email'], invite['id'], _now_text()),
        )
        conn.execute('UPDATE billing_otps SET verified=1 WHERE id=?', (otp['id'],))
        conn.execute("UPDATE billing_invites SET status='used', used_at=? WHERE id=?", (_now_text(), invite['id']))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    conn = get_db()
    try:
        staff_phone = _get_setting(conn, NOTIFY_PHONE_SETTING, '')
    finally:
        conn.close()
    _push('billing_members')
    if staff_phone:
        _send_sms(staff_phone, f'[새담 청구업무] 학교회원 가입 승인 요청\n{school_name} {name} 선생님\n'
                               f'본사 청구업무 > 학교회원 관리에서 승인해 주세요.')
    return jsonify({
        'status': 'success',
        'message': '가입 신청이 완료되었습니다. 새담 본사에서 승인하면 문자로 알려 드리며, 그 뒤 로그인할 수 있습니다.',
        'redirect': url_for('school_portal.portal_login'),
    })


# ---- 아이디 찾기 · 비밀번호 재설정 (휴대폰 문자 인증) ----------------------------------

def _recovery_hash(purpose, member_id, code):
    return hashlib.sha256(f'billing-recovery:{purpose}:{member_id}:{code}'.encode('utf-8')).hexdigest()


def _recovery_member(conn, mode, data, phone):
    if mode == 'id':
        name = str(data.get('name') or '').strip()
        if not name:
            raise ValueError('성명을 입력해 주세요.')
        return conn.execute(
            "SELECT * FROM billing_members WHERE name=? AND phone=? AND status != 'replaced' ORDER BY id DESC LIMIT 1",
            (name, phone),
        ).fetchone()
    login_id = str(data.get('login_id') or '').strip().lower()
    if not login_id:
        raise ValueError('아이디를 입력해 주세요.')
    return conn.execute(
        "SELECT * FROM billing_members WHERE login_id=? AND phone=? AND status != 'replaced'", (login_id, phone)
    ).fetchone()


def _issue_recovery_code(conn, purpose, member_id, phone, sms_text):
    """문자 인증번호를 보낸다. 같은 번호로 1분에 한 번, 1시간에 OTP_MAX_SENDS번까지. sms_text의 {code}를 바꿔 넣는다."""
    now = time.time()
    recent = conn.execute(
        "SELECT created_at FROM billing_recovery_codes WHERE phone=? AND purpose != 'reset_pw' ORDER BY id DESC LIMIT 1",
        (phone,),
    ).fetchone()
    if recent and now - float(recent['created_at']) < OTP_RESEND_SECONDS:
        wait = int(OTP_RESEND_SECONDS - (now - float(recent['created_at']))) + 1
        raise PermissionError(f'{wait}초 후에 다시 요청해 주세요.')
    sends = conn.execute(
        "SELECT COUNT(*) FROM billing_recovery_codes WHERE phone=? AND purpose != 'reset_pw' AND created_at > ?",
        (phone, now - 3600),
    ).fetchone()[0]
    if sends >= OTP_MAX_SENDS:
        raise PermissionError('인증번호 요청이 너무 많습니다. 1시간 뒤 다시 시도해 주세요.')
    code = f'{secrets.randbelow(1000000):06d}'
    ok, result = _send_sms(phone, sms_text.replace('{code}', code))
    if not ok:
        raise RuntimeError(f'인증번호 문자를 보내지 못했습니다. {result}')
    conn.execute(
        'INSERT INTO billing_recovery_codes (purpose, member_id, phone, code_hash, created_at, expires_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (purpose, member_id, phone, _recovery_hash(purpose, member_id, code), now, now + RECOVERY_VALID_SECONDS),
    )
    conn.commit()


def _check_recovery_code(conn, purpose, member_id, phone, code):
    row = conn.execute(
        'SELECT * FROM billing_recovery_codes WHERE purpose=? AND member_id=? AND phone=? AND used=0 '
        'ORDER BY id DESC LIMIT 1', (purpose, member_id, phone),
    ).fetchone()
    if not row:
        raise ValueError('먼저 [인증번호 받기]를 눌러 주세요.')
    if time.time() > float(row['expires_at']):
        raise ValueError('인증번호 유효시간(5분)이 지났습니다. 다시 받아 주세요.')
    if int(row['attempts']) >= OTP_MAX_ATTEMPTS:
        raise ValueError('인증번호를 여러 번 틀렸습니다. 인증번호를 다시 받아 주세요.')
    if not hmac.compare_digest(row['code_hash'], _recovery_hash(purpose, member_id, code)):
        conn.execute('UPDATE billing_recovery_codes SET attempts = attempts + 1 WHERE id=?', (row['id'],))
        conn.commit()
        raise ValueError('인증번호가 올바르지 않습니다.')
    conn.execute('UPDATE billing_recovery_codes SET used=1 WHERE id=?', (row['id'],))


@portal_bp.route('/find/otp', methods=['POST'])
def portal_find_send_otp():
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    mode = 'pw' if data.get('mode') == 'pw' else 'id'
    limit_key = f'{_client_key()}:find'
    if _login_rate_limited(limit_key):
        return _json_error('정보가 맞지 않는 요청이 여러 번 반복되었습니다. 15분 후 다시 시도해 주세요.', 429)
    try:
        phone = normalize_phone(data.get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        try:
            member = _recovery_member(conn, mode, data, phone)
        except ValueError as exc:
            return _json_error(str(exc))
        if not member:
            _record_login_failure(limit_key)
            return _json_error('입력한 정보와 일치하는 학교회원이 없습니다. 가입할 때 입력한 정보를 확인해 주세요.', 404)
        label = '아이디 찾기' if mode == 'id' else '비밀번호 재설정'
        try:
            _issue_recovery_code(conn, f'find_{mode}', member['id'], phone,
                                 f'[새담 청구업무] {label} 인증번호는 {{code}} 입니다. 5분 안에 입력해 주세요.')
        except PermissionError as exc:
            return _json_error(str(exc), 429)
        except RuntimeError as exc:
            return _json_error(str(exc), 502)
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': '인증번호를 문자로 보냈습니다. 5분 안에 입력해 주세요.'})


@portal_bp.route('/find/verify', methods=['POST'])
def portal_find_verify():
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    mode = 'pw' if data.get('mode') == 'pw' else 'id'
    code = re.sub(r'\D', '', str(data.get('code') or ''))
    if len(code) != 6:
        return _json_error('문자로 받은 인증번호 6자리를 입력해 주세요.')
    try:
        phone = normalize_phone(data.get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        try:
            member = _recovery_member(conn, mode, data, phone)
            if not member:
                return _json_error('입력한 정보와 일치하는 학교회원이 없습니다.', 404)
            _check_recovery_code(conn, f'find_{mode}', member['id'], phone, code)
        except ValueError as exc:
            return _json_error(str(exc))
        if mode == 'id':
            conn.commit()
            return jsonify({'status': 'success', 'login_id': member['login_id'],
                            'message': f'가입한 아이디는 {member["login_id"]} 입니다.'})
        token = secrets.token_urlsafe(32)
        now = time.time()
        conn.execute(
            'INSERT INTO billing_recovery_codes (purpose, member_id, phone, code_hash, reset_token_hash, created_at, expires_at) '
            "VALUES ('reset_pw', ?, ?, '', ?, ?, ?)",
            (member['id'], phone, _token_hash(token), now, now + RESET_TOKEN_VALID_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'reset_token': token, 'message': '본인 확인이 끝났습니다. 새 비밀번호를 정해 주세요.'})


@portal_bp.route('/find/reset', methods=['POST'])
def portal_find_reset():
    if not _portal_csrf_ok():
        return _json_error('요청을 확인할 수 없습니다. 새로고침 후 다시 시도해 주세요.', 403)
    data = request.get_json(silent=True) or {}
    password = str(data.get('password') or '')
    problem = _password_problem(password, str(data.get('password_confirm') or ''))
    if problem:
        return _json_error(problem)
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        row = conn.execute(
            "SELECT * FROM billing_recovery_codes WHERE purpose='reset_pw' AND reset_token_hash=? AND used=0",
            (_token_hash(data.get('token')),),
        ).fetchone()
        if not row or time.time() > float(row['expires_at']):
            return _json_error('비밀번호 재설정 시간이 지났습니다. 처음부터 다시 진행해 주세요.', 410)
        member = conn.execute(
            "SELECT * FROM billing_members WHERE id=? AND status != 'replaced'", (row['member_id'],)
        ).fetchone()
        if not member:
            return _json_error('회원 정보를 찾을 수 없습니다.', 404)
        conn.execute('UPDATE billing_members SET password=? WHERE id=?', (hash_password(password), member['id']))
        conn.execute('UPDATE billing_recovery_codes SET used=1 WHERE id=?', (row['id'],))
        conn.commit()
    finally:
        conn.close()
    _login_failures.pop(f'{_client_key()}:{member["login_id"]}', None)
    message = '비밀번호를 바꿨습니다. 새 비밀번호로 로그인해 주세요.'
    if member['status'] == 'pending':
        message += '\n(가입 승인 대기 중이라 본사 승인 후 로그인할 수 있습니다.)'
    elif member['status'] == 'dormant':
        message += '\n(장기 미접속으로 잠긴 계정이라 본사에서 접속을 허용해야 로그인할 수 있습니다.)'
    return jsonify({'status': 'success', 'login_id': member['login_id'], 'message': message})


# ---- 내 정보 (학교담당자 본인 정보 수정) ---------------------------------------------

def _member_public(member):
    try:
        phone_display = format_phone(member['phone']) or member['phone']
    except ValueError:
        phone_display = member['phone']
    return {key: member.get(key) or '' for key in ('login_id', 'name', 'school_name', 'tel', 'email')} | {
        'phone_display': phone_display}


@portal_bp.route('/api/me')
@member_required
def portal_me():
    return jsonify({'status': 'success', 'member': _member_public(request.billing_member)})


@portal_bp.route('/api/me', methods=['POST'])
@member_required
def portal_update_me():
    member = request.billing_member
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    tel = str(data.get('tel') or '').strip()
    email = _normalize_email(data.get('email'))
    if not name or len(name) > 30:
        return _json_error('성명을 30자 이내로 입력해 주세요.')
    if len(tel) > 20:
        return _json_error('전화번호를 확인해 주세요.')
    if not EMAIL_PATTERN.match(email):
        return _json_error('올바른 이메일 주소를 입력해 주세요.')
    conn = get_db()
    try:
        if conn.execute('SELECT id FROM billing_members WHERE LOWER(email)=? AND id != ?', (email, member['id'])).fetchone():
            return _json_error('다른 학교회원이 이미 쓰는 이메일입니다.', 409)
        conn.execute('UPDATE billing_members SET name=?, tel=?, email=? WHERE id=?', (name, tel, email, member['id']))
        conn.commit()
    finally:
        conn.close()
    session['billing_member_name'] = name
    return jsonify({'status': 'success', 'message': '내 정보를 저장했습니다.',
                    'member': _member_public(dict(member, name=name, tel=tel, email=email))})


@portal_bp.route('/api/me/phone-otp', methods=['POST'])
@member_required
def portal_phone_otp():
    member = request.billing_member
    try:
        phone = normalize_phone((request.get_json(silent=True) or {}).get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    if phone == member['phone']:
        return _json_error('지금 등록된 휴대폰번호와 같습니다.')
    conn = get_db()
    try:
        _issue_recovery_code(conn, 'phone', member['id'], phone,
                             '[새담 청구업무] 휴대폰번호 변경 인증번호는 {code} 입니다. 5분 안에 입력해 주세요.')
    except PermissionError as exc:
        return _json_error(str(exc), 429)
    except RuntimeError as exc:
        return _json_error(str(exc), 502)
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': '새 휴대폰번호로 인증번호를 보냈습니다. 5분 안에 입력해 주세요.'})


@portal_bp.route('/api/me/phone', methods=['POST'])
@member_required
def portal_change_phone():
    member = request.billing_member
    data = request.get_json(silent=True) or {}
    code = re.sub(r'\D', '', str(data.get('code') or ''))
    try:
        phone = normalize_phone(data.get('phone'), required=True)
    except ValueError as exc:
        return _json_error(str(exc))
    if len(code) != 6:
        return _json_error('문자로 받은 인증번호 6자리를 입력해 주세요.')
    conn = get_db()
    try:
        try:
            _check_recovery_code(conn, 'phone', member['id'], phone, code)
        except ValueError as exc:
            return _json_error(str(exc))
        conn.execute('UPDATE billing_members SET phone=? WHERE id=?', (phone, member['id']))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'phone_display': format_phone(phone) or phone,
                    'message': '휴대폰번호를 바꿨습니다. 앞으로 알림 문자는 새 번호로 갑니다.'})


@portal_bp.route('/api/me/password', methods=['POST'])
@member_required
def portal_change_password():
    member = request.billing_member
    data = request.get_json(silent=True) or {}
    current = str(data.get('current_password') or '')
    password = str(data.get('password') or '')
    if not verify_password(member['password'], current):
        return _json_error('현재 비밀번호가 올바르지 않습니다.')
    problem = _password_problem(password, str(data.get('password_confirm') or ''))
    if problem:
        return _json_error(problem)
    if password == current:
        return _json_error('지금 쓰는 비밀번호와 다른 비밀번호로 정해 주세요.')
    conn = get_db()
    try:
        conn.execute('UPDATE billing_members SET password=? WHERE id=?', (hash_password(password), member['id']))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': '비밀번호를 바꿨습니다. 다음 로그인부터 새 비밀번호를 쓰세요.'})


# ---- 학교회원 전용페이지 ------------------------------------------------------

@portal_bp.route('/')
@member_required
def portal_home():
    member = request.billing_member
    conn = get_db()
    try:
        staff_status = _staff_status(conn)
    finally:
        conn.close()
    return render_template(
        'school_billing/portal_home.html',
        member=member,
        staff_status=staff_status,
        priorities=TASK_PRIORITIES,
        preview_by=session.get('billing_preview_by'),
        task_types=TASK_TYPES,
        task_type_icons=TASK_TYPE_ICONS,
        stage_labels=STAGE_LABELS,
        csrf_token=_portal_csrf(),
    )


@portal_bp.route('/api/summary')
@member_required
def portal_summary():
    conn = get_db()
    try:
        data = _summary(conn, member_id=request.billing_member['id'])
        data['status'] = 'success'
        return jsonify(data)
    finally:
        conn.close()


@portal_bp.route('/api/tasks', methods=['GET'])
@member_required
def portal_tasks():
    conn = get_db()
    try:
        return jsonify({'status': 'success', 'tasks': _list_tasks(conn, member_id=request.billing_member['id'])})
    finally:
        conn.close()


@portal_bp.route('/api/tasks', methods=['POST'])
@member_required
def portal_start_task():
    member = request.billing_member
    task_type = request.form.get('task_type', '')
    title = str(request.form.get('title') or '').strip()
    content = str(request.form.get('content') or '').strip()
    amount = str(request.form.get('amount') or '').strip()
    if task_type not in TASK_TYPES:
        return _json_error('업무 종류를 선택해 주세요.')
    if not title or len(title) > 100:
        return _json_error('제목을 100자 이내로 입력해 주세요.')
    if len(amount) > 30:
        return _json_error('청구금액을 확인해 주세요.')
    priority = str(request.form.get('priority') or 'normal')
    if priority not in TASK_PRIORITIES:
        priority = 'normal'
    # 금일 이내 · 긴급은 오늘이 처리 기한 — 오늘 안에 완료되지 않으면 다음 날부터 [처리 지연]으로 보인다.
    due_date = _now_text()[:10] if priority in ('today', 'urgent') else ''

    conn = get_db()
    try:
        cur = conn.execute(
            'INSERT INTO billing_tasks (member_id, task_type, title, content, amount, due_date, priority) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (member['id'], task_type, title, content, amount if task_type != 'data_request' else '',
             due_date or None, priority),
        )
        task_id = cur.lastrowid
        uploads = [f for f in request.files.getlist('files') if f and f.filename]
        tokens = [t for t in request.form.getlist('upload_ids') if t]
        try:
            if content or uploads or tokens:
                _save_message(conn, task_id, 'member', member['name'], content, uploads,
                              tokens, ('member', str(member['id'])))
        except ValueError as exc:
            conn.rollback()
            return _json_error(str(exc))
        conn.execute(
            "INSERT INTO billing_stage_logs (task_id, from_stage, to_stage, actor_type, actor_name) "
            "VALUES (?, NULL, 'start', 'member', ?)",
            (task_id, member['name']),
        )
        conn.commit()
        _push('billing_task', member_id=member['id'], task_id=task_id, kind='created', by='member')

        notify_result = ''
        # 긴급 요청은 문자 알림 체크와 상관없이 본사에 바로 알린다.
        if priority == 'urgent' or request.form.get('notify') in ('1', 'true', 'on'):
            phone = _get_setting(conn, NOTIFY_PHONE_SETTING, '')
            if phone:
                head = '[긴급] ' if priority == 'urgent' else ''
                _ok, notify_result = _send_sms(
                    phone,
                    f'{head}[새담 청구업무] {member["school_name"]} {TASK_TYPES[task_type]} 업무가 새로 시작되었습니다.\n'
                    f'"{title[:24]}" · 담당 {member["name"]}\n처리 요청: {TASK_PRIORITIES[priority]["label"]}',
                )
            else:
                notify_result = '본사 알림 번호가 등록되지 않아 문자를 보내지 못했습니다.'
    finally:
        conn.close()
    return jsonify({'status': 'success', 'message': f'{TASK_TYPES[task_type]} 업무를 시작했습니다.',
                    'task_id': task_id, 'notify_result': notify_result})


@portal_bp.route('/api/tasks/<int:task_id>')
@member_required
def portal_task_detail(task_id):
    conn = get_db()
    try:
        task = _load_task(conn, task_id, member_id=request.billing_member['id'])
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        payload = _task_detail_payload(conn, task, 'school_portal.portal_file')
        payload['viewer'] = 'member'
        return jsonify(payload)
    finally:
        conn.close()


@portal_bp.route('/api/tasks/<int:task_id>/messages', methods=['POST'])
@member_required
def portal_post_message(task_id):
    member = request.billing_member
    conn = get_db()
    try:
        task = _load_task(conn, task_id, member_id=member['id'])
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        if task['stage'] == 'done':
            return _json_error('완료된 업무에는 더 이상 글을 남길 수 없습니다.')
        try:
            _save_message(conn, task_id, 'member', member['name'], request.form.get('body'),
                          request.files.getlist('files'), request.form.getlist('upload_ids'),
                          ('member', str(member['id'])))
        except ValueError as exc:
            conn.rollback()
            return _json_error(str(exc))
        conn.commit()
        _push('billing_task', member_id=member['id'], task_id=task_id, kind='message', by='member')
        return jsonify({'status': 'success', 'message': '등록했습니다.'})
    finally:
        conn.close()


@portal_bp.route('/api/tasks/<int:task_id>/advance', methods=['POST'])
@member_required
def portal_advance(task_id):
    member = request.billing_member
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        task = _load_task(conn, task_id, member_id=member['id'])
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        try:
            to_stage, notified, notify_result = _advance_task(
                conn, task, 'member', member['name'], bool(data.get('notify')))
        except PermissionError as exc:
            return _json_error(str(exc), 403)
        except ValueError as exc:
            return _json_error(str(exc))
        return jsonify({
            'status': 'success',
            'message': f'[{STAGE_LABELS[to_stage]}] 처리했습니다.',
            'notified': notified,
            'notify_result': notify_result,
        })
    finally:
        conn.close()


@portal_bp.route('/files/<int:file_id>')
@member_required
def portal_file(file_id):
    conn = get_db()
    try:
        return _serve_file(conn, file_id, member_id=request.billing_member['id'])
    finally:
        conn.close()


# =====================================================================
# 이용안내 — 새담 공지사항 · 본사 연락처 · 긴급연락요청
# =====================================================================

def _contact_info(conn):
    return {
        'phone': _get_setting(conn, 'billing_contact_phone', CONTACT_DEFAULTS['billing_contact_phone']),
        'manager': _get_setting(conn, 'billing_contact_manager', CONTACT_DEFAULTS['billing_contact_manager']),
        'email': _get_setting(conn, 'billing_contact_email', CONTACT_DEFAULTS['billing_contact_email']),
        'fax': _get_setting(conn, 'billing_contact_fax', ''),
    }


def _staff_status(conn):
    key = _get_setting(conn, STAFF_STATUS_SETTING, DEFAULT_STAFF_STATUS)
    status = dict(STAFF_STATUS_MAP.get(key) or STAFF_STATUS_MAP[DEFAULT_STAFF_STATUS])
    updated_at, _sep, updated_by = _get_setting(conn, STAFF_STATUS_UPDATED_SETTING, '').partition('|')
    status['updated_at'] = updated_at
    status['updated_by'] = updated_by
    status['updated_clock'] = _time12(updated_at)[11:] if updated_at else ''   # 'pm 02:24'
    return status


@billing_bp.route('/api/staff-status', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_set_status():
    key = str((request.get_json(silent=True) or {}).get('key') or '')
    if key not in STAFF_STATUS_MAP:
        return _json_error('알 수 없는 상태입니다.')
    conn = get_db()
    try:
        _set_setting(conn, STAFF_STATUS_SETTING, key)
        _set_setting(conn, STAFF_STATUS_UPDATED_SETTING, f'{_now_text()}|{_staff_name()}')
        conn.commit()
        status = _staff_status(conn)
    finally:
        conn.close()
    _push('billing_status', all_members=True)   # 모든 학교에 똑같이 공개되는 정보라 전체에 신호를 보낸다
    return jsonify({
        'status': 'success',
        'staff_status': status,
        'message': f"{status['emoji']} [{status['label']}] 상태로 바꿨습니다. 학교회원 화면에 바로 보입니다.",
    })


@portal_bp.route('/api/staff-status')
@member_required
def portal_staff_status():
    conn = get_db()
    try:
        status = _staff_status(conn)
    finally:
        conn.close()
    status.pop('updated_by', None)   # 학교에는 바뀐 시각만 보여준다
    return jsonify({'status': 'success', 'staff_status': status})


def _notice_rows(conn, limit=None):
    sql = ('SELECT id, title, body, is_pinned, created_by, created_at, updated_at '
           'FROM billing_notices ORDER BY is_pinned DESC, id DESC')
    if limit:
        sql += f' LIMIT {int(limit)}'
    return [dict(r) for r in conn.execute(sql).fetchall()]


@billing_bp.route('/api/guide')
@menu_permission_required(BILLING_MENU_KEY)
def staff_guide():
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        logs = [dict(r) for r in conn.execute(
            'SELECT id, school_name, member_name, phone, ok, result, created_at '
            'FROM billing_emergency_logs ORDER BY id DESC LIMIT 30'
        ).fetchall()]
        for log in logs:
            try:
                log['phone_display'] = format_phone(log['phone']) or log['phone']
            except ValueError:
                log['phone_display'] = log['phone']
        contacts, selected = _notify_contacts(conn)
        return jsonify({
            'status': 'success',
            'notices': _notice_rows(conn),
            'contact': _contact_info(conn),
            'emergencies': logs,
            'notify_phone_set': bool(_get_setting(conn, NOTIFY_PHONE_SETTING, '')),
            'notify_contacts': contacts,
            'notify_selected': selected,
        })
    finally:
        conn.close()


@billing_bp.route('/api/notices', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_save_notice():
    data = request.get_json(silent=True) or {}
    title = str(data.get('title') or '').strip()
    body = str(data.get('body') or '').strip()
    pinned = 1 if data.get('is_pinned') else 0
    if not title or len(title) > 80:
        return _json_error('공지 제목을 80자 이내로 입력해 주세요.')
    if len(body) > 2000:
        return _json_error('공지 내용은 2,000자 이내로 입력해 주세요.')
    notice_id = data.get('id')
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        if notice_id:
            cur = conn.execute(
                'UPDATE billing_notices SET title=?, body=?, is_pinned=?, updated_at=? WHERE id=?',
                (title, body, pinned, _now_text(), notice_id),
            )
            if not cur.rowcount:
                return _json_error('공지사항을 찾을 수 없습니다.', 404)
            message = '공지사항을 수정했습니다.'
        else:
            now = _now_text()
            conn.execute(
                'INSERT INTO billing_notices (title, body, is_pinned, created_by, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (title, body, pinned, _staff_name(), now, now),
            )
            removed = conn.execute(
                'DELETE FROM billing_notices WHERE id NOT IN '
                '(SELECT id FROM billing_notices ORDER BY id DESC LIMIT ?)',
                (NOTICE_KEEP_COUNT,),
            ).rowcount
            message = '공지사항을 등록했습니다. 학교회원 [이용안내] 탭에 바로 보입니다.'
            if removed:
                message += f'\n최근 {NOTICE_KEEP_COUNT}개만 남기고 이전 공지 {removed}개는 삭제했습니다.'
        conn.commit()
        return jsonify({'status': 'success', 'message': message})
    finally:
        conn.close()


@billing_bp.route('/api/notices/<int:notice_id>/delete', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_delete_notice(notice_id):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        cur = conn.execute('DELETE FROM billing_notices WHERE id=?', (notice_id,))
        conn.commit()
        if not cur.rowcount:
            return _json_error('공지사항을 찾을 수 없습니다.', 404)
        return jsonify({'status': 'success', 'message': '공지사항을 삭제했습니다.'})
    finally:
        conn.close()


@billing_bp.route('/api/contact', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_save_contact():
    data = request.get_json(silent=True) or {}
    phone = str(data.get('phone') or '').strip()[:30]
    fax = str(data.get('fax') or '').strip()[:30]
    manager = str(data.get('manager') or '').strip()[:40]
    email = _normalize_email(data.get('email'))[:120]
    if not phone:
        return _json_error('본사 대표전화를 입력해 주세요.')
    if email and not EMAIL_PATTERN.match(email):
        return _json_error('이메일 주소 형식을 확인해 주세요.')
    conn = get_db()
    try:
        _set_setting(conn, 'billing_contact_phone', phone)
        _set_setting(conn, 'billing_contact_fax', fax)
        _set_setting(conn, 'billing_contact_manager', manager)
        _set_setting(conn, 'billing_contact_email', email)
        conn.commit()
        return jsonify({'status': 'success', 'message': '본사 연락처를 저장했습니다.',
                        'contact': _contact_info(conn)})
    finally:
        conn.close()


@portal_bp.route('/api/guide')
@member_required
def portal_guide():
    conn = get_db()
    try:
        return jsonify({
            'status': 'success',
            'notices': _notice_rows(conn, limit=20),
            'contact': _contact_info(conn),
        })
    finally:
        conn.close()


# =====================================================================
# 대용량 첨부 — 조각 전송 · 재시도 · 이어받기
# =====================================================================

def _staff_owner_key():
    return str(session.get('emp_no') or '')


def _upload_tmp_dir():
    path = BILLING_UPLOADS / 'tmp'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _upload_part_path(token):
    return _upload_tmp_dir() / f'{token}.part'


def _load_upload(conn, token, owner_type, owner_key):
    if not UPLOAD_TOKEN_PATTERN.match(str(token or '')):
        return None
    row = conn.execute(
        'SELECT * FROM billing_upload_sessions WHERE token=? AND owner_type=? AND owner_key=?',
        (token, owner_type, str(owner_key)),
    ).fetchone()
    return dict(row) if row else None


def _cleanup_stale_uploads(conn):
    """24시간 넘게 끝나지 않았거나 첨부되지 않은 전송은 조각 파일과 함께 정리한다."""
    cutoff = (datetime.now() - timedelta(hours=UPLOAD_STALE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
    for row in conn.execute(
        'SELECT token FROM billing_upload_sessions WHERE updated_at < ?', (cutoff,)
    ).fetchall():
        delete_file(_upload_part_path(row['token']))
    conn.execute('DELETE FROM billing_upload_sessions WHERE updated_at < ?', (cutoff,))


def _upload_init(owner_type, owner_key):
    data = request.get_json(silent=True) or {}
    name = original_filename(data.get('name'))
    blocked = _blocked_file_reason(name)
    if blocked:
        return _json_error(blocked, 415)
    try:
        size = int(data.get('size'))
    except (TypeError, ValueError):
        return _json_error('파일 크기를 확인할 수 없습니다.')
    if size < 0:
        return _json_error('파일 크기를 확인할 수 없습니다.')
    if size > MAX_FILE_BYTES:
        return _json_error(f'"{name}" 파일이 30MB를 넘습니다.', 413)
    token = secrets.token_urlsafe(24)
    now = _now_text()
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _cleanup_stale_uploads(conn)
        _upload_part_path(token).write_bytes(b'')
        conn.execute(
            'INSERT INTO billing_upload_sessions '
            '(token, owner_type, owner_key, original_name, size, received, status, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)',
            (token, owner_type, str(owner_key), name, size, 'ready' if size == 0 else 'uploading', now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'upload_id': token,
        'chunk_size': UPLOAD_CHUNK_BYTES,
        'received': 0,
        'complete': size == 0,
    })


def _upload_chunk(owner_type, owner_key, token):
    """offset 위치의 조각을 이어 붙인다. 이미 받은 부분이 다시 오면 겹친 만큼 버린다."""
    try:
        offset = int(request.args.get('offset', ''))
    except ValueError:
        return _json_error('전송 위치를 확인할 수 없습니다.')
    if offset < 0:
        return _json_error('전송 위치를 확인할 수 없습니다.')
    declared = request.content_length
    if declared is not None and declared > UPLOAD_CHUNK_MAX_BYTES:
        return _json_error('한 번에 보낼 수 있는 조각 크기를 넘었습니다.', 413)
    chunk = request.get_data(cache=False)
    if len(chunk) > UPLOAD_CHUNK_MAX_BYTES:
        return _json_error('한 번에 보낼 수 있는 조각 크기를 넘었습니다.', 413)

    conn = get_db()
    try:
        ensure_billing_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        upload = _load_upload(conn, token, owner_type, owner_key)
        if not upload:
            conn.rollback()
            return _json_error('전송 정보를 찾을 수 없습니다. 파일을 다시 올려 주세요.', 404)
        received = int(upload['received'])
        size = int(upload['size'])
        if upload['status'] == 'ready' or received >= size:
            conn.rollback()
            return jsonify({'status': 'success', 'received': size, 'complete': True})
        if offset > received:
            conn.rollback()
            return _json_error('전송 위치가 맞지 않습니다.', 409, received=received)
        if offset + len(chunk) > size:
            conn.rollback()
            return _json_error('파일 크기보다 많은 데이터가 왔습니다.', 400, received=received)

        skip = received - offset
        fresh = chunk[skip:] if skip < len(chunk) else b''
        if fresh:
            part = _upload_part_path(token)
            with open(part, 'r+b' if part.exists() else 'wb') as handle:
                handle.seek(received)
                handle.write(fresh)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            received += len(fresh)
        complete = received >= size
        conn.execute(
            'UPDATE billing_upload_sessions SET received=?, status=?, updated_at=? WHERE token=?',
            (received, 'ready' if complete else 'uploading', _now_text(), token),
        )
        conn.commit()
        return jsonify({'status': 'success', 'received': received, 'complete': complete})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _upload_status(owner_type, owner_key, token):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        upload = _load_upload(conn, token, owner_type, owner_key)
        if not upload:
            return _json_error('전송 정보를 찾을 수 없습니다. 파일을 다시 올려 주세요.', 404)
        return jsonify({
            'status': 'success',
            'received': int(upload['received']),
            'size': int(upload['size']),
            'complete': upload['status'] == 'ready',
        })
    finally:
        conn.close()


def _upload_cancel(owner_type, owner_key, token):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        upload = _load_upload(conn, token, owner_type, owner_key)
        if upload:
            conn.execute('DELETE FROM billing_upload_sessions WHERE token=?', (token,))
            conn.commit()
            delete_file(_upload_part_path(token))
        return jsonify({'status': 'success'})
    finally:
        conn.close()


@billing_bp.route('/api/uploads', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_upload_init():
    return _upload_init('staff', _staff_owner_key())


@billing_bp.route('/api/uploads/<token>/chunk', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_upload_chunk(token):
    return _upload_chunk('staff', _staff_owner_key(), token)


@billing_bp.route('/api/uploads/<token>', methods=['GET'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_upload_status(token):
    return _upload_status('staff', _staff_owner_key(), token)


@billing_bp.route('/api/uploads/<token>/cancel', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_upload_cancel(token):
    return _upload_cancel('staff', _staff_owner_key(), token)


@portal_bp.route('/api/uploads', methods=['POST'])
@member_required
def portal_upload_init():
    return _upload_init('member', str(request.billing_member['id']))


@portal_bp.route('/api/uploads/<token>/chunk', methods=['POST'])
@member_required
def portal_upload_chunk(token):
    return _upload_chunk('member', str(request.billing_member['id']), token)


@portal_bp.route('/api/uploads/<token>', methods=['GET'])
@member_required
def portal_upload_status(token):
    return _upload_status('member', str(request.billing_member['id']), token)


@portal_bp.route('/api/uploads/<token>/cancel', methods=['POST'])
@member_required
def portal_upload_cancel(token):
    return _upload_cancel('member', str(request.billing_member['id']), token)


# =====================================================================
# 업무 취소 요청 — 학교·본사 양쪽 모두 요청할 수 있고, 상대편이 승인(삭제)·반려한다
# =====================================================================

SIDE_LABELS = {'member': '학교', 'staff': '본사'}


def _other_side(side):
    return 'staff' if side == 'member' else 'member'


def _clear_cancel_request(conn, task_id, actor_name, note):
    conn.execute(
        'UPDATE billing_tasks SET cancel_requested_at=NULL, cancel_requested_by=NULL, cancel_reason=NULL, '
        'cancel_requested_side=NULL, updated_at=? WHERE id=?',
        (_now_text(), task_id),
    )
    conn.execute(
        "INSERT INTO billing_messages (task_id, author_type, author_name, body) VALUES (?, 'system', ?, ?)",
        (task_id, actor_name, note),
    )


def _side_phone(conn, task, side):
    return task.get('member_phone') if side == 'member' else _staff_notify_phone(conn, task)


def _cancel_request(conn, task, side, actor_name, reason):
    other = _other_side(side)
    if task['stage'] == 'done':
        raise ValueError('완료된 업무는 취소를 요청할 수 없습니다.')
    if task['cancel_requested']:
        if task['cancel_requested_side'] == side:
            raise ValueError(f'이미 취소를 요청한 업무입니다. {SIDE_LABELS[other]} 확인을 기다려 주세요.')
        raise ValueError(f'{SIDE_LABELS[other]}에서 먼저 취소를 요청했습니다. 승인 또는 반려해 주세요.')
    now = _now_text()
    conn.execute(
        'UPDATE billing_tasks SET cancel_requested_at=?, cancel_requested_by=?, cancel_reason=?, '
        'cancel_requested_side=?, updated_at=? WHERE id=?',
        (now, actor_name, reason, side, now, task['id']),
    )
    note = f'{SIDE_LABELS[side]} {actor_name}님이 업무 취소를 요청했습니다.' + (f' (사유: {reason})' if reason else '')
    conn.execute(
        "INSERT INTO billing_messages (task_id, author_type, author_name, body) VALUES (?, 'system', ?, ?)",
        (task['id'], actor_name, note),
    )
    conn.commit()
    _push('billing_task', member_id=task['member_id'], task_id=task['id'], kind='cancel', by=side)

    notify_result = ''
    phone = _side_phone(conn, task, other)
    if phone:
        reason_line = f'사유: {reason[:40]}\n' if reason else ''
        head = f'{task["school_name"]} {task["type_label"]}\n"{str(task["title"])[:24]}"\n{reason_line}'
        if side == 'member':
            text = f'[새담 청구업무] 업무 취소 요청\n{head}본사에서 확인하면 업무와 첨부파일이 삭제됩니다.'
        else:
            text = (f'[새담 청구업무] 본사 업무 취소 요청\n{head}'
                    f'학교회원 페이지에서 승인하면 업무와 첨부파일이 삭제됩니다.\n{_portal_url("/")}')
        _ok, notify_result = _send_sms(phone, text)
    elif side == 'staff':
        notify_result = '학교 담당자 휴대폰번호가 없어 문자를 보내지 못했습니다.'
    message = ('취소 요청을 보냈습니다. 본사에서 확인하면 업무와 첨부파일이 삭제됩니다.' if side == 'member'
               else '취소 요청을 보냈습니다. 학교에서 승인하면 업무와 첨부파일이 삭제됩니다.')
    return {'message': message, 'notify_result': notify_result}


def _cancel_respond(conn, task, side, actor_name, action):
    """action: withdraw(요청한 쪽) / approve·reject(상대편)"""
    if not task['cancel_requested']:
        raise ValueError('취소 요청이 없는 업무입니다.')
    requester = task['cancel_requested_side']
    label = SIDE_LABELS[side]

    if action == 'withdraw':
        if requester != side:
            raise PermissionError(f'{SIDE_LABELS[requester]}에서 요청한 취소는 철회할 수 없습니다. 승인 또는 반려해 주세요.')
        _clear_cancel_request(conn, task['id'], actor_name, f'{label} {actor_name}님이 업무 취소 요청을 철회했습니다.')
        conn.commit()
        _push('billing_task', member_id=task['member_id'], task_id=task['id'], kind='cancel', by=side)
        return {'message': '취소 요청을 철회했습니다. 업무가 그대로 진행됩니다.', 'notify_result': ''}

    if requester == side:
        raise PermissionError(f'직접 요청한 취소는 {SIDE_LABELS[_other_side(side)]}에서 승인하거나 반려합니다.')

    phone = _side_phone(conn, task, requester)
    head = f'{task["school_name"]} {task["type_label"]}\n"{str(task["title"])[:24]}"\n'
    if action == 'approve':
        _delete_task_logged(conn, task, 'task_cancel', side, actor_name,
                            reason=f'{SIDE_LABELS[requester]} 요청 · {label} 승인',
                            emp_no=_staff_owner_key() if side == 'staff' else '')
        conn.commit()
        _push('billing_task', member_id=task['member_id'], task_id=task['id'], kind='deleted', by=side)
        result_word = '승인 · 삭제'
        message = ('취소 요청을 확인했습니다. 업무와 첨부파일을 모두 삭제했습니다.' if side == 'staff'
                   else '취소 요청을 승인했습니다. 업무와 첨부파일을 모두 삭제했습니다.')
    elif action == 'reject':
        _clear_cancel_request(conn, task['id'], actor_name,
                              f'{label} {actor_name}님이 업무 취소 요청을 반려했습니다. 업무는 그대로 진행됩니다.')
        conn.commit()
        _push('billing_task', member_id=task['member_id'], task_id=task['id'], kind='cancel', by=side)
        result_word = '반려'
        message = '취소 요청을 반려했습니다. 업무가 그대로 진행됩니다.'
    else:
        raise ValueError('알 수 없는 처리입니다.')

    notify_result = ''
    if phone:
        _ok, notify_result = _send_sms(
            phone, f'[새담 청구업무] 업무 취소 요청 {result_word}\n{head}{label} {actor_name}님이 처리했습니다.')
    return {'message': message, 'notify_result': notify_result}


def _cancel_dispatch(conn, task, side, actor_name, action):
    data = request.get_json(silent=True) or {}
    try:
        if action == 'request':
            result = _cancel_request(conn, task, side, actor_name, str(data.get('reason') or '').strip()[:300])
        else:
            result = _cancel_respond(conn, task, side, actor_name, action)
    except PermissionError as exc:
        return _json_error(str(exc), 403)
    except ValueError as exc:
        return _json_error(str(exc))
    return jsonify(dict({'status': 'success'}, **result))


def _staff_cancel(task_id, action):
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        task = _load_task(conn, task_id)
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        return _cancel_dispatch(conn, task, 'staff', _staff_name(), action)
    finally:
        conn.close()


def _member_cancel(task_id, action):
    member = request.billing_member
    conn = get_db()
    try:
        task = _load_task(conn, task_id, member_id=member['id'])
        if not task:
            return _json_error('업무를 찾을 수 없습니다.', 404)
        return _cancel_dispatch(conn, task, 'member', member['name'], action)
    finally:
        conn.close()


@billing_bp.route('/api/tasks/<int:task_id>/cancel', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_request_cancel(task_id):
    return _staff_cancel(task_id, 'request')


@billing_bp.route('/api/tasks/<int:task_id>/cancel/withdraw', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_withdraw_cancel(task_id):
    return _staff_cancel(task_id, 'withdraw')


@billing_bp.route('/api/tasks/<int:task_id>/cancel/approve', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_approve_cancel(task_id):
    return _staff_cancel(task_id, 'approve')


@billing_bp.route('/api/tasks/<int:task_id>/cancel/reject', methods=['POST'])
@menu_permission_required(BILLING_MENU_KEY)
def staff_reject_cancel(task_id):
    return _staff_cancel(task_id, 'reject')


@portal_bp.route('/api/tasks/<int:task_id>/cancel', methods=['POST'])
@member_required
def portal_cancel_task(task_id):
    return _member_cancel(task_id, 'request')


@portal_bp.route('/api/tasks/<int:task_id>/cancel/withdraw', methods=['POST'])
@member_required
def portal_withdraw_cancel(task_id):
    return _member_cancel(task_id, 'withdraw')


@portal_bp.route('/api/tasks/<int:task_id>/cancel/approve', methods=['POST'])
@member_required
def portal_approve_cancel(task_id):
    return _member_cancel(task_id, 'approve')


@portal_bp.route('/api/tasks/<int:task_id>/cancel/reject', methods=['POST'])
@member_required
def portal_reject_cancel(task_id):
    return _member_cancel(task_id, 'reject')


# =====================================================================
# 본사 → 학교담당자 페이지 미리보기(읽기 전용)
# =====================================================================

@billing_bp.route('/preview/<int:member_id>')
@menu_permission_required(BILLING_MENU_KEY)
def staff_preview_member(member_id):
    """본사 직원이 학교담당자가 보는 전용페이지를 그대로 열어본다. 저장·처리는 막힌다."""
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        row = conn.execute(
            "SELECT id, name, school_name FROM billing_members WHERE id=? AND status='active'",
            (member_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    for key in MEMBER_SESSION_KEYS:
        session.pop(key, None)
    session['billing_member_id'] = row['id']
    session['billing_member_name'] = row['name']
    session['billing_school_name'] = row['school_name']
    session['billing_preview_by'] = _staff_name()
    return redirect(url_for('school_portal.portal_home'))


# =====================================================================
# 실시간 신호 (Socket.IO — 사내 메신저와 같은 서버 연결)
# =====================================================================
#
# 보안 원칙
# 1) 본사용 · 학교용 연결 통로(namespace)를 완전히 나눈다. 두 통로 사이에는 아무것도 오가지 않는다.
# 2) 어느 방(room)에 들어갈지는 서버가 로그인 정보로만 정한다. 브라우저가 방을 고르는 기능은 없다.
#    - 본사: 인트라넷 로그인 + 청구업무 메뉴 권한이 있어야 연결되고, 본사 방 하나에 들어간다.
#    - 학교: 이용중인 학교회원만 연결되고, 자기 회원 번호 방 하나에만 들어간다.
# 3) 신호에는 내용(글 · 이름 · 금액 · 파일)을 싣지 않고 '무엇이 바뀌었는지'만 보낸다.
#    받은 화면은 권한을 매번 확인하는 기존 조회 API로 다시 불러오므로, 신호가 새더라도 정보는 새지 않는다.
# 4) 정지 · 교체 · 잠김 · 삭제된 학교회원은 연결을 즉시 끊는다.

BILLING_STAFF_NS = '/billing-staff'
BILLING_PORTAL_NS = '/billing-portal'
BILLING_STAFF_ROOM = 'billing-staff'
PUSH_FIELDS = ('task_id', 'kind', 'by')   # 신호에 실을 수 있는 값은 이것뿐이다
PUSH_DELAY_SECONDS = 0.3                  # 요청의 DB 저장이 끝난 뒤 보내기 위한 짧은 지연


def _member_room(member_id):
    return f'billing-member:{int(member_id)}'


def _push(event, *, member_id=None, staff=True, all_members=False, **payload):
    """실시간 신호를 보낸다. 실패해도 업무 처리는 막지 않는다."""
    data = {key: payload[key] for key in PUSH_FIELDS if payload.get(key) is not None}
    targets = []
    if staff:
        targets.append((BILLING_STAFF_NS, BILLING_STAFF_ROOM))
    if all_members:
        targets.append((BILLING_PORTAL_NS, None))   # 본사 담당자 상태처럼 모든 학교에 똑같이 공개되는 신호만
    elif member_id:
        targets.append((BILLING_PORTAL_NS, _member_room(member_id)))
    if not targets:
        return

    def send():
        socketio.sleep(PUSH_DELAY_SECONDS)
        for namespace, room in targets:
            try:
                if room:
                    socketio.emit(event, data, to=room, namespace=namespace)
                else:
                    socketio.emit(event, data, namespace=namespace)
            except Exception:
                pass

    try:
        socketio.start_background_task(send)
    except Exception:
        pass


def _kick_member(member_id):
    """이 학교회원의 실시간 연결을 즉시 끊는다(정지 · 교체 · 장기 미접속 잠김 · 삭제)."""
    room = _member_room(member_id)
    try:
        server = socketio.server
        participants = list(server.manager.get_participants(BILLING_PORTAL_NS, room))
        for item in participants:
            sid = item[0] if isinstance(item, (tuple, list)) else item
            server.disconnect(sid, namespace=BILLING_PORTAL_NS)
    except Exception:
        pass
    try:
        socketio.close_room(room, namespace=BILLING_PORTAL_NS)   # 혹시 남은 연결도 이 방 신호는 더 받지 못하게
    except Exception:
        pass


@socketio.on('connect', namespace=BILLING_STAFF_NS)
def billing_staff_socket_connect(auth=None):
    if not session.get('emp_no') or not has_menu_permission(BILLING_MENU_KEY):
        return False
    join_room(BILLING_STAFF_ROOM)
    return True


@socketio.on('connect', namespace=BILLING_PORTAL_NS)
def billing_portal_socket_connect(auth=None):
    member_id = session.get('billing_member_id')
    if not member_id:
        return False
    # 본사 미리보기는 인트라넷에 로그인한 동안만 (읽기 전용 화면이라 신호만 받는다)
    if session.get('billing_preview_by') and not session.get('emp_no'):
        return False
    last_seen = session.get('billing_last_seen')
    if last_seen and time.time() - float(last_seen) > MEMBER_IDLE_SECONDS:
        return False
    conn = get_db()
    try:
        ensure_billing_schema(conn)
        _lock_dormant_members(conn)
        row = conn.execute(
            "SELECT id FROM billing_members WHERE id=? AND status='active'", (member_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    join_room(_member_room(row['id']))
    return True
