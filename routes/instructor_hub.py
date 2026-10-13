"""학교지원 > 강사통합지원.

본사 담당자가 학교를 골라 강사를 직접 등록(한 명 · 붙여넣기 · 엑셀)하면, 강사마다 개인별 접속 링크와
6자리 비밀번호가 무작위로 만들어진다. 강사는 그 링크로 자기 페이지에 들어가 아래 세 가지를 한다.

1. 강사프로필  : 사진과 소개글을 올린다. 본사 승인 후 학부모통합지원 화면에 보인다.
2. 홍보페이지  : 그림·PDF를 올리면 그 학교의 e리플렛으로 자동 등록된다(없으면 새로 만든다).
                  본사 승인 전에는 외부 링크가 열리지 않고, 승인되면 학부모통합지원의 학교 e리플렛에 합쳐진다.
3. 학생출석관리: 출석 QR 화면, 학생 등록, 학부모 알림 등록 안내를 쓴다.

본사 담당자는 [학부모·강사 통합지원](routes/support_hub.py) 화면의 ① 강사 탭(접속 안내 문자·비밀번호 재발급 등)과
③ 승인관리 탭(강사별로 프로필과 홍보 자료를 한 줄에 보며 승인·반려)에서 이 모듈의 기능을 쓴다.
화면은 support_hub가 그리고, 이 모듈은 강사 계정·승인·강사 포털의 규칙과 처리 경로를 맡는다.
학부모에게 링크를 보내는 일도 같은 화면의 ④ 학부모 안내 탭이 맡는다.

강사 계정은 인트라넷 계정(emp_no)과 무관하다. 링크(비밀 토큰) + 비밀번호 + 별도 세션으로
보호하며, 이 세션은 인트라넷 메뉴 권한을 전혀 주지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Blueprint, Response, abort, flash, get_flashed_messages, jsonify, redirect,
    render_template, request, send_file, session, url_for,
)
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

try:  # PDF를 쪽마다 그림으로 바꿔 주는 라이브러리(없으면 PDF만 건너뛴다)
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover
    pdfium = None

from . import ebook as ebook_mod
from . import student_attendance as sa
from .database import get_db
from .instructor_attendance import _external_root, _qr_svg, format_phone, normalize_phone
from .secure_files import delete_file, encrypt_bytes, encrypted_response, encrypted_storage_name, read_decrypted
from .security import load_credential_secret, menu_permission_required
from .storage import DATA_ROOT

instructor_hub_bp = Blueprint('instructor_hub', __name__)
logger = logging.getLogger(__name__)

MENU_KEY = 'school_support_hub'      # 강사통합지원 + 학부모통합지원은 [학부모·강사 통합지원] 한 메뉴다
HUB_ROOT = Path(DATA_ROOT) / 'instructor_hub'
PHOTO_ROOT = HUB_ROOT / 'photos'
PROMO_ROOT = HUB_ROOT / 'promo'

SESSION_KEY = 'ih_account_id'
CSRF_KEY = 'ih_csrf'

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PROMO_FILE_BYTES = 25 * 1024 * 1024
MAX_PROMO_FILES = 20
MAX_PROMO_PAGES = 30
PHOTO_LONG_SIDE = 900
PROMO_LONG_SIDE = 1800
PDF_DPI = 130
BIO_LIMIT = 1500
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_FAILURES = 8
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
STATUS_LABELS = {'none': '미등록', 'pending': '승인대기', 'approved': '승인됨', 'rejected': '반려됨'}

_login_failures: dict[str, list[float]] = {}


# ---------------------------------------------------------------- 스키마

def ensure_instructor_hub_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS ih_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL UNIQUE,
            pw_hash TEXT NOT NULL,
            pw_enc TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            last_login_at TEXT,
            sms_sent_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ih_accounts_phone ON ih_accounts(phone);

        -- 강사가 일하는 학교·강의부서(본사가 등록한 한 건이 한 줄).
        CREATE TABLE IF NOT EXISTS ih_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            school_id INTEGER,
            school_name TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            contract_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (account_id, school_name, department)
        );

        -- 강사 한 명당 프로필 하나. 승인 전 수정본은 draft_*에 두고 승인하면 본 내용으로 바꾼다.
        CREATE TABLE IF NOT EXISTS ih_profiles (
            account_id INTEGER PRIMARY KEY,
            photo_path TEXT NOT NULL DEFAULT '',
            bio TEXT NOT NULL DEFAULT '',
            draft_photo_path TEXT NOT NULL DEFAULT '',
            draft_bio TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'none',
            submitted_at TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            reject_reason TEXT NOT NULL DEFAULT ''
        );

        -- 강사×학교마다 홍보 e리플렛 하나. 새로 올린 쪽(draft)은 ih_promo_pages에 보관한다.
        CREATE TABLE IF NOT EXISTS ih_promos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            school_id INTEGER,
            school_name TEXT NOT NULL DEFAULT '',
            ebook_id INTEGER,
            status TEXT NOT NULL DEFAULT 'none',
            submitted_at TEXT,
            reviewed_by TEXT,
            reviewed_at TEXT,
            reject_reason TEXT NOT NULL DEFAULT '',
            UNIQUE (account_id, school_name)
        );
        CREATE TABLE IF NOT EXISTS ih_promo_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ih_promo_pages ON ih_promo_pages(promo_id, position);
    ''')
    # e리플렛에 승인 상태를 더한다. 기존 리플렛은 모두 'approved'라 그대로 공개된다.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ebooks' in tables:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(ebooks)')}
        for name, definition in (
            ('approval_status', "TEXT NOT NULL DEFAULT 'approved'"),
            ('ih_account_id', 'INTEGER'),
        ):
            if name not in columns:
                conn.execute(f'ALTER TABLE ebooks ADD COLUMN {name} {definition}')
    conn.commit()


def init_instructor_hub_schema():
    for folder in (HUB_ROOT, PHOTO_ROOT, PROMO_ROOT):
        folder.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        ensure_instructor_hub_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 공통 도우미

def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _text(value, limit=200):
    return str(value or '').strip()[:limit]


def _multiline(value, limit):
    return str(value or '').replace('\r\n', '\n').replace('\r', '\n').strip()[:limit]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cipher():
    secret = load_credential_secret()
    digest = hashlib.sha256(f'instructor-hub-password:{secret}'.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value):
    return _cipher().encrypt(str(value).encode('utf-8')).decode('ascii')


def _decrypt(value):
    try:
        return _cipher().decrypt(str(value or '').encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''


def _new_password():
    return f'{secrets.randbelow(10 ** 6):06d}'


def _portal_url(account):
    return _external_root() + url_for('instructor_hub.portal_login', token=account['token'])


def _school_id_by_name(conn, school_name):
    row = conn.execute(
        'SELECT id FROM schools WHERE school_name=? ORDER BY year DESC, id DESC LIMIT 1',
        (school_name,),
    ).fetchone()
    return row['id'] if row else None


def norm(value):
    """학교·부서 이름을 비교할 때 쓰는 값(공백 차이를 무시한다)."""
    return re.sub(r'\s+', '', str(value or ''))


def _clean_name(value, limit):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _csrf_token():
    if not session.get(CSRF_KEY):
        session[CSRF_KEY] = secrets.token_urlsafe(24)
    return session[CSRF_KEY]


def _check_csrf():
    sent = request.form.get('csrf_token', '')
    expected = session.get(CSRF_KEY, '')
    if not expected or not secrets.compare_digest(str(sent), str(expected)):
        abort(400)


# ---------------------------------------------------------------- 계정·배정

def create_account(conn, name, phone, email=''):
    """새 강사 계정과 (링크, 비밀번호)를 만든다. 반환: (계정 행, 비밀번호)."""
    password = _new_password()
    token = secrets.token_urlsafe(24)
    account_id = conn.execute('''
        INSERT INTO ih_accounts (name, phone, email, token, pw_hash, pw_enc)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (_text(name, 60), normalize_phone(phone), _text(email, 120), token,
          generate_password_hash(password), _encrypt(password))).lastrowid
    return conn.execute('SELECT * FROM ih_accounts WHERE id=?', (account_id,)).fetchone(), password


def clean_phone(value):
    """휴대폰번호를 숫자만 남긴다. 엑셀이 앞의 0을 지운 번호(1012345678)도 되살린다."""
    digits = normalize_phone(value)
    if len(digits) == 10 and digits.startswith('10'):
        digits = '0' + digits
    return digits


def find_account(conn, name, phone):
    """같은 휴대폰번호의 강사 계정을 찾는다(사용 중인 계정, 이름이 같은 계정 순)."""
    phone = clean_phone(phone)
    if not phone:
        return None
    return conn.execute('''
        SELECT * FROM ih_accounts WHERE phone=?
        ORDER BY is_active DESC, (name=?) DESC, id LIMIT 1
    ''', (phone, _text(name, 60))).fetchone()


def canonical_school(conn, school_name):
    """입력한 학교명을 [학교업무공간]의 학교(공백·연도 무시)와 맞춰 (id, 표준 이름)을 돌려준다.

    등록된 학교가 아니면 (None, 입력한 이름)을 돌려준다.
    """
    cleaned = _clean_name(school_name, 100)
    if not cleaned:
        return None, ''
    key = norm(cleaned)
    rows = conn.execute('SELECT id, school_name FROM schools ORDER BY year DESC, id DESC').fetchall()
    for row in rows:
        if norm(row['school_name']) == key:
            return row['id'], row['school_name']
    return None, cleaned


def add_assignment(conn, account_id, school_name, department):
    """강사를 학교·강의부서에 배정한다(학교명은 학교업무공간의 표준 이름으로 맞춘다). 새로 배정했으면 True."""
    school_id, school_name = canonical_school(conn, school_name)
    if not school_name:
        return False
    cursor = conn.execute('''
        INSERT OR IGNORE INTO ih_assignments (account_id, school_id, school_name, department)
        VALUES (?, ?, ?, ?)
    ''', (account_id, school_id, school_name, _clean_name(department, 60)))
    return cursor.rowcount > 0


PHONE_PATTERN = re.compile(r'01\d{8,9}')


def register_instructor(conn, school_name, name, phone, department, email='', *, allow_unmatched=False):
    """본사 담당자가 강사를 학교·강의부서에 직접 등록한다. 호출한 쪽이 commit한다.

    같은 휴대폰번호의 계정이 이미 있으면 그 계정에 학교·강의부서만 더하고(다른 학교에서도 일하는 강사),
    없으면 접속 링크와 6자리 비밀번호가 무작위로 만들어진 새 계정을 만든다.
    반환: {'ok': False, 'error'} 또는 {'ok': True, 'account', 'created', 'assigned', 'reactivated', 'note'}
    """
    name = _clean_name(name, 60)
    phone = clean_phone(phone)
    department = _clean_name(department, 60)
    email = _text(email, 120)
    if not name:
        return {'ok': False, 'error': '강사 이름을 입력해 주세요.'}
    if not PHONE_PATTERN.fullmatch(phone):
        return {'ok': False, 'error': '휴대폰번호(010으로 시작하는 10~11자리)를 확인해 주세요.'}
    if not department:
        return {'ok': False, 'error': '강의부서를 입력해 주세요. 예) 로봇과학'}
    school_id, school_name = canonical_school(conn, school_name)
    if not school_name:
        return {'ok': False, 'error': '학교를 선택해 주세요.'}
    if school_id is None and not allow_unmatched:
        return {'ok': False, 'error': f'‘{school_name}’은(는) [학교업무공간]에 등록된 학교가 아닙니다.'}

    account = find_account(conn, name, phone)
    created = reactivated = False
    note = ''
    if account is None:
        account, _password = create_account(conn, name, phone, email)
        created = True
    else:
        if not account['is_active']:
            conn.execute('UPDATE ih_accounts SET is_active=1 WHERE id=?', (account['id'],))
            reactivated = True
        if email and not account['email']:
            conn.execute('UPDATE ih_accounts SET email=? WHERE id=?', (email, account['id']))
        if account['name'] != name:
            note = f"같은 휴대폰번호의 기존 계정(‘{account['name']}’)에 연결했습니다."
    assigned = add_assignment(conn, account['id'], school_name, department)
    account = conn.execute('SELECT * FROM ih_accounts WHERE id=?', (account['id'],)).fetchone()
    return {'ok': True, 'account': account, 'created': created, 'assigned': assigned,
            'reactivated': reactivated, 'note': note, 'school_name': school_name, 'department': department}


# -- 여러 명 한꺼번에 등록(붙여넣은 글 · 엑셀)

MAX_IMPORT_ROWS = 300
MAX_IMPORT_BYTES = 2 * 1024 * 1024
HEADER_ALIASES = {
    'name': ('이름', '성명', '성함', '강사명', '강사'),
    'phone': ('휴대폰', '휴대폰번호', '핸드폰', '핸드폰번호', '전화', '전화번호', '연락처'),
    'department': ('강의부서', '부서', '분야', '강의분야', '담당분야', '과목'),
    'email': ('이메일', '메일', 'email', 'e-mail'),
    'school': ('학교', '학교명'),
}
IMPORT_COLUMNS = ('name', 'phone', 'department', 'email', 'school')


def _header_key(value):
    """머리글 칸 이름을 name/phone/department/email/school 중 하나로 바꾼다(아니면 '')."""
    text = re.sub(r'\(.*?\)|\s+', '', str(value or '')).lower()
    for key, names in HEADER_ALIASES.items():
        if text in names:
            return key
    return ''


def _cell_text(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value if value is not None else '').strip()


def _import_row(line, values):
    values = list(values) + [''] * (len(IMPORT_COLUMNS) - len(values))
    row = {key: _cell_text(values[index]) for index, key in enumerate(IMPORT_COLUMNS)}
    row['line'] = line
    return row


def parse_instructor_text(text):
    """붙여넣은 글을 강사 줄로 읽는다. 줄마다 `이름, 휴대폰, 강의부서[, 이메일[, 학교]]`
    (엑셀에서 복사하면 탭으로 나뉜다. 쉼표나 띄어쓰기로 나눠도 된다)."""
    rows = []
    for number, line in enumerate(str(text or '').splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if '\t' in line:
            parts = [part.strip() for part in line.split('\t')]
        elif ',' in line:
            parts = [part.strip() for part in line.split(',')]
        else:
            tokens = line.split()
            parts = tokens[:2] + [' '.join(tokens[2:])] if len(tokens) > 2 else tokens
        if _header_key(parts[0]) == 'name':           # 머리글 줄은 건너뛴다
            continue
        rows.append(_import_row(number, parts[:len(IMPORT_COLUMNS)]))
    return rows


def parse_instructor_workbook(upload):
    """엑셀(.xlsx) 첫 시트를 강사 줄로 읽는다. 머리글(이름·휴대폰·강의부서·이메일·학교)이 있으면 순서와 상관없이
    찾고, 없으면 이름·휴대폰·강의부서·이메일·학교 순서로 본다."""
    from openpyxl import load_workbook
    name = str(getattr(upload, 'filename', '') or '')
    if not name.lower().endswith('.xlsx'):
        raise ValueError('엑셀(.xlsx) 파일만 올릴 수 있습니다. .xls 파일은 [다른 이름으로 저장]에서 .xlsx로 바꿔 주세요.')
    data = upload.stream.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError('엑셀 파일은 2MB까지 올릴 수 있습니다.')
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
        sheet_rows = [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
    except Exception as exc:
        raise ValueError('엑셀 파일을 읽을 수 없습니다. 파일이 깨졌는지 확인해 주세요.') from exc
    rows, mapping, first = [], None, True
    for number, cells in enumerate(sheet_rows, 1):
        if not any(_cell_text(cell) for cell in cells):
            continue
        if first:
            first = False
            keys = [_header_key(cell) for cell in cells]
            if 'name' in keys and 'phone' in keys:
                mapping = {key: index for index, key in enumerate(keys) if key}
                continue
        if mapping:
            values = [cells[mapping[key]] if key in mapping and mapping[key] < len(cells) else ''
                      for key in IMPORT_COLUMNS]
        else:
            values = cells[:len(IMPORT_COLUMNS)]
        rows.append(_import_row(number, values))
    return rows


def register_many(conn, rows, default_school, *, allow_unmatched=False):
    """여러 줄을 차례로 등록한다. 잘못된 줄은 건너뛰고 이유를 모은다. 반환: 요약 dict."""
    summary = {'rows': len(rows), 'created': 0, 'added': 0, 'already': 0, 'errors': [], 'notes': []}
    for row in rows:
        school = row.get('school') or default_school
        result = register_instructor(conn, school, row['name'], row['phone'], row['department'], row['email'],
                                     allow_unmatched=allow_unmatched and not row.get('school'))
        if not result['ok']:
            summary['errors'].append(f"{row['line']}번째 줄 {row['name'] or '(이름 없음)'}: {result['error']}")
        elif result['created']:
            summary['created'] += 1
        elif result['assigned'] or result['reactivated']:
            summary['added'] += 1
        else:
            summary['already'] += 1
        if result.get('note'):
            summary['notes'].append(f"{row['line']}번째 줄 {row['name']}: {result['note']}")
    return summary


def import_message(summary):
    done = summary['created'] + summary['added'] + summary['already']
    parts = [f"{summary['rows']}줄 중 {done}명을 처리했습니다."]
    detail = []
    if summary['created']:
        detail.append(f"새 강사 계정 {summary['created']}개(접속 링크·비밀번호 자동 생성)")
    if summary['added']:
        detail.append(f"기존 계정에 학교·강의부서 추가 {summary['added']}명")
    if summary['already']:
        detail.append(f"이미 등록되어 있음 {summary['already']}명")
    if detail:
        parts.append(' · '.join(detail))
    if summary['notes']:
        parts.append('참고: ' + ' / '.join(summary['notes'][:3]))
    if summary['errors']:
        parts.append(f"확인이 필요한 줄 {len(summary['errors'])}개 (등록하지 않음)")
        parts.extend(summary['errors'][:6])
        if len(summary['errors']) > 6:
            parts.append(f"… 외 {len(summary['errors']) - 6}줄")
    return '\n'.join(parts)


def delete_account(conn, account_id):
    """학교 배정이 하나도 없는 강사 계정을 프로필·홍보 자료 파일까지 지운다. 반환: 오류 메시지 또는 None."""
    if conn.execute('SELECT 1 FROM ih_assignments WHERE account_id=?', (account_id,)).fetchone():
        return '학교에 배정된 강사는 삭제할 수 없습니다. 먼저 [이 학교에서 빼기]를 해 주세요.'
    profile = conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (account_id,)).fetchone()
    if profile:
        delete_file(profile['photo_path'])
        delete_file(profile['draft_photo_path'])
    for promo in conn.execute('SELECT * FROM ih_promos WHERE account_id=?', (account_id,)).fetchall():
        _clear_drafts(conn, promo['id'])
        if promo['ebook_id']:
            book = conn.execute('SELECT cover_path FROM ebooks WHERE id=?', (promo['ebook_id'],)).fetchone()
            for page in conn.execute('SELECT image_path FROM ebook_pages WHERE ebook_id=?',
                                     (promo['ebook_id'],)).fetchall():
                delete_file(page['image_path'])
            if book:
                delete_file(book['cover_path'])
            conn.execute('DELETE FROM ebook_pages WHERE ebook_id=?', (promo['ebook_id'],))
            conn.execute('DELETE FROM ebooks WHERE id=?', (promo['ebook_id'],))
    conn.execute("UPDATE parent_classes SET instructor_emp_no='' WHERE instructor_emp_no=?", (f'IH{account_id}',))
    conn.execute('DELETE FROM ih_promos WHERE account_id=?', (account_id,))
    conn.execute('DELETE FROM ih_profiles WHERE account_id=?', (account_id,))
    conn.execute('DELETE FROM ih_accounts WHERE id=?', (account_id,))
    return None


# ---------------------------------------------------------------- 과목(학생출석관리) ↔ 강사 연결
# 연결 규칙
#  - 수동: 학생출석관리에서 과목에 강사를 직접 골랐으면(parent_classes.instructor_emp_no='IH<계정번호>') 그 강사만.
#  - 자동: 직접 고르지 않았으면, 과목의 학교명·분야(강의부서)가 등록된 학교명·강의부서와 같은 강사 모두.

def manual_account_id(class_row):
    emp = str(class_row['instructor_emp_no'] or '')
    return int(emp[2:]) if emp.startswith('IH') and emp[2:].isdigit() else None


def school_assignment_rows(conn, school_name):
    """한 학교(공백 무시)에 배정된 (배정, 강사 이름·사용 여부) 목록."""
    key = norm(school_name)
    rows = conn.execute('''
        SELECT s.*, a.name AS account_name, a.is_active AS account_active
        FROM ih_assignments s JOIN ih_accounts a ON a.id=s.account_id
        ORDER BY a.name COLLATE NOCASE, s.department
    ''').fetchall()
    return [row for row in rows if norm(row['school_name']) == key]


def linked_accounts(conn, class_row):
    """이 과목에 연결된 강사 계정들(수동 연결이 있으면 그 강사, 없으면 학교·부서가 같은 강사)."""
    manual = manual_account_id(class_row)
    if manual:
        row = conn.execute('SELECT * FROM ih_accounts WHERE id=?', (manual,)).fetchone()
        return [row] if row else []
    seen, result = set(), []
    for item in school_assignment_rows(conn, class_row['school_name']):
        if not item['account_active'] or item['account_id'] in seen:
            continue
        if norm(item['department']) == norm(class_row['department']):
            seen.add(item['account_id'])
            result.append(conn.execute('SELECT * FROM ih_accounts WHERE id=?', (item['account_id'],)).fetchone())
    return result


def accessible_classes(conn, account):
    """이 강사가 쓸 수 있는 과목 행들(수동 연결 + 학교·부서가 맞는 자동 연결)."""
    assignments = conn.execute('SELECT * FROM ih_assignments WHERE account_id=?', (account['id'],)).fetchall()
    keys = {(norm(a['school_name']), norm(a['department'])) for a in assignments}
    mine = f"IH{account['id']}"
    result = []
    for row in conn.execute('''
        SELECT id, school_name, department, class_name, instructor_emp_no FROM parent_classes
        WHERE is_active=1 ORDER BY school_name, department, class_name COLLATE NOCASE, id
    ''').fetchall():
        emp = str(row['instructor_emp_no'] or '')
        if emp == mine or (not emp.startswith('IH') and (norm(row['school_name']), norm(row['department'])) in keys):
            result.append(row)
    return result


def school_instructors(conn, school_name):
    """학생출석관리의 과목 만들기에서 고를 수 있는 이 학교의 강사 목록."""
    rows = school_assignment_rows(conn, school_name)
    seen, result = set(), []
    for item in rows:
        if not item['account_active'] or item['account_id'] in seen:
            continue
        seen.add(item['account_id'])
        result.append({
            'id': item['account_id'], 'name': item['account_name'],
            'departments': [r['department'] for r in rows
                            if r['account_id'] == item['account_id'] and r['department']],
        })
    return result


def school_departments(conn, school_name):
    return sorted({row['department'] for row in school_assignment_rows(conn, school_name) if row['department']})


# ---------------------------------------------------------------- 이미지 처리

def _flatten(image):
    """투명 배경은 흰색으로 깔아 RGB로 바꾼다."""
    if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
        base = Image.new('RGB', image.size, (255, 255, 255))
        rgba = image.convert('RGBA')
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    return image.convert('RGB')


def _jpeg(image, longest, quality=86):
    image = _flatten(image)
    side = max(image.width, image.height)
    if side > longest:
        ratio = longest / side
        image = image.resize((max(1, round(image.width * ratio)), max(1, round(image.height * ratio))),
                             Image.LANCZOS)
    buffer = BytesIO()
    image.save(buffer, 'JPEG', quality=quality, optimize=True)
    return buffer.getvalue(), image.width, image.height


def _read_upload(upload, limit):
    data = upload.stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'파일은 {limit // (1024 * 1024)}MB까지 올릴 수 있습니다.')
    if not data:
        raise ValueError('빈 파일입니다.')
    return data


def _open_image(data):
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            return image.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError('정상적인 이미지 파일이 아닙니다.') from exc


def _store_jpeg(folder, label, data):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / encrypted_storage_name(label + '.jpg')
    encrypt_bytes(data, path)
    return str(path)


def _save_photo(upload):
    """프로필 사진을 정사각에 가깝게 줄여(긴 변 900px) 암호화 저장한다. 반환: 경로."""
    image = _open_image(_read_upload(upload, MAX_PHOTO_BYTES))
    data, _w, _h = _jpeg(image, PHOTO_LONG_SIDE, 88)
    return _store_jpeg(PHOTO_ROOT, 'photo', data)


def _pdf_images(data, limit):
    """PDF를 쪽마다 PIL 그림으로 바꾼다. pypdfium2를 먼저 쓰고, 없으면 PyMuPDF를 쓴다."""
    images = []
    if pdfium is not None:
        document = pdfium.PdfDocument(BytesIO(data))
        try:
            for index in range(min(len(document), limit)):
                page = document.get_page(index)
                try:
                    images.append(page.render(scale=PDF_DPI / 72).to_pil())
                finally:
                    page.close()
        finally:
            document.close()
        return images
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError as exc:
            raise RuntimeError('이 서버에서는 PDF를 변환할 수 없습니다. 그림 파일로 올려 주세요.') from exc
    document = pymupdf.open(stream=data, filetype='pdf')
    try:
        for index in range(min(document.page_count, limit)):
            pixmap = document[index].get_pixmap(dpi=PDF_DPI)
            images.append(Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
                          if pixmap.n == 3 else Image.open(BytesIO(pixmap.tobytes('png'))))
    finally:
        document.close()
    return images


def _frames_from_uploads(uploads):
    """홍보 자료(이미지·PDF)를 쪽마다 JPEG로 바꾼다. 반환: [(jpeg, 가로, 세로, 이름)]."""
    uploads = [u for u in uploads if u and u.filename]
    if not uploads:
        raise ValueError('그림이나 PDF 파일을 선택해 주세요.')
    if len(uploads) > MAX_PROMO_FILES:
        raise ValueError(f'한 번에 파일 {MAX_PROMO_FILES}개까지 올릴 수 있습니다.')
    frames = []
    for upload in sorted(uploads, key=lambda u: str(u.filename).casefold()):
        name = Path(str(upload.filename).replace('\\', '/')).name
        extension = Path(name).suffix.lower()
        data = _read_upload(upload, MAX_PROMO_FILE_BYTES)
        if extension in IMAGE_EXTENSIONS:
            jpeg, width, height = _jpeg(_open_image(data), PROMO_LONG_SIDE)
            frames.append((jpeg, width, height, name))
        elif extension == '.pdf':
            try:
                pages = _pdf_images(data, MAX_PROMO_PAGES - len(frames))
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                raise ValueError(f'‘{name}’은(는) 정상적인 PDF가 아닙니다.') from exc
            for index, image in enumerate(pages, 1):
                jpeg, width, height = _jpeg(image, PROMO_LONG_SIDE)
                frames.append((jpeg, width, height, f'{name} {index}쪽'))
        else:
            raise ValueError(f'‘{name}’은(는) 올릴 수 없는 형식입니다. 그림(JPG·PNG 등)이나 PDF만 올려 주세요.')
        if len(frames) >= MAX_PROMO_PAGES:
            break
    if not frames:
        raise ValueError('올릴 수 있는 페이지가 없습니다.')
    return frames[:MAX_PROMO_PAGES]


# ---------------------------------------------------------------- 프로필

def _profile(conn, account_id):
    row = conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (account_id,)).fetchone()
    if row:
        return row
    conn.execute('INSERT OR IGNORE INTO ih_profiles (account_id) VALUES (?)', (account_id,))
    conn.commit()
    return conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (account_id,)).fetchone()


def submit_profile(conn, account_id, bio, photo_upload=None):
    """프로필을 제출한다(승인대기). 반환: 오류 메시지 또는 None."""
    bio = _multiline(bio, BIO_LIMIT)
    profile = _profile(conn, account_id)
    new_photo = ''
    if photo_upload is not None and getattr(photo_upload, 'filename', ''):
        try:
            new_photo = _save_photo(photo_upload)
        except ValueError as exc:
            return str(exc)
    has_photo = bool(new_photo or profile['draft_photo_path'] or profile['photo_path'])
    if not bio and not has_photo:
        return '프로필 사진이나 소개글을 입력해 주세요.'
    if new_photo:
        delete_file(profile['draft_photo_path'])
    conn.execute('''
        UPDATE ih_profiles SET draft_bio=?, draft_photo_path=?, status='pending',
            submitted_at=?, reject_reason='' WHERE account_id=?
    ''', (bio, new_photo or profile['draft_photo_path'], _now_text(), account_id))
    conn.commit()
    return None


def review_profile(conn, account_id, approve, reviewer, reason=''):
    profile = conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (account_id,)).fetchone()
    if not profile or profile['status'] != 'pending':
        return '승인대기 중인 프로필이 아닙니다.'
    if approve:
        photo = profile['photo_path']
        if profile['draft_photo_path']:
            delete_file(photo)
            photo = profile['draft_photo_path']
        conn.execute('''
            UPDATE ih_profiles SET bio=?, photo_path=?, draft_bio='', draft_photo_path='',
                status='approved', reviewed_by=?, reviewed_at=?, reject_reason='' WHERE account_id=?
        ''', (profile['draft_bio'], photo, reviewer, _now_text(), account_id))
    else:
        conn.execute('''
            UPDATE ih_profiles SET status='rejected', reviewed_by=?, reviewed_at=?, reject_reason=?
            WHERE account_id=?
        ''', (reviewer, _now_text(), _text(reason, 300), account_id))
    conn.commit()
    return None


# ---------------------------------------------------------------- 홍보 e리플렛

def _promo(conn, account, assignment_school):
    row = conn.execute('SELECT * FROM ih_promos WHERE account_id=? AND school_name=?',
                       (account['id'], assignment_school['school_name'])).fetchone()
    if row:
        return row
    conn.execute('''
        INSERT OR IGNORE INTO ih_promos (account_id, school_id, school_name) VALUES (?, ?, ?)
    ''', (account['id'], assignment_school['school_id'], assignment_school['school_name']))
    conn.commit()
    return conn.execute('SELECT * FROM ih_promos WHERE account_id=? AND school_name=?',
                        (account['id'], assignment_school['school_name'])).fetchone()


def _draft_pages(conn, promo_id):
    return conn.execute('SELECT * FROM ih_promo_pages WHERE promo_id=? ORDER BY position, id',
                        (promo_id,)).fetchall()


def _clear_drafts(conn, promo_id):
    for page in _draft_pages(conn, promo_id):
        delete_file(page['path'])
    conn.execute('DELETE FROM ih_promo_pages WHERE promo_id=?', (promo_id,))


def _apply_pages_to_leaflet(conn, ebook_id, pages):
    """리플렛의 쪽을 주어진 초안 쪽들로 통째로 바꾼다(파일은 복사해 둔다)."""
    old_paths = [row['image_path'] for row in conn.execute(
        'SELECT image_path FROM ebook_pages WHERE ebook_id=?', (ebook_id,)).fetchall()]
    conn.execute('DELETE FROM ebook_pages WHERE ebook_id=?', (ebook_id,))
    folder = ebook_mod.LEAFLET_ROOT / str(ebook_id)
    folder.mkdir(parents=True, exist_ok=True)
    first = None
    for number, page in enumerate(pages, 1):
        destination = folder / encrypted_storage_name(f'page{number}.jpg')
        shutil.copy2(page['path'], destination)
        conn.execute('''
            INSERT INTO ebook_pages (ebook_id, page_no, content_html, image_filename, image_path, width, height)
            VALUES (?, ?, '', ?, ?, ?, ?)
        ''', (ebook_id, number, page['name'], str(destination), page['width'], page['height']))
        if first is None:
            first = (page['name'], str(destination))
    if first:
        conn.execute('''
            UPDATE ebooks SET cover_filename=?, cover_path=?, source_filename=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (first[0], first[1], first[0], ebook_id))
    for path in old_paths:
        delete_file(path)


def _ensure_leaflet(conn, account, promo):
    """홍보 리플렛이 없으면 새로 만든다. 반환: ebook id."""
    if promo['ebook_id'] and conn.execute('SELECT 1 FROM ebooks WHERE id=?', (promo['ebook_id'],)).fetchone():
        return promo['ebook_id']
    title = f"{promo['school_name']} {account['name']} 강사 홍보"
    ebook_id = conn.execute('''
        INSERT INTO ebooks (title, author, description, cover_filename, cover_path, source_filename,
                            content_text, page_char_limit, created_by, kind, share_token,
                            approval_status, ih_account_id)
        VALUES (?, ?, ?, '', '', '', '', 1800, ?, 'leaflet', ?, 'pending', ?)
    ''', (title, account['name'], f"{promo['school_name']} 강사 홍보 e리플렛",
          f"강사 {account['name']}", secrets.token_urlsafe(24), account['id'])).lastrowid
    conn.execute('UPDATE ih_promos SET ebook_id=? WHERE id=?', (ebook_id, promo['id']))
    return ebook_id


def submit_promo(conn, account, assignment, uploads):
    """홍보 자료를 올린다. 해당 학교 e리플렛에 자동 등록되고(없으면 생성) 승인대기가 된다."""
    try:
        frames = _frames_from_uploads(uploads)
    except ValueError as exc:
        return str(exc)
    promo = _promo(conn, account, assignment)
    _clear_drafts(conn, promo['id'])
    folder = PROMO_ROOT / str(promo['id'])
    for number, (jpeg, width, height, name) in enumerate(frames, 1):
        conn.execute('''
            INSERT INTO ih_promo_pages (promo_id, position, path, name, width, height) VALUES (?, ?, ?, ?, ?, ?)
        ''', (promo['id'], number, _store_jpeg(folder, f'draft{number}', jpeg), name, width, height))
    ebook_id = _ensure_leaflet(conn, account, promo)
    book = conn.execute('SELECT approval_status FROM ebooks WHERE id=?', (ebook_id,)).fetchone()
    if book['approval_status'] != 'approved':
        # 아직 공개된 적 없는 리플렛은 곧바로 내용을 채워 둔다(본사 담당자가 미리 볼 수 있다).
        _apply_pages_to_leaflet(conn, ebook_id, _draft_pages(conn, promo['id']))
        conn.execute("UPDATE ebooks SET approval_status='pending' WHERE id=?", (ebook_id,))
    conn.execute('''
        UPDATE ih_promos SET status='pending', submitted_at=?, reject_reason='' WHERE id=?
    ''', (_now_text(), promo['id']))
    conn.commit()
    return None


def review_promo(conn, promo_id, approve, reviewer, reason=''):
    promo = conn.execute('SELECT * FROM ih_promos WHERE id=?', (promo_id,)).fetchone()
    if not promo or promo['status'] != 'pending' or not promo['ebook_id']:
        return '승인대기 중인 홍보페이지가 아닙니다.'
    if approve:
        drafts = _draft_pages(conn, promo_id)
        if drafts:
            _apply_pages_to_leaflet(conn, promo['ebook_id'], drafts)
            _clear_drafts(conn, promo_id)
        conn.execute("UPDATE ebooks SET approval_status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (promo['ebook_id'],))
        conn.execute('''
            UPDATE ih_promos SET status='approved', reviewed_by=?, reviewed_at=?, reject_reason='' WHERE id=?
        ''', (reviewer, _now_text(), promo_id))
    else:
        # 이미 공개 중인 리플렛은 그대로 두고, 새로 올린 초안만 반려한다.
        conn.execute("UPDATE ebooks SET approval_status='rejected' WHERE id=? AND approval_status<>'approved'",
                     (promo['ebook_id'],))
        conn.execute('''
            UPDATE ih_promos SET status='rejected', reviewed_by=?, reviewed_at=?, reject_reason=? WHERE id=?
        ''', (reviewer, _now_text(), _text(reason, 300), promo_id))
    conn.commit()
    return None


# ---------------------------------------------------------------- 강사 포털(링크 + 비밀번호)

def _failure_key(token):
    return f"{token}:{request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()}"


def _locked(key):
    now = time.time()
    recent = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_failures[key] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex'
    return response


def _account_by_token(conn, token):
    return conn.execute('SELECT * FROM ih_accounts WHERE token=? AND is_active=1', (token,)).fetchone()


def _portal_context(conn, token):
    """링크 토큰과 로그인 세션을 모두 확인한다. 반환: (계정 또는 None, 로그인 여부)."""
    account = _account_by_token(conn, token)
    if not account:
        return None, False
    return account, session.get(SESSION_KEY) == account['id']


def _assignments(conn, account_id):
    return conn.execute('''
        SELECT * FROM ih_assignments WHERE account_id=? ORDER BY school_name, department
    ''', (account_id,)).fetchall()


def _own_class(conn, account, class_id):
    """내가 쓸 수 있는 과목인지(수동·자동 연결) 확인하고 출석 설정까지 합친 정보를 돌려준다."""
    if not any(row['id'] == class_id for row in accessible_classes(conn, account)):
        return None
    return sa._load_class(conn, class_id)


def _school_options(assignments):
    """학교별로 묶는다(같은 학교의 여러 강의부서는 한 학교로 본다)."""
    seen, result = set(), []
    for item in assignments:
        key = norm(item['school_name'])
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


@instructor_hub_bp.route('/teacher-hub/<token>', methods=['GET', 'POST'])
def portal_login(token):
    conn = get_db()
    try:
        account = _account_by_token(conn, token)
        if not account:
            return _no_store(Response(render_template('instructor_hub/portal_login.html', invalid=True), 404))
        if session.get(SESSION_KEY) == account['id']:
            return redirect(url_for('instructor_hub.portal_home', token=token))
        error = ''
        if request.method == 'POST':
            key = _failure_key(token)
            if _locked(key):
                error = '비밀번호가 여러 번 맞지 않아 잠시 막혔습니다. 10분 뒤 다시 시도해 주세요.'
            elif check_password_hash(account['pw_hash'], request.form.get('password', '').strip()):
                session[SESSION_KEY] = account['id']
                conn.execute('UPDATE ih_accounts SET last_login_at=? WHERE id=?', (_now_text(), account['id']))
                conn.commit()
                return redirect(url_for('instructor_hub.portal_home', token=token))
            else:
                _login_failures.setdefault(key, []).append(time.time())
                error = '비밀번호가 맞지 않습니다. 문자로 받은 비밀번호를 다시 확인해 주세요.'
        return _no_store(Response(render_template(
            'instructor_hub/portal_login.html', invalid=False, account=account, error=error)))
    finally:
        conn.close()


@instructor_hub_bp.route('/teacher-hub/<token>/logout', methods=['POST'])
def portal_logout(token):
    session.pop(SESSION_KEY, None)
    return redirect(url_for('instructor_hub.portal_login', token=token))


def _portal_guard(conn, token):
    """공통 검사. 통과하면 계정을, 아니면 (None, 응답)을 돌려준다."""
    account, logged_in = _portal_context(conn, token)
    if not account:
        return None, _no_store(Response(render_template('instructor_hub/portal_login.html', invalid=True), 404))
    if not logged_in:
        return None, redirect(url_for('instructor_hub.portal_login', token=token))
    return account, None


@instructor_hub_bp.route('/teacher-hub/<token>/home')
def portal_home(token):
    tab = request.args.get('tab') if request.args.get('tab') in ('profile', 'promo', 'attendance') else 'profile'
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        assignments = _assignments(conn, account['id'])
        profile = _profile(conn, account['id'])
        promos = []
        for item in _school_options(assignments):
            promo = _promo(conn, account, item)
            book = conn.execute('SELECT approval_status, share_token FROM ebooks WHERE id=?',
                                (promo['ebook_id'],)).fetchone() if promo['ebook_id'] else None
            promos.append({
                'school_name': item['school_name'], 'school_id': item['school_id'],
                'status': promo['status'], 'reject_reason': promo['reject_reason'],
                'submitted_at': promo['submitted_at'] or '',
                'live': bool(book and book['approval_status'] == 'approved'),
                'live_url': (_external_root() + url_for('ebook.public_reader', token=book['share_token']))
                if book and book['approval_status'] == 'approved' else '',
                'draft_count': len(_draft_pages(conn, promo['id'])),
            })
        classes = []
        grouped = {}
        for row in accessible_classes(conn, account):
            info = sa._load_class(conn, row['id'])
            grouped.setdefault((row['school_name'], row['department'] or ''), []).append({
                'id': info['id'], 'name': info['class_name'],
                'schedule': sa._schedule_label(info),
                'students': conn.execute('''
                    SELECT COUNT(*) c FROM parent_class_students cs
                    JOIN parent_students s ON s.id=cs.student_id AND s.is_active=1
                    WHERE cs.class_id=?''', (info['id'],)).fetchone()['c'],
                'live_url': sa._live_url(info), 'display_url': sa._display_url(info),
            })
        shown = set()
        for item in assignments:
            key = (item['school_name'], item['department'] or '')
            shown.add(key)
            classes.append({'school_name': key[0], 'department': key[1], 'classes': grouped.get(key, [])})
        for key, items in grouped.items():          # 수동으로 연결된 다른 부서 과목
            if key not in shown:
                classes.append({'school_name': key[0], 'department': key[1], 'classes': items})
        photo_exists = bool(profile['photo_path'] or profile['draft_photo_path'])
        return _no_store(Response(render_template(
            'instructor_hub/portal_home.html', account=account, token=token, tab=tab, csrf=_csrf_token(),
            profile=profile, photo_exists=photo_exists, promos=promos, class_groups=classes,
            status_labels=STATUS_LABELS, weekday_labels=sa.WEEKDAY_LABELS,
            messages=get_flashed_messages(with_categories=True),
        )))
    finally:
        conn.close()


def _back(token, tab):
    return redirect(url_for('instructor_hub.portal_home', token=token, tab=tab))


@instructor_hub_bp.route('/teacher-hub/<token>/profile', methods=['POST'])
def portal_save_profile(token):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        _check_csrf()
        error = submit_profile(conn, account['id'], request.form.get('bio'), request.files.get('photo'))
        flash(error or '프로필을 제출했습니다. 본사 담당자가 승인하면 학부모 안내 페이지에 보입니다.',
              'error' if error else 'success')
    finally:
        conn.close()
    return _back(token, 'profile')


@instructor_hub_bp.route('/teacher-hub/<token>/photo')
def portal_photo(token):
    """내 사진 미리보기(승인대기 중인 새 사진이 있으면 그것을 보여 준다)."""
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        profile = _profile(conn, account['id'])
    finally:
        conn.close()
    path = profile['draft_photo_path'] or profile['photo_path']
    if not path or not Path(path).is_file():
        abort(404)
    return _no_store(encrypted_response(path, 'photo.jpg', as_attachment=False, mimetype='image/jpeg'))


@instructor_hub_bp.route('/teacher-hub/<token>/promo', methods=['POST'])
def portal_save_promo(token):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        _check_csrf()
        school_name = _text(request.form.get('school_name'), 100)
        assignment = next((a for a in _assignments(conn, account['id']) if a['school_name'] == school_name), None)
        if not assignment:
            flash('내 학교가 아닙니다.', 'error')
        else:
            error = submit_promo(conn, account, assignment, request.files.getlist('files'))
            flash(error or f'‘{school_name}’ e리플렛에 등록했습니다. 본사 담당자가 승인하면 외부 링크가 열립니다.',
                  'error' if error else 'success')
    finally:
        conn.close()
    return _back(token, 'promo')


@instructor_hub_bp.route('/teacher-hub/<token>/classes/<int:class_id>')
def portal_students(token, class_id):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        info = _own_class(conn, account, class_id)
        if not info:
            abort(404)
        students = sa._roster(conn, class_id)
        return _no_store(Response(render_template(
            'instructor_hub/portal_students.html', account=account, token=token, csrf=_csrf_token(),
            info=info, students=students, schedule=sa._schedule_label(info),
            messages=get_flashed_messages(with_categories=True),
        )))
    finally:
        conn.close()


@instructor_hub_bp.route('/teacher-hub/<token>/classes/<int:class_id>/students', methods=['POST'])
def portal_student_action(token, class_id):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        _check_csrf()
        info = _own_class(conn, account, class_id)
        if not info:
            abort(404)
        action = request.form.get('action')
        if action == 'add':
            error = sa._save_student(conn, info, {
                'name': request.form.get('name'), 'grade': request.form.get('grade'),
                'classroom': request.form.get('classroom'),
                'guardian_name': request.form.get('guardian_name'),
                'guardian_phone': request.form.get('guardian_phone'),
            })
            if error:
                conn.rollback()
                flash(error, 'error')
            else:
                conn.commit()
                flash('학생을 등록했습니다.', 'success')
        elif action == 'bulk':
            lines = [line.strip() for line in str(request.form.get('text') or '').splitlines() if line.strip()]
            errors = []
            if not lines:
                errors.append('등록할 학생 명단을 입력해 주세요.')
            elif len(lines) > 300:
                errors.append('한 번에 300명까지 등록할 수 있습니다.')
            else:
                import re
                for number, line in enumerate(lines, 1):
                    parts = [part.strip() for part in re.split(r'[,\t]', line)] + [''] * 5
                    error = sa._save_student(conn, info, {
                        'name': parts[0], 'grade': re.sub(r'학년$', '', parts[1]),
                        'classroom': re.sub(r'반$', '', parts[2]),
                        'guardian_name': parts[3], 'guardian_phone': parts[4],
                    })
                    if error:
                        errors.append(f'{number}번째 줄 - {error}')
            if errors:
                conn.rollback()
                flash('명단을 확인해 주세요. 아무것도 등록하지 않았습니다. ' + ' / '.join(errors[:5]), 'error')
            else:
                conn.commit()
                flash(f'학생 {len(lines)}명을 등록했습니다.', 'success')
        elif action in ('remove', 'reset'):
            student_id = _int(request.form.get('student_id'))
            enrolled = student_id and conn.execute(
                'SELECT 1 FROM parent_class_students WHERE class_id=? AND student_id=?',
                (class_id, student_id)).fetchone()
            if enrolled and action == 'remove':
                conn.execute('DELETE FROM parent_class_students WHERE class_id=? AND student_id=?',
                             (class_id, student_id))
                conn.commit()
                flash('명단에서 뺐습니다. 지난 출석 기록은 보존됩니다.', 'success')
            elif enrolled:
                conn.execute('DELETE FROM student_att_devices WHERE student_id=?', (student_id,))
                conn.commit()
                flash(sa.RESET_DEVICE_MESSAGE, 'success')
    finally:
        conn.close()
    return redirect(url_for('instructor_hub.portal_students', token=token, class_id=class_id))


# ---------------------------------------------------------------- 본사 담당자 관리(인트라넷 메뉴)
# 화면은 routes/support_hub.py(학부모·강사 통합지원)가 그린다. 아래 함수들은 그 화면이 쓰는 자료 모으기와 처리 경로다.

ADMIN_SCHOOL_SESSION = 'ih_admin_school'
REVIEW_THUMBS = 5


def _admin_name():
    return str(session.get('user_name') or session.get('emp_no') or '')


def _catalog_schools(conn):
    """[학교업무공간]에 등록된 학교(이름 기준 한 줄, 최신 연도 우선)."""
    seen, result = set(), []
    for school in sa._list_schools(conn):
        key = norm(school['name'])
        if key and key not in seen:
            seen.add(key)
            result.append(school)
    return result


def _all_assignments(conn):
    return conn.execute('''
        SELECT s.*, a.name AS account_name, a.phone AS account_phone, a.email AS account_email,
               a.token AS account_token, a.pw_enc AS account_pw_enc, a.is_active AS account_active,
               a.last_login_at AS last_login_at, a.sms_sent_at AS sms_sent_at
        FROM ih_assignments s JOIN ih_accounts a ON a.id=s.account_id
        ORDER BY s.school_name, s.department, a.name COLLATE NOCASE
    ''').fetchall()


def _status_maps(conn):
    profiles = {r['account_id']: r['status'] for r in conn.execute('SELECT account_id, status FROM ih_profiles')}
    promos = {(r['account_id'], norm(r['school_name'])): r['status']
              for r in conn.execute('SELECT account_id, school_name, status FROM ih_promos')}
    return profiles, promos


def _school_choices(conn):
    """선택 상자용 학교 목록. 반환: (학교업무공간 학교들, 예전 자료에만 남은 학교들, 승인 대기 전체 건수)."""
    catalog = _catalog_schools(conn)
    profiles, promos = _status_maps(conn)
    by_school = {}
    for row in _all_assignments(conn):
        by_school.setdefault(norm(row['school_name']), []).append(row)
    class_count = {}
    for row in conn.execute('SELECT school_name FROM parent_classes WHERE is_active=1'):
        class_count[norm(row['school_name'])] = class_count.get(norm(row['school_name']), 0) + 1

    def summarize(name, school_id, key, matched):
        rows = by_school.get(key, [])
        accounts = {r['account_id']: r for r in rows if r['account_active']}
        pending = sum(1 for a in accounts if profiles.get(a) == 'pending')
        pending += sum(1 for a in accounts if promos.get((a, key)) == 'pending')
        return {
            'name': name, 'school_id': school_id, 'matched': matched, 'instructors': len(accounts),
            'unused': sum(1 for r in accounts.values() if not r['last_login_at']), 'pending': pending,
            'classes': class_count.get(key, 0), 'departments': len({norm(r['department']) for r in rows}),
        }

    schools = sorted((summarize(s['name'], s['id'], norm(s['name']), True) for s in catalog),
                     key=lambda item: item['name'])
    known = {norm(s['name']) for s in catalog}
    legacy = sorted((summarize(rows[0]['school_name'], None, key, False)
                     for key, rows in by_school.items() if key not in known), key=lambda item: item['name'])
    # 전체 승인 대기: 지금 어느 학교에든 배정된 사용 가능한 강사의 자료만 센다(학교에서 뺀 강사의 남은 자료는 제외).
    assigned = {}
    for rows in by_school.values():
        for row in rows:
            if row['account_active']:
                assigned.setdefault(row['account_id'], set()).add(norm(row['school_name']))
    pending_total = sum(1 for account in assigned if profiles.get(account) == 'pending') \
        + sum(1 for (account, key), status in promos.items()
              if status == 'pending' and key in assigned.get(account, ()))
    return schools, legacy, pending_total


def _selected_school(schools, legacy):
    """고른 학교. 주소의 school 값을 쓰고, 없으면 마지막으로 본 학교를 기억해 둔 값을 쓴다."""
    if 'school' in request.args:
        wanted = _text(request.args.get('school'), 100)
        session[ADMIN_SCHOOL_SESSION] = wanted            # 빈 값이면 '학교 선택 해제'(전체 현황)
    else:
        wanted = session.get(ADMIN_SCHOOL_SESSION, '')
    key = norm(wanted)
    if not key:
        return None
    return next((item for item in schools + legacy if norm(item['name']) == key), None)


def _allow_unmatched(conn, school_name):
    """학교업무공간에 없지만 예전 자료에 남아 있는 학교는 계속 관리할 수 있게 한다."""
    if canonical_school(conn, school_name)[0] is not None:
        return False
    key = norm(school_name)
    return any(norm(row['school_name']) == key for row in _all_assignments(conn))


def _school_instructors(conn, school_name):
    """① 화면용: 강의부서별 강사 목록(접속 링크·비밀번호·승인 상태 포함)."""
    key = norm(school_name)
    profiles, promos = _status_maps(conn)
    groups = {}
    for row in _all_assignments(conn):
        if norm(row['school_name']) != key:
            continue
        groups.setdefault(row['department'], []).append({
            'assignment_id': row['id'], 'id': row['account_id'], 'name': row['account_name'],
            'phone': format_phone(row['account_phone']), 'email': row['account_email'] or '',
            'department': row['department'], 'is_active': bool(row['account_active']),
            'url': _portal_url({'token': row['account_token']}), 'password': _decrypt(row['account_pw_enc']),
            'last_login_at': row['last_login_at'] or '', 'sms_sent_at': row['sms_sent_at'] or '',
            'profile_status': profiles.get(row['account_id'], 'none'),
            'promo_status': promos.get((row['account_id'], key), 'none'),
        })
    return [{'name': dept, 'people': sorted(items, key=lambda item: item['name'])}
            for dept, items in sorted(groups.items(), key=lambda pair: (pair[0] == '', pair[0]))]


def _school_classes(conn, school_name):
    """이 학교의 학생출석관리 과목과 연결된 강사(강의부서 이름이 맞는지 확인용)."""
    key = norm(school_name)
    result = []
    for row in conn.execute('''
        SELECT id, school_name, department, class_name, instructor_name, instructor_emp_no
        FROM parent_classes WHERE is_active=1 ORDER BY department, class_name COLLATE NOCASE, id
    ''').fetchall():
        if norm(row['school_name']) != key:
            continue
        linked = linked_accounts(conn, row)
        result.append({
            'id': row['id'], 'name': row['class_name'], 'department': row['department'] or '',
            'manual': bool(manual_account_id(row)), 'instructors': [a['name'] for a in linked],
            'students': conn.execute('''
                SELECT COUNT(*) c FROM parent_class_students cs
                JOIN parent_students s ON s.id=cs.student_id AND s.is_active=1 WHERE cs.class_id=?''',
                (row['id'],)).fetchone()['c'],
        })
    return result


def _department_suggestions(conn, school_name):
    """강의부서 입력칸의 추천 목록: 이 학교의 부서·과목 분야를 먼저, 다른 학교에서 쓴 부서도 함께."""
    names = list(school_departments(conn, school_name))
    key = norm(school_name)
    for row in conn.execute("SELECT school_name, department FROM parent_classes WHERE is_active=1 "
                            "AND COALESCE(department, '')<>''").fetchall():
        if norm(row['school_name']) == key and row['department'] not in names:
            names.append(row['department'])
    for row in conn.execute("SELECT DISTINCT department FROM ih_assignments WHERE department<>'' "
                            'ORDER BY department').fetchall():
        if row['department'] not in names:
            names.append(row['department'])
    return names


def _review_rows(conn, school_name):
    """② 화면용: 이 학교 강사마다 한 줄 — 프로필과 홍보 리플렛 자료를 함께 보여 준다."""
    key = norm(school_name)
    people = {}
    for row in _all_assignments(conn):
        if norm(row['school_name']) != key:
            continue
        person = people.setdefault(row['account_id'], {
            'account_id': row['account_id'], 'name': row['account_name'], 'phone': format_phone(row['account_phone']),
            'is_active': bool(row['account_active']), 'departments': []})
        if row['department'] and row['department'] not in person['departments']:
            person['departments'].append(row['department'])
    rows = []
    for person in people.values():
        profile = conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (person['account_id'],)).fetchone()
        status = profile['status'] if profile else 'none'
        if profile and status in ('pending', 'rejected'):
            bio, photo, which = profile['draft_bio'], bool(profile['draft_photo_path'] or profile['photo_path']), 'draft'
        elif profile:
            bio, photo, which = profile['bio'], bool(profile['photo_path']), 'live'
        else:
            bio, photo, which = '', False, 'live'
        promo = next((p for p in conn.execute('SELECT * FROM ih_promos WHERE account_id=?',
                                             (person['account_id'],)).fetchall()
                      if norm(p['school_name']) == key), None)
        pages, from_ebook = [], False
        if promo:
            pages = _draft_pages(conn, promo['id'])
            if not pages and promo['ebook_id']:
                pages = conn.execute('SELECT id, page_no FROM ebook_pages WHERE ebook_id=? ORDER BY page_no',
                                     (promo['ebook_id'],)).fetchall()
                from_ebook = True
        promo_status = promo['status'] if promo else 'none'
        rows.append({
            **person,
            'profile': {'status': status, 'bio': bio, 'has_photo': photo, 'which': which,
                        'old_bio': profile['bio'] if profile and status == 'pending' else '',
                        'submitted_at': (profile['submitted_at'] or '')[:16] if profile else '',
                        'reject_reason': profile['reject_reason'] if profile else ''},
            'promo': {'status': promo_status, 'id': promo['id'] if promo else None,
                      'ebook_id': promo['ebook_id'] if promo else None, 'from_ebook': from_ebook,
                      'pages': [row['id'] for row in pages[:REVIEW_THUMBS]], 'total': len(pages),
                      'submitted_at': (promo['submitted_at'] or '')[:16] if promo else '',
                      'reject_reason': promo['reject_reason'] if promo else ''},
            'needs_action': status == 'pending' or promo_status == 'pending',
        })
    rows.sort(key=lambda r: (not r['departments'], ' '.join(r['departments']), r['name']))
    return rows


def _everyone(conn):
    """전체 현황의 강사 검색용: 학교·강의부서마다 한 줄."""
    profiles, promos = _status_maps(conn)
    return [{
        'id': row['account_id'], 'name': row['account_name'], 'phone': format_phone(row['account_phone']),
        'school': row['school_name'], 'department': row['department'], 'is_active': bool(row['account_active']),
        'last_login_at': (row['last_login_at'] or '')[:10],
        'profile_status': profiles.get(row['account_id'], 'none'),
        'promo_status': promos.get((row['account_id'], norm(row['school_name'])), 'none'),
    } for row in _all_assignments(conn)]


def _orphan_accounts(conn):
    """어느 학교에도 배정되지 않은 강사 계정(실수로 만든 계정을 정리할 때 쓴다)."""
    return [{'id': row['id'], 'name': row['name'], 'phone': format_phone(row['phone'])}
            for row in conn.execute('''
        SELECT * FROM ih_accounts a WHERE NOT EXISTS (SELECT 1 FROM ih_assignments s WHERE s.account_id=a.id)
        ORDER BY a.name COLLATE NOCASE
    ''').fetchall()]


TAB_ALIASES = {'register': 'instructors', 'schools': 'instructors', 'links': 'instructors', 'review': 'review',
               'instructors': 'instructors'}


def _admin_url(tab='instructors', school=None):
    """통합지원 화면 주소(예전 탭 이름도 받는다)."""
    return url_for('support_hub.admin_page', tab=TAB_ALIASES.get(tab, tab), school=school or None)


def _redirect_back(default_tab='instructors'):
    """작업을 마친 뒤 원래 보던 화면(학교·탭)으로 돌아간다."""
    target = request.form.get('next') or ''
    if target.startswith(('/support-hub', '/instructor-hub', '/parent-hub')) and not target.startswith('//'):
        return redirect(target)
    return redirect(_admin_url(default_tab))


@instructor_hub_bp.route('/instructor-hub/')
@instructor_hub_bp.route('/instructor-hub')
@menu_permission_required(MENU_KEY)
def admin_page():
    """예전 강사통합지원 주소. 지금은 학부모·강사 통합지원 화면으로 보낸다."""
    return redirect(_admin_url(request.args.get('tab') or 'instructors', request.args.get('school')))


@instructor_hub_bp.route('/instructor-hub/school')
@menu_permission_required(MENU_KEY)
def admin_school():
    return redirect(_admin_url('instructors', _text(request.args.get('name'), 100)))


# -- 강사 등록(한 명 · 여러 명)

@instructor_hub_bp.route('/instructor-hub/accounts/register', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_register():
    school_name = _text(request.form.get('school_name'), 100)
    conn = get_db()
    try:
        result = register_instructor(
            conn, school_name, request.form.get('name'), request.form.get('phone'),
            request.form.get('department'), request.form.get('email'),
            allow_unmatched=_allow_unmatched(conn, school_name))
        conn.commit()
    finally:
        conn.close()
    if not result['ok']:
        flash(result['error'], 'error')
    else:
        name = result['account']['name']
        if result['created']:
            message = f'{name} 강사를 등록했습니다. 접속 링크와 6자리 비밀번호가 만들어졌어요. 아래 목록에서 확인하고 문자로 보낼 수 있습니다.'
        elif result['assigned'] or result['reactivated']:
            message = f"{name} 강사는 이미 있는 계정이라 {result['school_name']} · {result['department']}에 더했습니다."
        else:
            flash(f"{name} 강사는 이 학교 · {result['department']}에 이미 등록되어 있습니다.", 'error')
            return _redirect_back()
        if result['note']:
            message += f" ({result['note']})"
        flash(message, 'success')
    return _redirect_back()


@instructor_hub_bp.route('/instructor-hub/accounts/import', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_import():
    """붙여넣은 글 또는 엑셀(.xlsx)로 강사를 한꺼번에 등록한다."""
    school_name = _text(request.form.get('school_name'), 100)
    upload = request.files.get('file')
    try:
        rows = parse_instructor_workbook(upload) if upload and upload.filename \
            else parse_instructor_text(request.form.get('text'))
    except ValueError as exc:
        flash(str(exc), 'error')
        return _redirect_back()
    if not rows:
        flash('등록할 강사 줄이 없습니다. 이름·휴대폰·강의부서를 줄마다 적거나 엑셀 파일을 골라 주세요.', 'error')
        return _redirect_back()
    if len(rows) > MAX_IMPORT_ROWS:
        flash(f'한 번에 {MAX_IMPORT_ROWS}줄까지 등록할 수 있습니다. 나누어 올려 주세요.', 'error')
        return _redirect_back()
    conn = get_db()
    try:
        summary = register_many(conn, rows, school_name, allow_unmatched=_allow_unmatched(conn, school_name))
        conn.commit()
    finally:
        conn.close()
    flash(import_message(summary), 'error' if summary['errors'] else 'success')
    return _redirect_back()


@instructor_hub_bp.route('/instructor-hub/accounts/template.xlsx')
@menu_permission_required(MENU_KEY)
def admin_template():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = '강사등록'
    sheet.append(['이름', '휴대폰', '강의부서', '이메일(선택)', '학교(선택)'])
    sheet.append(['홍길동', '010-1234-5678', '로봇과학', '', ''])
    sheet.append(['김미술', '010-2345-6789', '미술', 'art@example.com', ''])
    for column, width in zip('ABCDE', (14, 18, 18, 24, 22)):
        sheet.column_dimensions[column].width = width
    note = book.create_sheet('작성방법')
    for line in ('1. 이름·휴대폰·강의부서는 꼭 적어 주세요. 이메일·학교는 비워도 됩니다.',
                 '2. 학교 칸을 비우면 화면 위에서 고른 학교에 등록됩니다. 학교업무공간에 등록된 학교 이름을 그대로 적으면 학교별로 한 번에 등록할 수 있어요.',
                 '3. 같은 휴대폰번호의 강사가 이미 있으면 새로 만들지 않고 그 강사에게 학교·강의부서만 더합니다.',
                 '4. 예시 두 줄은 지우고 올려 주세요. (.xlsx 파일, 한 번에 300줄까지)'):
        note.append([line])
    note.column_dimensions['A'].width = 110
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return send_file(stream, as_attachment=True, download_name='강사등록양식.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@instructor_hub_bp.route('/instructor-hub/accounts/export.xlsx')
@menu_permission_required(MENU_KEY)
def admin_export():
    """한 학교 강사의 접속 링크·비밀번호를 엑셀로 내려받는다(다른 방법으로 안내할 때 쓴다)."""
    from openpyxl import Workbook
    school_name = _text(request.args.get('school'), 100)
    conn = get_db()
    try:
        groups = _school_instructors(conn, school_name)
    finally:
        conn.close()
    book = Workbook()
    sheet = book.active
    sheet.title = '강사 접속정보'
    sheet.append(['이름', '휴대폰', '강의부서', '접속 링크', '비밀번호', '최근 접속', '안내 문자 발송일'])
    for group in groups:
        for item in group['people']:
            sheet.append([item['name'], item['phone'], item['department'], item['url'], item['password'],
                          item['last_login_at'][:10], item['sms_sent_at'][:10]])
    for column, width in zip('ABCDEFG', (14, 16, 16, 60, 12, 12, 16)):
        sheet.column_dimensions[column].width = width
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    response = send_file(stream, as_attachment=True, download_name=f'{school_name}_강사접속정보.xlsx',
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response.headers['Cache-Control'] = 'no-store'
    return response


# -- 강사 한 명 관리(비밀번호·링크 재발급, 문자, 사용 중지, 정보 수정, 학교에서 빼기)

def _account_or_404(conn, account_id):
    account = conn.execute('SELECT * FROM ih_accounts WHERE id=?', (account_id,)).fetchone()
    if not account:
        abort(404)
    return account


@instructor_hub_bp.route('/instructor-hub/accounts/<int:account_id>/<action>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_account_action(account_id, action):
    conn = get_db()
    try:
        account = _account_or_404(conn, account_id)
        if action == 'reset-password':
            password = _new_password()
            conn.execute('UPDATE ih_accounts SET pw_hash=?, pw_enc=? WHERE id=?',
                         (generate_password_hash(password), _encrypt(password), account_id))
            flash(f'{account["name"]} 강사의 비밀번호를 새로 만들었습니다.', 'success')
        elif action == 'reissue-link':
            conn.execute('UPDATE ih_accounts SET token=? WHERE id=?', (secrets.token_urlsafe(24), account_id))
            flash(f'{account["name"]} 강사의 접속 링크를 새로 만들었습니다. 예전 링크는 더 이상 열리지 않습니다.', 'success')
        elif action == 'toggle':
            conn.execute('UPDATE ih_accounts SET is_active=1-is_active WHERE id=?', (account_id,))
            flash('사용 상태를 바꿨습니다.', 'success')
        elif action == 'edit':
            name, phone = _clean_name(request.form.get('name'), 60), clean_phone(request.form.get('phone'))
            if not name or not PHONE_PATTERN.fullmatch(phone):
                flash('이름과 휴대폰번호(010으로 시작하는 10~11자리)를 확인해 주세요.', 'error')
            elif conn.execute('SELECT 1 FROM ih_accounts WHERE phone=? AND id<>?', (phone, account_id)).fetchone():
                flash('같은 휴대폰번호의 다른 강사 계정이 이미 있습니다.', 'error')
            else:
                conn.execute('UPDATE ih_accounts SET name=?, phone=?, email=? WHERE id=?',
                             (name, phone, _text(request.form.get('email'), 120), account_id))
                flash(f'{name} 강사 정보를 고쳤습니다.', 'success')
        elif action == 'set-department':
            department = _clean_name(request.form.get('department'), 60)
            if not department:
                flash('강의부서를 입력해 주세요.', 'error')
            else:
                try:
                    changed = conn.execute('UPDATE ih_assignments SET department=? WHERE id=? AND account_id=?',
                                           (department, _int(request.form.get('assignment_id')), account_id)).rowcount
                    flash('강의부서를 바꿨습니다.' if changed else '배정 정보를 찾을 수 없습니다.',
                          'success' if changed else 'error')
                except sqlite3.IntegrityError:
                    flash('이 학교에 같은 강의부서로 이미 배정되어 있습니다.', 'error')
        elif action == 'unassign':
            removed = conn.execute('DELETE FROM ih_assignments WHERE id=? AND account_id=?',
                                   (_int(request.form.get('assignment_id')), account_id)).rowcount
            flash(f'{account["name"]} 강사를 이 학교에서 뺐습니다. (계정과 올린 자료는 남아 있어요)' if removed
                  else '배정 정보를 찾을 수 없습니다.', 'success' if removed else 'error')
        elif action == 'delete':
            error = delete_account(conn, account_id)
            flash(error or f'{account["name"]} 강사 계정을 삭제했습니다.', 'error' if error else 'success')
        elif action == 'sms':
            return _send_access_sms(conn, account)
        else:
            abort(404)
        conn.commit()
    finally:
        conn.close()
    return _redirect_back()


def _access_sms_text(account):
    return (f"[새담] {account['name']} 강사님 강사통합지원 접속 안내\n"
            f"주소: {_portal_url(account)}\n비밀번호: {_decrypt(account['pw_enc'])}\n"
            "프로필·홍보페이지·학생출석을 이 주소에서 관리합니다.")


def _send_access_sms(conn, account):
    """강사에게 접속 링크와 비밀번호를 문자로 보낸다(담당자가 버튼을 눌렀을 때만)."""
    from .solapi_settings import send_bulk_text
    try:
        result = send_bulk_text([{'to': account['phone'], 'text': _access_sms_text(account)}])[0]
    except Exception as exc:  # 설정 누락 등
        flash(f'문자를 보내지 못했습니다: {exc}', 'error')
        return _redirect_back()
    if result.get('ok'):
        conn.execute('UPDATE ih_accounts SET sms_sent_at=? WHERE id=?', (_now_text(), account['id']))
        conn.commit()
        flash(f'{account["name"]} 강사에게 접속 안내 문자를 보냈습니다.', 'success')
    else:
        flash(f'문자를 보내지 못했습니다: {result.get("error") or "알 수 없는 오류"}', 'error')
    return _redirect_back()


@instructor_hub_bp.route('/instructor-hub/schools/sms', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_school_sms():
    """한 학교 강사들에게 접속 링크·비밀번호를 문자로 한꺼번에 보낸다(담당자가 확인 창에서 동의했을 때만)."""
    from .solapi_settings import send_bulk_text
    school_name = _text(request.form.get('school_name'), 100)
    only_unused = bool(request.form.get('only_unused'))
    conn = get_db()
    try:
        accounts = {}
        for row in _all_assignments(conn):
            if norm(row['school_name']) != norm(school_name) or not row['account_active']:
                continue
            if only_unused and row['last_login_at']:
                continue
            accounts[row['account_id']] = conn.execute('SELECT * FROM ih_accounts WHERE id=?',
                                                       (row['account_id'],)).fetchone()
        if not accounts:
            flash('문자를 보낼 강사가 없습니다.', 'error')
            return _redirect_back()
        targets = list(accounts.values())
        try:
            results = send_bulk_text([{'to': a['phone'], 'text': _access_sms_text(a)} for a in targets])
        except Exception as exc:
            flash(f'문자를 보내지 못했습니다: {exc}', 'error')
            return _redirect_back()
        sent, failed = 0, []
        for account, result in zip(targets, results):
            if result.get('ok'):
                sent += 1
                conn.execute('UPDATE ih_accounts SET sms_sent_at=? WHERE id=?', (_now_text(), account['id']))
            else:
                failed.append(f"{account['name']}({result.get('error') or '알 수 없는 오류'})")
        conn.commit()
    finally:
        conn.close()
    message = f'{school_name} 강사 {sent}명에게 접속 안내 문자를 보냈습니다.'
    if failed:
        message += f"\n보내지 못한 강사 {len(failed)}명: " + ', '.join(failed[:5])
    flash(message, 'error' if failed else 'success')
    return _redirect_back()


# -- 승인

@instructor_hub_bp.route('/instructor-hub/review/profile/<int:account_id>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_review_profile(account_id):
    conn = get_db()
    try:
        approve = request.form.get('decision') == 'approve'
        error = review_profile(conn, account_id, approve, _admin_name(), request.form.get('reason'))
    finally:
        conn.close()
    flash(error or ('프로필을 승인했습니다.' if approve else '프로필을 반려했습니다.'), 'error' if error else 'success')
    return _redirect_back('review')


@instructor_hub_bp.route('/instructor-hub/review/promo/<int:promo_id>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_review_promo(promo_id):
    conn = get_db()
    try:
        approve = request.form.get('decision') == 'approve'
        error = review_promo(conn, promo_id, approve, _admin_name(), request.form.get('reason'))
    finally:
        conn.close()
    flash(error or ('홍보페이지를 승인했습니다. 학교 e리플렛에 합쳐져 학부모에게 보입니다.' if approve else '홍보페이지를 반려했습니다.'),
          'error' if error else 'success')
    return _redirect_back('review')


@instructor_hub_bp.route('/instructor-hub/photo/<int:account_id>/<which>')
@menu_permission_required(MENU_KEY)
def admin_photo(account_id, which):
    conn = get_db()
    try:
        profile = conn.execute('SELECT * FROM ih_profiles WHERE account_id=?', (account_id,)).fetchone()
    finally:
        conn.close()
    if not profile:
        abort(404)
    path = (profile['draft_photo_path'] or profile['photo_path']) if which == 'draft' else profile['photo_path']
    if not path or not Path(path).is_file():
        abort(404)
    return encrypted_response(path, 'photo.jpg', as_attachment=False, mimetype='image/jpeg')


@instructor_hub_bp.route('/instructor-hub/promo-page/<int:page_id>')
@menu_permission_required(MENU_KEY)
def admin_promo_page(page_id):
    conn = get_db()
    try:
        page = conn.execute('SELECT path FROM ih_promo_pages WHERE id=?', (page_id,)).fetchone()
    finally:
        conn.close()
    if not page or not Path(page['path']).is_file():
        abort(404)
    return encrypted_response(page['path'], 'page.jpg', as_attachment=False, mimetype='image/jpeg')
