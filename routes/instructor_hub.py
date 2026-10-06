"""학교지원 > 강사통합지원.

전자계약을 마친 강사에게 개인별 접속 링크와 비밀번호를 부여하고, 강사는 그 링크로
자기 페이지에 들어가 아래 세 가지를 한다.

1. 강사프로필  : 사진과 소개글을 올린다. 본사 승인 후 학부모 안내 페이지에 보인다.
2. 홍보페이지  : 그림·PDF를 올리면 그 학교의 e리플렛으로 자동 등록된다(없으면 새로 만든다).
                  본사 승인 전에는 외부 링크가 열리지 않는다.
3. 학생출석관리: 출석 QR 화면, 학생 등록, 학부모 알림 등록 안내를 쓴다.

본사 담당자는 [강사통합지원] 메뉴에서 접속 계정·승인·학부모 링크페이지를 관리한다.
학부모 링크페이지는 학교×강의부서마다 하나씩 만들어 강사프로필·출석알림·홍보
e리플렛·교실안내를 한 모바일 화면으로 보여 준다.

강사 계정은 인트라넷 계정(emp_no)과 무관하다. 링크(비밀 토큰) + 비밀번호 + 별도 세션으로
보호하며, 이 세션은 인트라넷 메뉴 권한을 전혀 주지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import shutil
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import (
    Blueprint, Response, abort, flash, get_flashed_messages, jsonify, redirect,
    render_template, request, session, url_for,
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

MENU_KEY = 'school_instructor_hub'
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

        -- 강사가 일하는 학교·강의부서(전자계약 한 건이 한 줄을 만든다).
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

        -- 학부모에게 보여 주는 학교×강의부서 모바일 링크페이지.
        CREATE TABLE IF NOT EXISTS ih_link_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            school_name TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            notice TEXT NOT NULL DEFAULT '',
            show_profile INTEGER NOT NULL DEFAULT 1,
            show_attendance INTEGER NOT NULL DEFAULT 1,
            show_leaflet INTEGER NOT NULL DEFAULT 1,
            show_guide INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (school_name, department)
        );
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


def find_account(conn, name, phone):
    phone = normalize_phone(phone)
    if not phone:
        return None
    return conn.execute('''
        SELECT * FROM ih_accounts WHERE phone=? AND is_active=1
        ORDER BY (name=?) DESC, id LIMIT 1
    ''', (phone, _text(name, 60))).fetchone()


def add_assignment(conn, account_id, school_name, department, contract_id=None):
    school_name = _text(school_name, 100)
    if not school_name:
        return False
    cursor = conn.execute('''
        INSERT OR IGNORE INTO ih_assignments (account_id, school_id, school_name, department, contract_id)
        VALUES (?, ?, ?, ?, ?)
    ''', (account_id, _school_id_by_name(conn, school_name), school_name,
          _text(department, 60), contract_id))
    return cursor.rowcount > 0


def ensure_account_for_signer(conn, name, phone, email, school_name, department, contract_id=None):
    """전자계약 서명자에게 계정이 없으면 만들고, 계약의 학교·강의부서를 배정한다."""
    account = find_account(conn, name, phone)
    created = False
    if not account:
        account, _ = create_account(conn, name, phone, email)
        created = True
    add_assignment(conn, account['id'], school_name, department, contract_id)
    return account, created


def on_instructor_contract_completed(row, phone):
    """강사 전자계약이 완료되면 호출된다. 실패해도 계약 완료에는 영향을 주지 않는다."""
    try:
        conn = get_db()
        try:
            ensure_instructor_hub_schema(conn)
            ensure_account_for_signer(
                conn, row['signer_name'], phone or row['signer_phone'], row['signer_email'],
                row['school_name'], row['department'], row['id'],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # 계약 완료 응답을 막지 않는다.
        logger.exception('강사통합지원 계정 자동 부여 실패')


def sync_from_completed_contracts(conn):
    """이미 완료된 강사 전자계약 중 계정이 없는 강사를 한꺼번에 만든다. 반환: (새 계정 수, 배정 수)."""
    from .verified_contract_repository import contract_table
    table = contract_table('instructor')
    created = assigned = 0
    rows = conn.execute(f'''
        SELECT id, signer_name, signer_phone, signer_email, school_name, department
        FROM {table} WHERE status='completed' ORDER BY id
    ''').fetchall()
    for row in rows:
        if not normalize_phone(row['signer_phone']):
            continue
        account, was_created = ensure_account_for_signer(
            conn, row['signer_name'], row['signer_phone'], row['signer_email'],
            row['school_name'], row['department'], row['id'],
        )
        created += 1 if was_created else 0
        assigned += 1
    return created, assigned


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
            if pdfium is None:
                raise ValueError('이 서버에서는 PDF를 변환할 수 없습니다. 그림 파일로 올려 주세요.')
            try:
                document = pdfium.PdfDocument(BytesIO(data))
            except Exception as exc:
                raise ValueError(f'‘{name}’은(는) 정상적인 PDF가 아닙니다.') from exc
            try:
                for index in range(len(document)):
                    if len(frames) >= MAX_PROMO_PAGES:
                        break
                    page = document.get_page(index)
                    try:
                        scale = PDF_DPI / 72
                        image = page.render(scale=scale).to_pil()
                    finally:
                        page.close()
                    jpeg, width, height = _jpeg(image, PROMO_LONG_SIDE)
                    frames.append((jpeg, width, height, f'{name} {index + 1}쪽'))
            finally:
                document.close()
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
    """내(강사) 과목인지 확인하고 출석 설정까지 합친 정보를 돌려준다."""
    row = conn.execute('''
        SELECT id FROM parent_classes WHERE id=? AND is_active=1 AND instructor_emp_no=?
    ''', (class_id, f"IH{account['id']}")).fetchone()
    return sa._load_class(conn, row['id']) if row else None


def _school_options(assignments):
    """학교별로 묶는다(같은 학교의 여러 강의부서는 한 학교로 본다)."""
    seen, result = set(), []
    for item in assignments:
        if item['school_name'] in seen:
            continue
        seen.add(item['school_name'])
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
        for item in assignments:
            rows = conn.execute('''
                SELECT id FROM parent_classes WHERE instructor_emp_no=? AND is_active=1
                  AND school_name=? AND COALESCE(department,'')=? ORDER BY class_name COLLATE NOCASE, id
            ''', (f"IH{account['id']}", item['school_name'], item['department'])).fetchall()
            payload = []
            for row in rows:
                info = sa._load_class(conn, row['id'])
                payload.append({
                    'id': info['id'], 'name': info['class_name'],
                    'schedule': sa._schedule_label(info),
                    'students': conn.execute('''
                        SELECT COUNT(*) c FROM parent_class_students cs
                        JOIN parent_students s ON s.id=cs.student_id AND s.is_active=1
                        WHERE cs.class_id=?''', (info['id'],)).fetchone()['c'],
                    'live_url': sa._live_url(info), 'display_url': sa._display_url(info),
                })
            classes.append({'assignment': item, 'classes': payload})
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


@instructor_hub_bp.route('/teacher-hub/<token>/classes', methods=['POST'])
def portal_save_class(token):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        _check_csrf()
        assignment = conn.execute('SELECT * FROM ih_assignments WHERE id=? AND account_id=?',
                                  (_int(request.form.get('assignment_id')), account['id'])).fetchone()
        class_name = _text(request.form.get('class_name'), 60)
        if not assignment or not class_name:
            flash('과목 이름을 입력해 주세요.', 'error')
            return _back(token, 'attendance')
        school_id = assignment['school_id'] or _school_id_by_name(conn, assignment['school_name'])
        if not school_id:
            flash('이 학교가 학교관리에 등록되어 있지 않아 과목을 만들 수 없습니다. 본사 담당자에게 문의해 주세요.', 'error')
            return _back(token, 'attendance')
        if conn.execute('''SELECT 1 FROM parent_classes WHERE school_id=? AND class_name=? AND is_active=1''',
                        (school_id, class_name)).fetchone():
            flash(f'이 학교에 같은 이름의 과목({class_name})이 이미 있습니다.', 'error')
            return _back(token, 'attendance')
        weekdays = ','.join(str(d) for d in sa._weekday_list(','.join(request.form.getlist('weekdays'))))
        class_id = conn.execute('''
            INSERT INTO parent_classes (school_id, school_name, department, class_name, instructor_emp_no,
                                        instructor_name, access_token)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (school_id, assignment['school_name'], assignment['department'], class_name,
              f"IH{account['id']}", account['name'], secrets.token_urlsafe(32))).lastrowid
        conn.commit()
        sa._ensure_settings(conn, class_id)
        conn.execute('''
            UPDATE student_att_settings SET weekdays=?, start_time=?, end_time=?, late_minutes=?,
                qr_mode=?, notify_parents=1, updated_at=? WHERE class_id=?
        ''', (weekdays, sa._valid_time(request.form.get('start_time')), sa._valid_time(request.form.get('end_time')),
              max(0, min(120, _int(request.form.get('late_minutes')) or 10)),
              'live' if request.form.get('qr_mode') == 'live' else 'daily', _now_text(), class_id))
        conn.commit()
        flash(f'‘{class_name}’ 과목을 만들었습니다. [학생 관리]에서 학생을 등록해 주세요.', 'success')
    finally:
        conn.close()
    return _back(token, 'attendance')


@instructor_hub_bp.route('/teacher-hub/<token>/classes/<int:class_id>/delete', methods=['POST'])
def portal_delete_class(token, class_id):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        _check_csrf()
        if _own_class(conn, account, class_id):
            conn.execute('UPDATE parent_classes SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?', (class_id,))
            conn.commit()
            flash('과목을 삭제했습니다. 지난 출석 기록은 보존됩니다.', 'success')
    finally:
        conn.close()
    return _back(token, 'attendance')


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
        base = _external_root()
        for student in students:
            for guardian in student['guardians']:
                path = guardian.pop('invite_path', '')
                guardian['invite_url'] = base + path if path else ''
        return _no_store(Response(render_template(
            'instructor_hub/portal_students.html', account=account, token=token, csrf=_csrf_token(),
            info=info, students=students, parent_url=sa._parent_url(info), schedule=sa._schedule_label(info),
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


@instructor_hub_bp.route('/teacher-hub/<token>/classes/<int:class_id>/parent-qr.svg')
def portal_parent_qr(token, class_id):
    conn = get_db()
    try:
        account, response = _portal_guard(conn, token)
        if response:
            return response
        info = _own_class(conn, account, class_id)
        if not info:
            abort(404)
    finally:
        conn.close()
    return sa._svg_response(sa._parent_url(info))


# ---------------------------------------------------------------- 본사 담당자 관리(인트라넷 메뉴)

def _admin_name():
    return str(session.get('user_name') or session.get('emp_no') or '')


def _accounts_view(conn):
    items = []
    for account in conn.execute('SELECT * FROM ih_accounts ORDER BY is_active DESC, name COLLATE NOCASE, id').fetchall():
        items.append({
            'id': account['id'], 'name': account['name'], 'phone': format_phone(account['phone']),
            'is_active': bool(account['is_active']), 'last_login_at': account['last_login_at'] or '',
            'sms_sent_at': account['sms_sent_at'] or '',
            'url': _portal_url(account), 'password': _decrypt(account['pw_enc']),
            'assignments': _assignments(conn, account['id']),
        })
    return items


def _pending_view(conn):
    profiles = []
    for row in conn.execute('''
        SELECT p.*, a.name FROM ih_profiles p JOIN ih_accounts a ON a.id=p.account_id
        WHERE p.status='pending' ORDER BY p.submitted_at
    ''').fetchall():
        profiles.append({
            'account_id': row['account_id'], 'name': row['name'], 'bio': row['draft_bio'],
            'old_bio': row['bio'], 'has_new_photo': bool(row['draft_photo_path']),
            'has_photo': bool(row['draft_photo_path'] or row['photo_path']),
            'submitted_at': row['submitted_at'] or '',
        })
    promos = []
    for row in conn.execute('''
        SELECT p.*, a.name FROM ih_promos p JOIN ih_accounts a ON a.id=p.account_id
        WHERE p.status='pending' ORDER BY p.submitted_at
    ''').fetchall():
        pages = _draft_pages(conn, row['id'])
        if not pages and row['ebook_id']:
            pages = conn.execute('SELECT id, page_no, image_filename AS name FROM ebook_pages WHERE ebook_id=? '
                                 'ORDER BY page_no', (row['ebook_id'],)).fetchall()
            from_ebook = True
        else:
            from_ebook = False
        promos.append({
            'id': row['id'], 'name': row['name'], 'school_name': row['school_name'],
            'submitted_at': row['submitted_at'] or '', 'ebook_id': row['ebook_id'],
            'pages': [{'id': p['id'], 'name': p['name']} for p in pages][:12], 'page_total': len(pages),
            'from_ebook': from_ebook,
        })
    return profiles, promos


def _link_pairs(conn):
    """링크페이지를 만들 수 있는 (학교, 강의부서) 후보."""
    pairs = {}
    for row in conn.execute('SELECT DISTINCT school_id, school_name, department FROM ih_assignments').fetchall():
        pairs[(row['school_name'], row['department'] or '')] = row['school_id']
    for row in conn.execute('''
        SELECT DISTINCT school_id, school_name, COALESCE(department,'') AS department
        FROM parent_classes WHERE is_active=1
    ''').fetchall():
        pairs.setdefault((row['school_name'], row['department']), row['school_id'])
    return sorted(((name, dept, sid) for (name, dept), sid in pairs.items()),
                  key=lambda item: (item[0], item[1]))


def _link_view(conn):
    items = []
    for row in conn.execute('SELECT * FROM ih_link_pages ORDER BY school_name, department').fetchall():
        item = dict(row)
        item['url'] = _external_root() + url_for('instructor_hub.public_link_page', token=row['token'])
        items.append(item)
    return items


@instructor_hub_bp.route('/instructor-hub/')
@instructor_hub_bp.route('/instructor-hub')
@menu_permission_required(MENU_KEY)
def admin_page():
    tab = request.args.get('tab') if request.args.get('tab') in ('accounts', 'review', 'links') else 'accounts'
    conn = get_db()
    try:
        profiles, promos = _pending_view(conn)
        return render_template(
            'instructor_hub/admin.html', tab=tab, accounts=_accounts_view(conn),
            pending_profiles=profiles, pending_promos=promos,
            link_pages=_link_view(conn), link_pairs=_link_pairs(conn),
            schools=sa._list_schools(conn),
        )
    finally:
        conn.close()


def _admin_back(tab):
    return redirect(url_for('instructor_hub.admin_page', tab=tab))


@instructor_hub_bp.route('/instructor-hub/accounts/sync', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_sync():
    conn = get_db()
    try:
        created, assigned = sync_from_completed_contracts(conn)
        conn.commit()
    finally:
        conn.close()
    flash(f'완료된 강사 전자계약을 확인했습니다. 새 계정 {created}개, 계약 {assigned}건을 반영했습니다.', 'success')
    return _admin_back('accounts')


@instructor_hub_bp.route('/instructor-hub/accounts/add', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_add_account():
    name, phone = _text(request.form.get('name'), 60), normalize_phone(request.form.get('phone'))
    school_name = _text(request.form.get('school_name'), 100)
    if not name or len(phone) < 10:
        flash('강사 이름과 휴대폰번호를 입력해 주세요.', 'error')
        return _admin_back('accounts')
    conn = get_db()
    try:
        ensure_account_for_signer(conn, name, phone, '', school_name,
                                  request.form.get('department'), None)
        conn.commit()
    finally:
        conn.close()
    flash(f'{name} 강사 계정을 만들었습니다.', 'success')
    return _admin_back('accounts')


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
        elif action == 'assign':
            added = add_assignment(conn, account_id, request.form.get('school_name'), request.form.get('department'))
            flash('학교·강의부서를 추가했습니다.' if added else '이미 있거나 입력이 비었습니다.', 'success' if added else 'error')
        elif action == 'sms':
            return _send_access_sms(conn, account)
        else:
            abort(404)
        conn.commit()
    finally:
        conn.close()
    return _admin_back('accounts')


def _send_access_sms(conn, account):
    """강사에게 접속 링크와 비밀번호를 문자로 보낸다(담당자가 버튼을 눌렀을 때만)."""
    from .solapi_settings import send_bulk_text
    text = (f"[새담] {account['name']} 강사님 강사통합지원 접속 안내\n"
            f"주소: {_portal_url(account)}\n비밀번호: {_decrypt(account['pw_enc'])}\n"
            "프로필·홍보페이지·학생출석을 이 주소에서 관리합니다.")
    try:
        result = send_bulk_text([{'to': account['phone'], 'text': text}])[0]
    except Exception as exc:  # 설정 누락 등
        flash(f'문자를 보내지 못했습니다: {exc}', 'error')
        return _admin_back('accounts')
    if result.get('ok'):
        conn.execute('UPDATE ih_accounts SET sms_sent_at=? WHERE id=?', (_now_text(), account['id']))
        conn.commit()
        flash(f'{account["name"]} 강사에게 접속 안내 문자를 보냈습니다.', 'success')
    else:
        flash(f'문자를 보내지 못했습니다: {result.get("error") or "알 수 없는 오류"}', 'error')
    return _admin_back('accounts')


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
    return _admin_back('review')


@instructor_hub_bp.route('/instructor-hub/review/promo/<int:promo_id>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_review_promo(promo_id):
    conn = get_db()
    try:
        approve = request.form.get('decision') == 'approve'
        error = review_promo(conn, promo_id, approve, _admin_name(), request.form.get('reason'))
    finally:
        conn.close()
    flash(error or ('홍보페이지를 승인했습니다. e리플렛 링크가 열렸습니다.' if approve else '홍보페이지를 반려했습니다.'),
          'error' if error else 'success')
    return _admin_back('review')


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


# -- 학부모 링크페이지 관리

@instructor_hub_bp.route('/instructor-hub/links/save', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_save_link():
    school_name = _text(request.form.get('school_name'), 100)
    department = _text(request.form.get('department'), 60)
    if not school_name:
        flash('학교를 선택해 주세요.', 'error')
        return _admin_back('links')
    conn = get_db()
    try:
        link_id = _int(request.form.get('id'))
        values = (
            _text(request.form.get('title'), 100), _multiline(request.form.get('notice'), 500),
            1 if request.form.get('show_profile') else 0, 1 if request.form.get('show_attendance') else 0,
            1 if request.form.get('show_leaflet') else 0, 1 if request.form.get('show_guide') else 0,
        )
        if link_id:
            conn.execute('''
                UPDATE ih_link_pages SET title=?, notice=?, show_profile=?, show_attendance=?,
                    show_leaflet=?, show_guide=? WHERE id=?
            ''', (*values, link_id))
            flash('링크페이지를 저장했습니다.', 'success')
        elif conn.execute('SELECT 1 FROM ih_link_pages WHERE school_name=? AND department=?',
                          (school_name, department)).fetchone():
            flash('이 학교·강의부서의 링크페이지가 이미 있습니다.', 'error')
        else:
            conn.execute('''
                INSERT INTO ih_link_pages (school_id, school_name, department, token, title, notice,
                    show_profile, show_attendance, show_leaflet, show_guide)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (_school_id_by_name(conn, school_name), school_name, department,
                  secrets.token_urlsafe(18), *values[:2], 1, 1, 1, 1))   # 새 링크는 네 영역을 모두 켠 채 시작
            flash('링크페이지를 만들었습니다.', 'success')
        conn.commit()
    finally:
        conn.close()
    return _admin_back('links')


@instructor_hub_bp.route('/instructor-hub/links/<int:link_id>/<action>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_link_action(link_id, action):
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM ih_link_pages WHERE id=?', (link_id,)).fetchone():
            abort(404)
        if action == 'regenerate':
            conn.execute('UPDATE ih_link_pages SET token=? WHERE id=?', (secrets.token_urlsafe(18), link_id))
            flash('링크를 새로 만들었습니다. 예전 링크는 더 이상 열리지 않습니다.', 'success')
        elif action == 'toggle':
            conn.execute('UPDATE ih_link_pages SET is_active=1-is_active WHERE id=?', (link_id,))
            flash('공개 상태를 바꿨습니다.', 'success')
        elif action == 'delete':
            conn.execute('DELETE FROM ih_link_pages WHERE id=?', (link_id,))
            flash('링크페이지를 삭제했습니다.', 'success')
        else:
            abort(404)
        conn.commit()
    finally:
        conn.close()
    return _admin_back('links')


@instructor_hub_bp.route('/instructor-hub/links/<int:link_id>/qr.svg')
@menu_permission_required(MENU_KEY)
def admin_link_qr(link_id):
    conn = get_db()
    try:
        row = conn.execute('SELECT token FROM ih_link_pages WHERE id=?', (link_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return sa._svg_response(_external_root() + url_for('instructor_hub.public_link_page', token=row['token']))


# ---------------------------------------------------------------- 학부모용 모바일 링크페이지(공개)

def _link_data(conn, link):
    department = link['department'] or ''
    people = conn.execute('''
        SELECT DISTINCT a.id, a.name FROM ih_accounts a
        JOIN ih_assignments s ON s.account_id=a.id
        WHERE a.is_active=1 AND s.school_name=? AND s.department=? ORDER BY a.name
    ''', (link['school_name'], department)).fetchall()
    profiles = []
    leaflets = []
    for person in people:
        profile = conn.execute("SELECT * FROM ih_profiles WHERE account_id=? AND status IN ('approved','pending','rejected')",
                               (person['id'],)).fetchone()
        # 한 번이라도 승인된 내용(photo_path·bio)만 보여 준다. 승인 전 초안은 절대 보이지 않는다.
        if profile and (profile['bio'] or profile['photo_path']):
            profiles.append({'account_id': person['id'], 'name': person['name'], 'bio': profile['bio'],
                             'has_photo': bool(profile['photo_path'])})
        promo = conn.execute('SELECT ebook_id FROM ih_promos WHERE account_id=? AND school_name=?',
                             (person['id'], link['school_name'])).fetchone()
        if promo and promo['ebook_id']:
            book = conn.execute("SELECT title, share_token FROM ebooks WHERE id=? AND approval_status='approved'",
                                (promo['ebook_id'],)).fetchone()
            if book:
                leaflets.append({'title': book['title'], 'name': person['name'],
                                 'url': url_for('ebook.public_reader', token=book['share_token'])})
    classes = []
    for row in conn.execute('''
        SELECT id FROM parent_classes WHERE school_name=? AND COALESCE(department,'')=? AND is_active=1
        ORDER BY class_name COLLATE NOCASE
    ''', (link['school_name'], department)).fetchall():
        info = sa._load_class(conn, row['id'])
        classes.append({
            'name': info['class_name'], 'instructor': info['instructor_name'] or '',
            'schedule': sa._schedule_label(info),
            'url': url_for('student_attendance.parent_lookup', parent_key=info['parent_key']),
        })
    guide_url = ''
    if link['school_id']:
        guide = conn.execute('SELECT share_token FROM classroom_guide_maps WHERE school_id=? AND share_enabled=1',
                             (link['school_id'],)).fetchone() \
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='classroom_guide_maps'").fetchone() else None
        if guide and guide['share_token']:
            guide_url = f"/school/guide/p/{guide['share_token']}"
    return profiles, classes, leaflets, guide_url


@instructor_hub_bp.route('/school-link/<token>')
def public_link_page(token):
    conn = get_db()
    try:
        link = conn.execute('SELECT * FROM ih_link_pages WHERE token=? AND is_active=1', (token,)).fetchone()
        if not link:
            return _no_store(Response(render_template('instructor_hub/link_page.html', invalid=True), 404))
        profiles, classes, leaflets, guide_url = _link_data(conn, link)
        return _no_store(Response(render_template(
            'instructor_hub/link_page.html', invalid=False, link=link, token=token,
            profiles=profiles, classes=classes, leaflets=leaflets, guide_url=guide_url)))
    finally:
        conn.close()


@instructor_hub_bp.route('/school-link/<token>/photo/<int:account_id>')
def public_link_photo(token, account_id):
    """링크페이지에 나오는 강사의 '승인된' 사진만 보여 준다."""
    conn = get_db()
    try:
        link = conn.execute('SELECT * FROM ih_link_pages WHERE token=? AND is_active=1', (token,)).fetchone()
        if not link:
            abort(404)
        allowed = conn.execute('''
            SELECT p.photo_path FROM ih_profiles p
            JOIN ih_assignments s ON s.account_id=p.account_id
            JOIN ih_accounts a ON a.id=p.account_id AND a.is_active=1
            WHERE p.account_id=? AND s.school_name=? AND s.department=?
        ''', (account_id, link['school_name'], link['department'] or '')).fetchone()
    finally:
        conn.close()
    if not allowed or not allowed['photo_path'] or not Path(allowed['photo_path']).is_file():
        abort(404)
    return encrypted_response(allowed['photo_path'], 'profile.jpg', as_attachment=False, mimetype='image/jpeg')
