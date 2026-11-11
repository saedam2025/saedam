"""학교지원 > 학부모·강사 통합지원 > 학생등록 — 강사용 학생등록 링크.

학생 등록은 본사와 강사가 모두 한다. 본사는 통합지원 화면의 [학생등록] 탭에서 직접 등록하고,
강사에게는 이 모듈이 만드는 **학교별 학생등록 링크**를 문자로 보낸다.

- 학교마다 링크(비밀 토큰) 하나와 무작위 6자리 숫자 비밀번호 하나. 강사는 링크를 열고 비밀번호를 넣으면
  그 학교의 강의반을 골라 학생을 한 명씩 또는 붙여넣기로 등록한다(출석 명단에만 더할 수 있고 지우거나 고치지는 못한다).
- 보호자 휴대폰번호 같은 개인정보는 화면에 다시 보여 주지 않는다(학생 이름·학년·반만 보인다).
- 비밀번호 재발급·링크 재발급·사용 중지는 본사가 한다(예전 링크와 비밀번호는 즉시 쓸 수 없게 된다).
- 비밀번호는 암호화해 저장하므로(`pw_enc`) 본사 화면에서 다시 볼 수 있고, 로그인 검사는 해시(`pw_hash`)로 한다.

학생 한 명을 명단에 넣는 규칙은 student_attendance의 `_save_student`·`register_students`를 그대로 쓴다.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime

from flask import (
    Blueprint, Response, flash, get_flashed_messages, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import instructor_hub as hub
from . import student_attendance as sa
from .database import get_db
from .instructor_attendance import _external_root, format_phone

student_register_bp = Blueprint('student_register', __name__)

SESSION_KEY = 'sr_links'                 # 비밀번호를 맞춘 링크의 id 목록
MAX_BULK_ROWS = 300
MAX_OPEN_LINKS = 20                      # 한 브라우저에서 동시에 열어 둘 수 있는 링크 수(세션 쿠키가 커지지 않게)


# ---------------------------------------------------------------- 스키마

def ensure_student_register_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sr_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            school_name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            pw_hash TEXT NOT NULL,
            pw_enc TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            last_used_at TEXT,
            last_sent_at TEXT,
            last_sent_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sr_links_school ON sr_links(school_name);
    ''')
    conn.commit()


def init_student_register_schema():
    conn = get_db()
    try:
        ensure_student_register_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 링크 관리(본사)

def _now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def link_url(link):
    return _external_root() + url_for('student_register.login', token=link['token'])


def link_for_school(conn, school_name):
    """학교 이름(공백 무시)으로 학생등록 링크를 찾는다."""
    key = hub.norm(school_name)
    return next((row for row in conn.execute('SELECT * FROM sr_links ORDER BY id').fetchall()
                 if hub.norm(row['school_name']) == key), None)


def create_link(conn, school_id, school_name):
    """학교의 학생등록 링크와 6자리 비밀번호를 만든다. 이미 있으면 그대로 돌려준다."""
    existing = link_for_school(conn, school_name)
    if existing:
        return existing
    password = hub._new_password()
    conn.execute('''
        INSERT INTO sr_links (school_id, school_name, token, pw_hash, pw_enc) VALUES (?, ?, ?, ?, ?)
    ''', (school_id, school_name, secrets.token_urlsafe(18), generate_password_hash(password), hub._encrypt(password)))
    conn.commit()
    return link_for_school(conn, school_name)


def reset_password(conn, link_id):
    password = hub._new_password()
    conn.execute('UPDATE sr_links SET pw_hash=?, pw_enc=? WHERE id=?',
                 (generate_password_hash(password), hub._encrypt(password), link_id))
    conn.commit()


def reissue_token(conn, link_id):
    conn.execute('UPDATE sr_links SET token=? WHERE id=?', (secrets.token_urlsafe(18), link_id))
    conn.commit()


def toggle_link(conn, link_id):
    conn.execute('UPDATE sr_links SET is_active=1-is_active WHERE id=?', (link_id,))
    conn.commit()


def password_of(link):
    return hub._decrypt(link['pw_enc'])


def sms_text(link, instructor_name=''):
    greeting = f'{instructor_name} 강사님, ' if instructor_name else ''
    return (f"[새담] {link['school_name']} 학생등록 안내\n"
            f"{greeting}맡으신 강의반의 학생을 아래 주소에서 등록해 주세요.\n"
            f"주소: {link_url(link)}\n비밀번호: {password_of(link)}")


def school_instructor_contacts(conn, school_name):
    """학생등록 링크를 문자로 보낼 수 있는 이 학교 강사(한 명이 한 줄). 휴대폰번호가 틀린 강사는 ok=False."""
    seen, result = set(), []
    for row in hub.school_assignment_rows(conn, school_name):
        if not row['account_active'] or row['account_id'] in seen:
            continue
        seen.add(row['account_id'])
        account = conn.execute('SELECT * FROM ih_accounts WHERE id=?', (row['account_id'],)).fetchone()
        phone = hub.clean_phone(account['phone'])
        result.append({
            'id': account['id'], 'name': account['name'], 'phone': phone,
            'phone_label': format_phone(phone) if hub.PHONE_PATTERN.fullmatch(phone) else (account['phone'] or ''),
            'ok': bool(hub.PHONE_PATTERN.fullmatch(phone)),
            'departments': sorted({r['department'] for r in hub.school_assignment_rows(conn, school_name)
                                   if r['account_id'] == account['id'] and r['department']}),
        })
    result.sort(key=lambda item: item['name'])
    return result


def send_link_sms(conn, link, account_ids):
    """고른 강사들에게 링크·비밀번호를 문자로 보낸다. 반환: (보낸 수, 실패 문장 목록). 문자 서비스 오류는 그대로 올린다."""
    from .solapi_settings import send_bulk_text
    wanted = set(account_ids)
    targets = [c for c in school_instructor_contacts(conn, link['school_name']) if c['id'] in wanted and c['ok']]
    if not targets:
        return 0, []
    results = send_bulk_text([{'to': c['phone'], 'text': sms_text(link, c['name'])} for c in targets])
    sent, failed = 0, []
    for contact, result in zip(targets, results):
        if result.get('ok'):
            sent += 1
        else:
            failed.append(f"{contact['name']}({result.get('error') or '알 수 없는 오류'})")
    if sent:
        conn.execute('UPDATE sr_links SET last_sent_at=?, last_sent_count=? WHERE id=?', (_now_text(), sent, link['id']))
        conn.commit()
    return sent, failed


# ---------------------------------------------------------------- 학교의 강의반

def school_classes(conn, school_name, school_id=None):
    """이 학교의 사용 중인 강의반(출석 설정·인원 포함). 학교 이름은 공백을 무시하고 맞춘다."""
    key = hub.norm(school_name)
    result = []
    for row in conn.execute('SELECT id, school_id, school_name FROM parent_classes WHERE is_active=1 '
                            'ORDER BY class_name COLLATE NOCASE, id').fetchall():
        if hub.norm(row['school_name']) == key or (school_id and row['school_id'] == school_id):
            result.append(sa._class_payload(conn, row))
    return result


# ---------------------------------------------------------------- 공개 화면(링크 + 비밀번호)

def _no_store(response):
    return hub._no_store(response)


def _link_by_token(conn, token):
    return conn.execute('SELECT * FROM sr_links WHERE token=? AND is_active=1', (str(token or '')[:80],)).fetchone()


def _opened(link):
    return link['id'] in (session.get(SESSION_KEY) or [])


def _remember(link):
    opened = [i for i in (session.get(SESSION_KEY) or []) if i != link['id']] + [link['id']]
    session[SESSION_KEY] = opened[-MAX_OPEN_LINKS:]


def _invalid_page():
    return _no_store(Response(render_template('student_register/login.html', invalid=True), 404))


def _guard(conn, token):
    """링크와 비밀번호 확인을 모두 거친다. 통과하면 (링크, None), 아니면 (None, 응답)."""
    link = _link_by_token(conn, token)
    if not link:
        return None, _invalid_page()
    if not _opened(link):
        return None, redirect(url_for('student_register.login', token=token))
    return link, None


def _class_of_link(conn, link, class_id):
    """링크의 학교에 속한 강의반인지 확인하고 출석 설정까지 합친 정보를 돌려준다(아니면 None)."""
    row = conn.execute('SELECT id, school_id, school_name FROM parent_classes WHERE id=? AND is_active=1',
                       (class_id,)).fetchone() if class_id else None
    if not row:
        return None
    if hub.norm(row['school_name']) != hub.norm(link['school_name']) \
            and not (link['school_id'] and row['school_id'] == link['school_id']):
        return None
    return sa._load_class(conn, row['id'])


@student_register_bp.route('/student-register/<token>', methods=['GET', 'POST'])
def login(token):
    conn = get_db()
    try:
        link = _link_by_token(conn, token)
        if not link:
            return _invalid_page()
        if _opened(link):
            return redirect(url_for('student_register.home', token=token))
        error = ''
        if request.method == 'POST':
            key = 'sr:' + hub._failure_key(token)
            if hub._locked(key):
                error = '비밀번호가 여러 번 맞지 않아 잠시 막혔습니다. 10분 뒤 다시 시도해 주세요.'
            elif check_password_hash(link['pw_hash'], request.form.get('password', '').strip()):
                _remember(link)
                conn.execute('UPDATE sr_links SET last_used_at=? WHERE id=?', (_now_text(), link['id']))
                conn.commit()
                return redirect(url_for('student_register.home', token=token))
            else:
                hub._login_failures.setdefault(key, []).append(time.time())
                error = '비밀번호가 맞지 않습니다. 문자로 받은 6자리 숫자를 다시 확인해 주세요.'
        return _no_store(Response(render_template(
            'student_register/login.html', invalid=False, link=link, error=error)))
    finally:
        conn.close()


@student_register_bp.route('/student-register/<token>/logout', methods=['POST'])
def logout(token):
    conn = get_db()
    try:
        link = _link_by_token(conn, token)
    finally:
        conn.close()
    if link:
        session[SESSION_KEY] = [i for i in (session.get(SESSION_KEY) or []) if i != link['id']]
    return redirect(url_for('student_register.login', token=token))


@student_register_bp.route('/student-register/<token>/home')
def home(token):
    conn = get_db()
    try:
        link, response = _guard(conn, token)
        if response:
            return response
        classes = school_classes(conn, link['school_name'], link['school_id'])
        wanted = hub._int(request.args.get('class'))
        current = next((c for c in classes if c['id'] == wanted), classes[0] if classes else None)
        students = []
        if current:
            students = [{'name': s['name'], 'grade': s['grade'], 'classroom': s['classroom']}
                        for s in sa._roster(conn, current['id'])]
        return _no_store(Response(render_template(
            'student_register/home.html', link=link, token=token, csrf=hub._csrf_token(),
            classes=classes, current=current, students=students,
            messages=get_flashed_messages(with_categories=True))))
    finally:
        conn.close()


@student_register_bp.route('/student-register/<token>/students', methods=['POST'])
def save_students(token):
    conn = get_db()
    class_id = hub._int(request.form.get('class_id'))
    try:
        link, response = _guard(conn, token)
        if response:
            return response
        hub._check_csrf()
        info = _class_of_link(conn, link, class_id)
        if not info:
            flash('이 학교의 강의반이 아닙니다. 강의반을 다시 골라 주세요.', 'error')
            return redirect(url_for('student_register.home', token=token))
        if request.form.get('action') == 'bulk':
            rows = sa.parse_roster_text(request.form.get('text'))
            if not rows:
                flash('등록할 학생 명단을 입력해 주세요.', 'error')
            elif len(rows) > MAX_BULK_ROWS:
                flash(f'한 번에 {MAX_BULK_ROWS}명까지 등록할 수 있습니다. 나누어 올려 주세요.', 'error')
            else:
                errors = sa.register_students(conn, info, rows)
                if errors:
                    conn.rollback()
                    flash('명단을 확인해 주세요. 아무도 등록하지 않았습니다.\n' + '\n'.join(errors[:8]), 'error')
                else:
                    conn.commit()
                    flash(f'학생 {len(rows)}명을 {info["class_name"]}에 등록했습니다.', 'success')
        else:
            error = sa._save_student(conn, info, {
                'name': request.form.get('name'), 'grade': request.form.get('grade'),
                'classroom': request.form.get('classroom'), 'guardian_name': request.form.get('guardian_name'),
                'guardian_phone': request.form.get('guardian_phone'),
            })
            if error:
                conn.rollback()
                flash(error, 'error')
            else:
                conn.commit()
                flash(f'학생을 {info["class_name"]}에 등록했습니다.', 'success')
    finally:
        conn.close()
    return redirect(url_for('student_register.home', token=token, **{'class': class_id}))
