"""학교지원 > 학부모통합지원.

학부모가 휴대폰(기본)이나 PC에서 같은 주소 하나로 아래를 한 곳에서 본다.

1. 출석알림·출석현황 : 자녀가 수업에 도착하면 푸시알림을 받고, 오늘 출석과 월별 출석표·출석률을 본다.
2. 강사 프로필       : 자녀가 등록된 강의부서의 강사 사진·소개글(본사 승인본)을 본다.
3. 학교 e리플렛      : 본사가 올린 표지·안내 + 강사들이 올린 홍보(승인본)가 한 권으로 합쳐져 보인다.
4. 3D 교실안내       : [교실안내]에서 공유를 켠 학교의 3D 안내도. 자녀 강좌가 있는 교실로 바로 안내한다.

흐름
- 본사 담당자는 [학부모통합지원] 메뉴에서 학교마다 포털(링크·QR)을 만들고, 학교 e리플렛의 앞·뒤쪽을 채운다.
- 학부모는 가정통신문의 학교 링크로 들어와 '자녀 이름 + 보호자 휴대폰번호'를 한 번 확인하면
  보호자 개인 주소(/parent/home/<개인토큰>)로 이동한다. 개인 주소는 [학부모알림전송]이 이미 쓰는
  보호자 초대 토큰과 같아서, 출결 푸시 구독·홈 화면 아이콘·알림 내역이 모두 한 사람 기준으로 이어진다.
- 출석·알림은 보호자 본인의 자녀 것만 보여 주고, 강사 프로필·리플렛·교실안내는 학교 포털 설정(보일 항목)을 따른다.

학부모의 입구는 이 포털 하나다. 과목별 QR·예전 알림 등록 주소(/parent/register, /student-attendance/p)는
이 포털(또는 보호자 개인 화면)로 이어 주기만 하고 새로 만들지 않는다. 학부모에게 보내는 링크는 학교 포털에서
문자로 한꺼번에 발송한다(개인 링크 또는 학교 공통 링크).

학교 포털이 없는 학교의 보호자도 개인 주소로 출석현황·알림 내역은 볼 수 있다(기존 알림 등록 보호자 호환).
화면(본사용)은 routes/support_hub.py의 [학부모·강사 통합지원]에서 그린다.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime
from calendar import monthrange
from datetime import date
from pathlib import Path

from flask import (
    Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for,
)

from . import instructor_hub as hub
from . import parent_notifications as pn
from . import solapi_settings as solapi
from . import student_attendance as sa
from .classroom_guide import _load_layout
from .database import get_db
from .instructor_attendance import _external_root, normalize_phone
from .secure_files import delete_file, encrypted_response
from .security import has_menu_permission, menu_permission_required
from .storage import DATA_ROOT

parent_hub_bp = Blueprint('parent_hub', __name__)
logger = logging.getLogger(__name__)

MENU_KEY = 'school_support_hub'      # 강사통합지원 + 학부모통합지원 = [학부모·강사 통합지원] 한 메뉴
HUB_ROOT = Path(DATA_ROOT) / 'parent_hub'
LEAFLET_ROOT = HUB_ROOT / 'leaflet'

COOKIE_NAME = 'saedam_parent_hub'
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
MAX_HQ_PAGES = 40                     # 본사가 한 구역(앞쪽·뒤쪽)에 둘 수 있는 쪽 수
LEAFLET_SECTIONS = {'front': '앞쪽 (표지·학교 소개)', 'back': '뒤쪽 (마무리·안내)'}
TABS = ('home', 'attendance', 'teachers', 'leaflet', 'guide', 'notices')
# 학부모 화면 디자인. 학교(포털)마다 하나를 고르고, 'classic'이 기본이다.
# 새 디자인을 더하려면 여기에 한 줄 + static/css/ 아래 덮어쓰기 CSS(기본 CSS 다음에 불러온다)를 만든다.
DEFAULT_DESIGN = 'classic'
DESIGNS = {
    'classic': {'label': '기본 디자인', 'tagline': '따뜻한 오렌지 · 부드러운 카드', 'css': '', 'theme_color': '#e78100',
                'points': ('익숙한 오렌지 색상', '부드럽고 편안한 느낌')},
    'neo': {'label': '네오 디자인', 'tagline': '선명한 남색 · 미래형 글래스 카드', 'css': 'css/parent_hub_neo.css',
            'theme_color': '#0f172a',
            'points': ('글자·상태 색이 더 또렷해요', '남색 + 청록 포인트, 둥근 유리 카드')},
}
WEEKDAY_FIRST_SUNDAY = ('일', '월', '화', '수', '목', '금', '토')


# ---------------------------------------------------------------- 스키마

def ensure_parent_hub_schema(conn):
    conn.executescript('''
        -- 학교마다 하나. 학부모에게 나눠 주는 링크(token)와 보여 줄 항목.
        CREATE TABLE IF NOT EXISTS ph_portals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            school_name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            notice TEXT NOT NULL DEFAULT '',
            show_attendance INTEGER NOT NULL DEFAULT 1,
            show_profile INTEGER NOT NULL DEFAULT 1,
            show_leaflet INTEGER NOT NULL DEFAULT 1,
            show_guide INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (school_name)
        );

        -- 본사가 올린 학교 e리플렛 쪽. 앞쪽(front)은 강사 홍보 앞에, 뒤쪽(back)은 뒤에 붙는다.
        CREATE TABLE IF NOT EXISTS ph_leaflet_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portal_id INTEGER NOT NULL,
            section TEXT NOT NULL DEFAULT 'front',
            position INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ph_leaflet_pages
            ON ph_leaflet_pages(portal_id, section, position);
    ''')
    # 학부모에게 마지막으로 링크 문자를 보낸 때(운영 중인 DB에도 열을 더한다)
    pn._add_missing_columns(conn, 'ph_portals', {'last_sent_at': 'TEXT', 'last_sent_count': 'INTEGER NOT NULL DEFAULT 0',
                                                 'last_sent_method': "TEXT NOT NULL DEFAULT ''",
                                                 'design': f"TEXT NOT NULL DEFAULT '{DEFAULT_DESIGN}'"})
    conn.commit()


def init_parent_hub_schema():
    LEAFLET_ROOT.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        ensure_parent_hub_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 공통 도우미

_text = hub._text
_int = hub._int
_multiline = hub._multiline
_norm = hub.norm


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


def _admin_name():
    return str(session.get('user_name') or session.get('emp_no') or '')


def _portal_url(portal, *, external=True):
    path = url_for('parent_hub.hub_landing', token=portal['token'])
    return (_external_root() + path) if external else path


def _portal_by_token(conn, token, *, staff_preview=False):
    row = conn.execute('SELECT * FROM ph_portals WHERE token=?', (str(token or '')[:80],)).fetchone()
    if not row:
        return None
    if row['is_active'] or (staff_preview and session.get('emp_no') and has_menu_permission(MENU_KEY)):
        return row
    return None


def portal_for_school(conn, school_name, *, active_only=True):
    """학교 이름(공백 무시)으로 포털을 찾는다."""
    key = _norm(school_name)
    for row in conn.execute('SELECT * FROM ph_portals').fetchall():
        if _norm(row['school_name']) == key and (row['is_active'] or not active_only):
            return row
    return None


def design_of(portal):
    """포털에서 고른 학부모 화면 디자인 키. 없거나 모르는 값이면 기본 디자인."""
    try:
        value = portal['design']
    except (KeyError, IndexError, TypeError):
        value = ''
    return value if value in DESIGNS else DEFAULT_DESIGN


def _design_context(key):
    info = DESIGNS.get(key) or DESIGNS[DEFAULT_DESIGN]
    return {'design': key if key in DESIGNS else DEFAULT_DESIGN, 'design_css': info['css'],
            'theme_color': info['theme_color']}


def _portals_by_school(conn):
    return {_norm(row['school_name']): row for row in conn.execute('SELECT * FROM ph_portals').fetchall()}


def _show(portal):
    """포털 설정에서 보일 항목. 포털이 없는 학교는 출석·알림만 보인다(기존 알림 등록 보호자 호환)."""
    if not portal:
        return {'attendance': True, 'profile': False, 'leaflet': False, 'guide': False}
    return {
        'attendance': bool(portal['show_attendance']),
        'profile': bool(portal['show_profile']),
        'leaflet': bool(portal['show_leaflet']),
        'guide': bool(portal['show_guide']),
    }


# ---------------------------------------------------------------- 학교 e리플렛(본사 쪽 + 강사 홍보)

def _hq_pages(conn, portal_id, section=None):
    sql = 'SELECT * FROM ph_leaflet_pages WHERE portal_id=?'
    params = [portal_id]
    if section:
        sql += ' AND section=?'
        params.append(section)
    return conn.execute(sql + ' ORDER BY section, position, id', params).fetchall()


def _live_instructor_promos(conn, school_name):
    """이 학교에서 승인되어 공개 중인 강사 홍보 리플렛. 반환: [{'account_id','name','department','pages'}]"""
    key = _norm(school_name)
    assignments = {}
    for row in hub.school_assignment_rows(conn, school_name):
        if row['account_active']:
            assignments.setdefault(row['account_id'], {'name': row['account_name'], 'departments': []})
            department = (row['department'] or '').strip()
            if department and department not in assignments[row['account_id']]['departments']:
                assignments[row['account_id']]['departments'].append(department)
    result = []
    for promo in conn.execute('SELECT * FROM ih_promos WHERE ebook_id IS NOT NULL').fetchall():
        if _norm(promo['school_name']) != key or promo['account_id'] not in assignments:
            continue
        book = conn.execute("SELECT id, title FROM ebooks WHERE id=? AND approval_status='approved'",
                            (promo['ebook_id'],)).fetchone()
        if not book:
            continue
        pages = [row for row in conn.execute(
            'SELECT id, page_no, image_path, image_filename, width, height FROM ebook_pages '
            'WHERE ebook_id=? ORDER BY page_no', (book['id'],)).fetchall()
            if row['image_path'] and Path(row['image_path']).is_file()]
        if not pages:
            continue
        info = assignments[promo['account_id']]
        result.append({'account_id': promo['account_id'], 'name': info['name'],
                       'department': ' · '.join(info['departments']), 'pages': pages})
    result.sort(key=lambda item: (item['department'] == '', item['department'], item['name']))
    return result


def leaflet_pages(conn, portal):
    """학교 e리플렛의 쪽 순서: 본사 앞쪽 → 강사 홍보(부서·이름 순) → 본사 뒤쪽.

    반환: (쪽 목록, 목차). 쪽은 {'no','kind'('hq'|'eb'),'id','width','height','name'}.
    """
    pages, toc = [], []

    def add(title, kind, rows):
        rows = [row for row in rows if row['path' if kind == 'hq' else 'image_path']
                and Path(row['path' if kind == 'hq' else 'image_path']).is_file()]
        if not rows:
            return
        toc.append({'title': title, 'first': len(pages) + 1, 'count': len(rows)})
        for row in rows:
            pages.append({'no': len(pages) + 1, 'kind': kind, 'id': row['id'],
                          'width': row['width'] or 0, 'height': row['height'] or 0,
                          'name': row['name'] if kind == 'hq' else (row['image_filename'] or '')})

    add('표지·학교 소개', 'hq', _hq_pages(conn, portal['id'], 'front'))
    for promo in _live_instructor_promos(conn, portal['school_name']):
        label = f"{promo['department']} · {promo['name']} 강사" if promo['department'] else f"{promo['name']} 강사"
        add(label, 'eb', promo['pages'])
    add('마무리·안내', 'hq', _hq_pages(conn, portal['id'], 'back'))
    return pages, toc


def _leaflet_page_url(token, page):
    endpoint = 'parent_hub.leaflet_hq_image' if page['kind'] == 'hq' else 'parent_hub.leaflet_eb_image'
    return url_for(endpoint, token=token, page_id=page['id'])


def _instructor_coverage(conn, school_name):
    """본사 화면용: 이 학교 강사별 홍보 리플렛 상태. 반환: [{'name','departments','status','live_pages'}]"""
    key = _norm(school_name)
    live = {item['account_id']: len(item['pages']) for item in _live_instructor_promos(conn, school_name)}
    promos = {row['account_id']: row['status'] for row in conn.execute('SELECT account_id, school_name, status '
                                                                       'FROM ih_promos').fetchall()
              if _norm(row['school_name']) == key}
    by_account = {}
    for row in hub.school_assignment_rows(conn, school_name):
        if not row['account_active']:
            continue
        item = by_account.setdefault(row['account_id'], {
            'account_id': row['account_id'], 'name': row['account_name'], 'departments': []})
        if row['department'] and row['department'] not in item['departments']:
            item['departments'].append(row['department'])
    result = []
    for account_id, item in by_account.items():
        item['status'] = promos.get(account_id, 'none')
        item['live_pages'] = live.get(account_id, 0)
        result.append(item)
    result.sort(key=lambda it: (not it['departments'], ' '.join(it['departments']), it['name']))
    return result


# ---------------------------------------------------------------- 3D 교실안내 연결

def _guide_for_school(conn, portal):
    """공유가 켜진 3D 교실안내가 있으면 {'path', 'layout'}, 없으면 None."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='classroom_guide_maps'").fetchone():
        return None
    school_id = portal['school_id'] or hub.canonical_school(conn, portal['school_name'])[0]
    if not school_id:
        return None
    row = conn.execute('SELECT share_token, layout_json FROM classroom_guide_maps '
                       'WHERE school_id=? AND share_enabled=1', (school_id,)).fetchone()
    layout = _load_layout(row) if row and row['share_token'] else None
    if not layout:
        return None
    return {'path': url_for('classroom_guide.public_view', token=row['share_token']), 'layout': layout}


def _room_for_class(layout, class_name, instructor_name='', department=''):
    """강좌가 배치된 교실을 찾는다(강좌명이 같거나 서로 포함). 반환: {'id','label'} 또는 None.

    과목명(강의부서)과 강의반 이름 둘 다로 찾는다. 배치도에는 '로봇과학'처럼 과목명으로,
    과목에는 '로봇과학A'처럼 강의반으로 적혀 있어도 같은 교실로 이어진다. 강의반 이름이 더 정확하므로 점수를 더 준다.
    """
    wanted = [(_norm(class_name), 1), (_norm(department), 0)]
    teacher = _norm(instructor_name)
    best, best_score = None, 0
    for building in layout.get('buildings') or []:
        for room in building.get('rooms') or []:
            for course in room.get('courses') or []:
                name = _norm(course.get('name'))
                if len(name) < 2:
                    continue
                for want, bonus in wanted:
                    if not want:
                        continue
                    if name == want:
                        score = 3
                    elif name in want or want in name:
                        score = 2
                    else:
                        continue
                    score += bonus * 0.5
                    if teacher and _norm(course.get('teacher')) == teacher:
                        score += 1
                    if score > best_score:
                        label = ' '.join(part for part in (
                            building.get('name') or '',
                            f"{room.get('floor')}층" if room.get('floor') else '',
                            room.get('name') or room.get('no') or '') if part)
                        best, best_score = {'id': room['id'], 'label': label}, score
    return best


# ---------------------------------------------------------------- 보호자 확인(자녀 이름 + 보호자 휴대폰번호)

def _student_in_school(conn, student, portal):
    if _norm(student['school_name']) == _norm(portal['school_name']):
        return True
    if portal['school_id'] and student['school_id'] == portal['school_id']:
        return True
    for row in conn.execute('''
        SELECT c.school_name FROM parent_class_students cs
        JOIN parent_classes c ON c.id=cs.class_id AND c.is_active=1 WHERE cs.student_id=?
    ''', (student['id'],)).fetchall():
        if _norm(row['school_name']) == _norm(portal['school_name']):
            return True
    return False


def _find_guardian(conn, portal, child_name, phone):
    """이 학교 학생의 이름과 보호자 번호가 명단과 같을 때만 보호자 id를 돌려준다."""
    rows = conn.execute('''
        SELECT DISTINCT g.id AS guardian_id, s.id, s.school_id, s.school_name
        FROM parent_students s
        JOIN parent_guardian_students gs ON gs.student_id=s.id
        JOIN parent_guardians g ON g.id=gs.guardian_id AND g.is_active=1
        WHERE s.is_active=1 AND REPLACE(s.name, ' ', '')=? AND g.phone=?
    ''', (child_name, phone)).fetchall()
    for row in rows:
        if _student_in_school(conn, row, portal):
            return row['guardian_id']
    return None


def _guardian_has_child_at(conn, guardian_id, portal):
    for row in conn.execute('''
        SELECT s.id, s.school_id, s.school_name FROM parent_guardian_students gs
        JOIN parent_students s ON s.id=gs.student_id AND s.is_active=1 WHERE gs.guardian_id=?
    ''', (guardian_id,)).fetchall():
        if _student_in_school(conn, row, portal):
            return True
    return False


def _remember_guardian(response, invite_token):
    response.set_cookie(
        COOKIE_NAME, invite_token, max_age=COOKIE_MAX_AGE, httponly=True, samesite='Lax', path='/parent/',
        secure=request.is_secure or request.headers.get('X-Forwarded-Proto', '').startswith('https'))
    return response


# ---------------------------------------------------------------- 자녀·출석 자료

def _children(conn, guardian_id):
    """보호자의 자녀와 수강 과목(출석 설정 포함)."""
    children = []
    for student in conn.execute('''
        SELECT s.* FROM parent_guardian_students gs
        JOIN parent_students s ON s.id=gs.student_id AND s.is_active=1
        WHERE gs.guardian_id=? ORDER BY s.name COLLATE NOCASE, s.id
    ''', (guardian_id,)).fetchall():
        classes = []
        for row in conn.execute('''
            SELECT c.id FROM parent_class_students cs
            JOIN parent_classes c ON c.id=cs.class_id AND c.is_active=1
            WHERE cs.student_id=? ORDER BY c.school_name, c.department, c.class_name COLLATE NOCASE, c.id
        ''', (student['id'],)).fetchall():
            info = sa._load_class(conn, row['id'])
            if info:
                info['schedule'] = sa._schedule_label(info)
                classes.append(info)
        school_name = student['school_name'] or (classes[0]['school_name'] if classes else '')
        children.append({
            'id': student['id'], 'name': student['name'], 'grade': student['grade'] or '',
            'classroom': student['classroom'] or '', 'school_name': school_name, 'classes': classes,
        })
    return children


def _month_key(month_text, today):
    match = re.fullmatch(r'(\d{4})-(\d{2})', str(month_text or '').strip())
    year, month = (int(match.group(1)), int(match.group(2))) if match and 1 <= int(match.group(2)) <= 12 \
        else (today.year, today.month)
    if (year, month) > (today.year, today.month):
        year, month = today.year, today.month
    return year, month


def _rate(attended, held):
    return round(attended * 100 / held) if held else None


def attendance_report(conn, class_info, student_id, month_text=None):
    """자녀 한 명의 한 과목 출석현황: 오늘·누적·월별 출석표·출석률·최근 기록.

    수업 횟수는 '그 과목에서 한 명이라도 출석이 기록된 날' 중 자녀의 첫 기록일 이후만 센다
    (중간에 등록한 학생이 이전 수업을 결석으로 떠안지 않도록). 지각은 출석으로 계산한다.
    """
    today = sa._now().date()
    class_id = class_info['id']
    records = {row['att_date']: row for row in conn.execute(
        'SELECT att_date, status, check_in_at, dismissed_at FROM student_att_records '
        'WHERE class_id=? AND student_id=?', (class_id, student_id)).fetchall()}
    first = min(records) if records else None
    held = [row[0] for row in conn.execute(
        'SELECT DISTINCT att_date FROM student_att_records WHERE class_id=? ORDER BY att_date', (class_id,)
    ).fetchall() if first and row[0] >= first]

    def count(dates):
        present = sum(1 for d in dates if records.get(d) and records[d]['status'] == '출석')
        late = sum(1 for d in dates if records.get(d) and records[d]['status'] == '지각')
        return {'present': present, 'late': late, 'absent': len(dates) - present - late,
                'held': len(dates), 'rate': _rate(present + late, len(dates))}

    year, month = _month_key(month_text, today)
    month_prefix = f'{year:04d}-{month:02d}'
    class_days = set(sa._weekday_list(class_info['weekdays']))
    cells = [None] * ((date(year, month, 1).weekday() + 1) % 7)       # 일요일 시작 달력
    for day in range(1, monthrange(year, month)[1] + 1):
        current = date(year, month, day)
        iso = current.isoformat()
        record = records.get(iso)
        cells.append({
            'day': day, 'status': record['status'] if record else '',
            'time': (record['check_in_at'] or '')[11:16] if record else '',
            'class_day': current.weekday() in class_days, 'future': current > today,
            'today': current == today,
        })
    month_dates = [d for d in held if d.startswith(month_prefix)]
    today_record = records.get(today.isoformat())
    recent = []
    for iso in sorted(records, reverse=True)[:8]:
        record = records[iso]
        recent.append({
            'date': iso, 'weekday': sa.WEEKDAY_LABELS[date.fromisoformat(iso).weekday()],
            'status': record['status'], 'time': (record['check_in_at'] or '')[11:16],
            'dismissed': (record['dismissed_at'] or '')[11:16],
        })
    previous_month = ''
    if first and first[:7] < month_prefix:
        previous_month = f'{year - 1:04d}-12' if month == 1 else f'{year:04d}-{month - 1:02d}'
    next_month = ''
    if (year, month) < (today.year, today.month):
        next_month = f'{year + 1:04d}-01' if month == 12 else f'{year:04d}-{month + 1:02d}'
    return {
        'class_id': class_id,
        'today': {
            'status': today_record['status'] if today_record else '',
            'time': (today_record['check_in_at'] or '')[11:16] if today_record else '',
            'dismissed': (today_record['dismissed_at'] or '')[11:16] if today_record else '',
            'class_day': today.weekday() in class_days,
        },
        'total': count(held),
        'month': {'key': month_prefix, 'label': f'{year}년 {month}월', 'cells': cells,
                  'previous': previous_month, 'next': next_month, **count(month_dates)},
        'recent': recent,
        'has_records': bool(records),
    }


def _instructors_for_class(conn, class_info):
    """과목에 연결된 강사와 '승인된' 프로필. 계정이 없으면 과목에 적힌 강사 이름만 보여 준다."""
    people = []
    for account in hub.linked_accounts(conn, class_info):
        profile = conn.execute('SELECT bio, photo_path FROM ih_profiles WHERE account_id=?',
                               (account['id'],)).fetchone()
        approved = bool(profile and (profile['bio'] or profile['photo_path']))
        people.append({
            'account_id': account['id'], 'name': account['name'],
            'bio': profile['bio'] if approved else '', 'has_photo': bool(approved and profile['photo_path']),
            'prepared': approved,
        })
    if not people and class_info.get('instructor_name'):
        people.append({'account_id': None, 'name': class_info['instructor_name'], 'bio': '',
                       'has_photo': False, 'prepared': False})
    return people


def _photo_account_ids(conn, guardian_id):
    ids = set()
    for child in _children(conn, guardian_id):
        for info in child['classes']:
            ids.update(item['account_id'] for item in _instructors_for_class(conn, info) if item['account_id'])
    return ids


def _push_state(conn, guardian_id):
    count = conn.execute('SELECT COUNT(*) FROM parent_push_subscriptions WHERE guardian_id=? AND is_active=1',
                         (guardian_id,)).fetchone()[0]
    health = pn.receipt_health(conn, [guardian_id]).get(guardian_id, {})
    return {'registered': count > 0, 'count': count, 'last_received_at': health.get('last_received_at', ''),
            'warn': bool(health.get('warn'))}


def _notices(conn, guardian_id, limit=30):
    return [pn._with_kst(row) for row in conn.execute('''
        SELECT n.kind, n.title, n.body, n.created_at, r.status, r.received_at
        FROM parent_notification_recipients r JOIN parent_notifications n ON n.id=r.notification_id
        WHERE r.guardian_id=? ORDER BY r.id DESC LIMIT ?
    ''', (guardian_id, limit)).fetchall()]


def home_context(conn, invite, token, tab, child_id, month_text):
    """보호자 개인 화면에 필요한 자료를 모은다(탭마다 필요한 것만 무겁게 계산한다)."""
    children = _children(conn, invite['guardian_id'])
    portals = _portals_by_school(conn)

    schools, seen = [], set()
    for child in children:
        names = [child['school_name']] + [info['school_name'] for info in child['classes']]
        child['schools'] = []
        for name in names:
            key = _norm(name)
            if key and key not in {_norm(n) for n in child['schools']}:
                child['schools'].append(name)
            if key and key not in seen:
                seen.add(key)
                schools.append({'name': name, 'portal': portals.get(key)})
        primary = portals.get(_norm(child['school_name']))
        child['portal'] = primary if primary and primary['is_active'] else None
        child['show'] = _show(child['portal'])
    active = [item for item in schools if item['portal'] and item['portal']['is_active']]
    show = {key: any(_show(item['portal'])[key] for item in active) for key in ('profile', 'leaflet', 'guide')}
    show['attendance'] = any(child['show']['attendance'] for child in children) or not active
    tabs = [('home', '홈', 'fa-house')]
    if show['attendance']:
        tabs.append(('attendance', '출석', 'fa-calendar-check'))
    if show['profile']:
        tabs.append(('teachers', '강사', 'fa-chalkboard-user'))
    if show['leaflet']:
        tabs.append(('leaflet', '리플렛', 'fa-book-open-reader'))
    if show['guide']:
        tabs.append(('guide', '교실안내', 'fa-map-location-dot'))
    tabs.append(('notices', '알림', 'fa-bell'))
    if tab not in {key for key, _label, _icon in tabs}:
        tab = 'home'

    selected = next((c for c in children if c['id'] == child_id), children[0] if children else None)
    # 디자인은 고른 자녀의 학교 포털 설정을 따르고, 포털이 없으면 공개 중인 첫 포털을 따른다.
    design_portal = (selected['portal'] if selected and selected['portal'] else None)         or (active[0]['portal'] if active else None)
    context = {
        **_design_context(design_of(design_portal)), 'demo': False,
        'token': token, 'guardian_name': invite['guardian_name'], 'tab': tab, 'tabs': tabs,
        'children': children, 'selected': selected, 'schools': schools, 'show': show,
        'push': _push_state(conn, invite['guardian_id']), 'push_configured': pn._push_ready(),
        'notices': [], 'reports': {}, 'teachers': [], 'leaflets': [], 'guides': [],
        'month': month_text or '', 'weekday_labels': WEEKDAY_FIRST_SUNDAY,
        'portal_notices': [item['portal'] for item in active if item['portal']['notice']],
        'more_tabs': [item for item in tabs if item[0] in ('teachers', 'leaflet', 'guide')],
    }
    # 홈에서는 모든 과목의 오늘 상태·출석률을, 출석 탭에서는 고른 자녀의 월별 출석표까지 보여 준다.
    if tab in ('home', 'attendance') and show['attendance']:
        for child in children:
            if child['show']['attendance'] or not child['portal']:
                for info in child['classes']:
                    month = month_text if tab == 'attendance' and child is selected else None
                    context['reports'][(child['id'], info['id'])] = attendance_report(
                        conn, info, child['id'], month)
    if tab == 'teachers':
        # 자녀가 등록된 강의부서마다 한 묶음: 같은 부서의 여러 과목은 한 곳에 모으고 강사는 한 번만 보여 준다.
        groups = {}
        for child in children:
            if not child['show']['profile']:
                continue
            for info in child['classes']:
                group = groups.setdefault((child['id'], _norm(info['department'])), {
                    'child': child, 'department': info['department'] or '', 'classes': [], 'people': []})
                group['classes'].append(info['class_name'])
                known = {person['account_id'] or person['name'] for person in group['people']}
                for person in _instructors_for_class(conn, info):
                    if (person['account_id'] or person['name']) not in known:
                        group['people'].append(person)
        context['teachers'] = list(groups.values())
    if tab == 'leaflet':
        for item in active:
            if _show(item['portal'])['leaflet']:
                pages, toc = leaflet_pages(conn, item['portal'])
                context['leaflets'].append({
                    'school': item['name'], 'portal': item['portal'], 'pages': len(pages), 'toc': toc,
                    'cover': _leaflet_page_url(item['portal']['token'], pages[0]) if pages else '',
                    'url': url_for('parent_hub.hub_leaflet', token=item['portal']['token']),
                })
    if tab == 'guide':
        for item in active:
            if not _show(item['portal'])['guide']:
                continue
            guide = _guide_for_school(conn, item['portal'])
            rooms = []
            if guide:
                for child in children:
                    for info in child['classes']:
                        if _norm(info['school_name']) != _norm(item['name']):
                            continue
                        room = _room_for_class(guide['layout'], info['class_name'], info.get('instructor_name', ''),
                                              info.get('department', ''))
                        rooms.append({
                            'child': child['name'], 'class_name': info['class_name'],
                            'room': room['label'] if room else '',
                            'url': guide['path'] + (f"?to={room['id']}" if room else ''),
                        })
            context['guides'].append({'school': item['name'], 'path': guide['path'] if guide else '',
                                      'rooms': rooms})
    if tab == 'notices':
        context['notices'] = _notices(conn, invite['guardian_id'])
    elif tab == 'home':
        context['notices'] = _notices(conn, invite['guardian_id'], 3)
    return context


# ---------------------------------------------------------------- 학부모 공개 화면

def _invalid_page(message='사용할 수 없는 링크입니다.'):
    return _no_store(Response(render_template('parent_hub/landing.html', invalid=True, message=message,
                                              **_design_context(DEFAULT_DESIGN)), 404))


@parent_hub_bp.route('/parent/hub/<token>', methods=['GET', 'POST'])
def hub_landing(token):
    """학교 링크(가정통신문 QR). 자녀 이름과 보호자 휴대폰번호를 한 번 확인하면 개인 화면으로 보낸다."""
    conn = get_db()
    try:
        portal = _portal_by_token(conn, token)
        if not portal:
            return _invalid_page('링크가 바뀌었거나 사용이 중지되었습니다. 학교나 담당 강사에게 새 링크를 요청해 주세요.')
        error = ''
        if request.method == 'GET' and request.args.get('switch'):
            response = redirect(url_for('parent_hub.hub_landing', token=token))
            response.delete_cookie(COOKIE_NAME, path='/parent/')
            return response
        if request.method == 'GET':
            remembered = pn._invite_from_token(conn, request.cookies.get(COOKIE_NAME, ''))
            if remembered and _guardian_has_child_at(conn, remembered['guardian_id'], portal):
                return redirect(url_for('parent_hub.parent_home', token=remembered['token']))
        else:
            child = re.sub(r'\s+', '', _text(request.form.get('child_name'), 40))
            phone = normalize_phone(request.form.get('phone'))
            limit_key = sa._client_key(f"phub:{portal['id']}")
            if sa._too_many_failures(limit_key):
                error = '입력한 정보가 여러 번 맞지 않아 잠시 막혔습니다. 10분 뒤 다시 시도해 주세요.'
            elif not child or not re.fullmatch(r'01\d{8,9}', phone):
                error = '자녀 이름과 보호자 휴대폰번호를 정확히 입력해 주세요.'
            else:
                guardian_id = _find_guardian(conn, portal, child, phone)
                if guardian_id:
                    invite = pn._active_invite(conn, guardian_id)
                    return _remember_guardian(
                        redirect(url_for('parent_hub.parent_home', token=invite['token'])), invite['token'])
                sa._record_failure(limit_key)
                error = ('수강생 명단의 자녀·보호자 정보와 일치하지 않습니다. 방과후 신청 때 적은 휴대폰번호로 '
                         '입력하거나 학교 담당자에게 문의해 주세요.')
        show = _show(portal)
        pages, toc = leaflet_pages(conn, portal) if show['leaflet'] else ([], [])
        guide = _guide_for_school(conn, portal) if show['guide'] else None
        return _no_store(Response(render_template(
            'parent_hub/landing.html', invalid=False, portal=portal, token=token, show=show, error=error,
            leaflet_pages=len(pages), leaflet_toc=toc, guide_path=guide['path'] if guide else '',
            form=request.form, **_design_context(design_of(portal)))))
    finally:
        conn.close()


@parent_hub_bp.route('/parent/hub/<token>/leaflet')
def hub_leaflet(token):
    """학교 e리플렛 펼쳐 보기(보호자 확인 없이 학교 링크만으로 연다)."""
    conn = get_db()
    try:
        portal = _portal_by_token(conn, token, staff_preview=True)
        if not portal or not _show(portal)['leaflet']:
            return _invalid_page('공개 중인 학교 리플렛이 없습니다.')
        pages, toc = leaflet_pages(conn, portal)
        items = [{'no': page['no'], 'url': _leaflet_page_url(token, page), 'w': page['width'], 'h': page['height']}
                 for page in pages]
        back = request.args.get('back', '')
        if not back.startswith('/parent/') or back.startswith('//'):
            back = ''
        return _no_store(Response(render_template(
            'parent_hub/leaflet.html', portal=portal, token=token, pages=pages, toc=toc,
            page_json=json.dumps(items, ensure_ascii=False).replace('<', '\\u003c'), back_url=back)))
    finally:
        conn.close()


def _serve_leaflet_file(path, name):
    if not path or not Path(path).is_file():
        abort(404)
    response = encrypted_response(path, name or 'page.jpg', as_attachment=False, mimetype='image/jpeg')
    response.headers['Cache-Control'] = 'private, max-age=600'
    return response


@parent_hub_bp.route('/parent/hub/<token>/leaflet/hq/<int:page_id>')
def leaflet_hq_image(token, page_id):
    conn = get_db()
    try:
        portal = _portal_by_token(conn, token, staff_preview=True)
        page = conn.execute('SELECT * FROM ph_leaflet_pages WHERE id=? AND portal_id=?',
                            (page_id, portal['id'])).fetchone() if portal else None
    finally:
        conn.close()
    if not page:
        abort(404)
    return _serve_leaflet_file(page['path'], page['name'])


@parent_hub_bp.route('/parent/hub/<token>/leaflet/eb/<int:page_id>')
def leaflet_eb_image(token, page_id):
    """강사 홍보 쪽 그림. 이 학교 리플렛에 실린(승인된) 쪽만 보여 준다."""
    conn = get_db()
    try:
        portal = _portal_by_token(conn, token, staff_preview=True)
        page = None
        if portal:
            for promo in _live_instructor_promos(conn, portal['school_name']):
                page = next((row for row in promo['pages'] if row['id'] == page_id), None)
                if page:
                    break
    finally:
        conn.close()
    if not page:
        abort(404)
    return _serve_leaflet_file(page['image_path'], page['image_filename'])


@parent_hub_bp.route('/parent/home/<token>')
def parent_home(token):
    """보호자 개인 화면. 개인 토큰은 학부모 알림 등록 링크의 토큰과 같다."""
    conn = get_db()
    try:
        invite = pn._invite_from_token(conn, token)
        if not invite:
            return _invalid_page('링크가 만료되었거나 새 링크로 바뀌었습니다. 학교 담당자에게 링크를 다시 요청해 주세요.')
        conn.execute('UPDATE parent_invites SET last_opened_at=CURRENT_TIMESTAMP WHERE id=?', (invite['id'],))
        conn.commit()
        tab = request.args.get('tab') if request.args.get('tab') in TABS else 'home'
        context = home_context(conn, invite, token, tab, _int(request.args.get('child')),
                               _text(request.args.get('month'), 7))
        response = _no_store(Response(render_template('parent_hub/home.html', **context)))
        return _remember_guardian(response, token)
    finally:
        conn.close()


@parent_hub_bp.route('/parent/home/<token>/photo/<int:account_id>')
def parent_teacher_photo(token, account_id):
    """내 자녀의 과목에 연결된 강사의 '승인된' 사진만 보여 준다."""
    conn = get_db()
    try:
        invite = pn._invite_from_token(conn, token)
        allowed = bool(invite) and account_id in _photo_account_ids(conn, invite['guardian_id'])
        profile = conn.execute('SELECT photo_path FROM ih_profiles WHERE account_id=?',
                               (account_id,)).fetchone() if allowed else None
    finally:
        conn.close()
    if not profile or not profile['photo_path'] or not Path(profile['photo_path']).is_file():
        abort(404)
    return _serve_leaflet_file(profile['photo_path'], 'profile.jpg')


@parent_hub_bp.route('/parent/home/<token>/manifest.webmanifest')
def parent_home_manifest(token):
    """홈 화면 아이콘을 누르면 이 보호자의 학부모 화면이 바로 열린다."""
    conn = get_db()
    try:
        valid = pn._invite_from_token(conn, token) is not None
    finally:
        conn.close()
    if not valid:
        return jsonify(ok=False, message='유효하지 않은 링크입니다.'), 404
    start_url = f'/parent/home/{token}'
    icons = '/static/images/parent_app'
    manifest = {
        'id': start_url, 'name': '새담 학부모 홈', 'short_name': '새담학부모',
        'description': '자녀 출석, 강사 소개, 방과후 e리플렛, 교실 안내를 한곳에서 봅니다.',
        'lang': 'ko', 'start_url': start_url, 'scope': '/parent/', 'display': 'standalone',
        'background_color': '#fff9f0', 'theme_color': '#e78100',
        'icons': [
            {'src': f'{icons}/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': f'{icons}/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': f'{icons}/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png',
             'purpose': 'maskable'},
        ],
    }
    response = Response(json.dumps(manifest, ensure_ascii=False), mimetype='application/manifest+json')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# ---------------------------------------------------------------- 본사 담당자 화면(인트라넷 메뉴)

def _school_stats(conn):
    """학교(공백 무시 이름)별 학생·보호자·알림등록·과목 수."""
    school_key = {row['id']: _norm(row['school_name']) for row in conn.execute('SELECT id, school_name FROM schools')}
    student_keys = {}
    for row in conn.execute('SELECT id, school_id, school_name FROM parent_students WHERE is_active=1').fetchall():
        keys = student_keys.setdefault(row['id'], set())
        if row['school_id'] in school_key:
            keys.add(school_key[row['school_id']])
        if row['school_name']:
            keys.add(_norm(row['school_name']))
    class_count = {}
    for row in conn.execute('SELECT school_name FROM parent_classes WHERE is_active=1').fetchall():
        class_count[_norm(row['school_name'])] = class_count.get(_norm(row['school_name']), 0) + 1
    for row in conn.execute('''
        SELECT cs.student_id, c.school_name FROM parent_class_students cs
        JOIN parent_classes c ON c.id=cs.class_id AND c.is_active=1
    ''').fetchall():
        if row['student_id'] in student_keys:
            student_keys[row['student_id']].add(_norm(row['school_name']))
    guardians = {}
    for row in conn.execute('''
        SELECT gs.student_id, gs.guardian_id FROM parent_guardian_students gs
        JOIN parent_guardians g ON g.id=gs.guardian_id AND g.is_active=1
    ''').fetchall():
        guardians.setdefault(row['student_id'], set()).add(row['guardian_id'])
    pushing = {row[0] for row in conn.execute(
        'SELECT DISTINCT guardian_id FROM parent_push_subscriptions WHERE is_active=1').fetchall()}
    stats = {}
    for student_id, keys in student_keys.items():
        for key in keys:
            item = stats.setdefault(key, {'students': 0, 'guardians': set()})
            item['students'] += 1
            item['guardians'] |= guardians.get(student_id, set())
    return {key: {'students': item['students'], 'guardians': len(item['guardians']),
                  'push': len(item['guardians'] & pushing), 'classes': class_count.get(key, 0)}
            for key, item in stats.items()}, class_count


def _admin_redirect(tab='portal', school=''):
    """처리 뒤에 통합지원 화면으로 돌아간다(보던 학교·탭 유지)."""
    target = request.form.get('next') or ''
    if target.startswith(('/support-hub', '/parent-hub', '/instructor-hub')) and not target.startswith('//'):
        return redirect(target)
    return redirect(url_for('support_hub.admin_page', tab=tab, school=school or None))


def _new_portal(conn, school_id, school_name):
    return conn.execute('INSERT INTO ph_portals (school_id, school_name, token) VALUES (?, ?, ?)',
                        (school_id, school_name, secrets.token_urlsafe(18))).lastrowid


@parent_hub_bp.route('/parent-hub/')
@parent_hub_bp.route('/parent-hub')
@menu_permission_required(MENU_KEY)
def admin_page():
    """예전 학부모통합지원 주소. 지금은 학부모·강사 통합지원 화면의 [학부모 안내] 탭으로 보낸다."""
    school = _text(request.args.get('school'), 100)
    return redirect(url_for('support_hub.admin_page', tab='portal', school=school or None))


@parent_hub_bp.route('/parent-hub/portals/create', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_create_portals():
    """학교 이름 하나(school_name) 또는 포털이 없는 모든 학교(all=1)의 포털을 만든다."""
    conn = get_db()
    try:
        made = 0
        existing = _portals_by_school(conn)
        if request.form.get('all'):
            targets = [(s['id'], s['name']) for s in hub._catalog_schools(conn)]
        else:
            school_id, school_name = hub.canonical_school(conn, request.form.get('school_name'))
            targets = [(school_id, school_name)] if school_id else []
            if not targets:
                flash('학교관리에 등록된 학교만 포털을 만들 수 있습니다.', 'error')
        for school_id, school_name in targets:
            if _norm(school_name) in existing:
                continue
            _new_portal(conn, school_id, school_name)
            made += 1
        conn.commit()
        if made:
            flash(f'학부모 포털 {made}개를 만들었습니다. 학교 리플렛을 채우고 링크·QR을 학부모에게 안내해 주세요.', 'success')
        elif targets:
            flash('이미 포털이 만들어진 학교입니다.', 'error')
    finally:
        conn.close()
    return _admin_redirect('portal')


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/save', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_save_portal(portal_id):
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM ph_portals WHERE id=?', (portal_id,)).fetchone():
            abort(404)
        conn.execute('''
            UPDATE ph_portals SET title=?, notice=?, show_attendance=?, show_profile=?,
                show_leaflet=?, show_guide=? WHERE id=?
        ''', (_text(request.form.get('title'), 100), _multiline(request.form.get('notice'), 500),
              1 if request.form.get('show_attendance') else 0, 1 if request.form.get('show_profile') else 0,
              1 if request.form.get('show_leaflet') else 0, 1 if request.form.get('show_guide') else 0, portal_id))
        conn.commit()
        flash('포털 설정을 저장했습니다.', 'success')
    finally:
        conn.close()
    return _admin_redirect('portal')


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/design', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_save_design(portal_id):
    """학부모가 휴대폰으로 보는 화면의 디자인을 고른다(학교마다 하나)."""
    design = request.form.get('design')
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM ph_portals WHERE id=?', (portal_id,)).fetchone():
            abort(404)
        if design not in DESIGNS:
            flash('고를 수 없는 디자인입니다.', 'error')
        else:
            conn.execute('UPDATE ph_portals SET design=? WHERE id=?', (design, portal_id))
            conn.commit()
            flash(f"학부모 화면 디자인을 [{DESIGNS[design]['label']}]으로 바꿨습니다. 학부모가 다음에 열 때부터 바뀐 모습으로 보입니다.",
                  'success')
    finally:
        conn.close()
    return _admin_redirect('portal')


def _demo_report(today):
    """디자인 미리보기용 예시 출석 자료(실제 학생 정보가 아니다)."""
    cycle = ['출석', '출석', '지각', '출석', '출석', '결석', '출석']
    class_days = {0, 2}                                              # 월·수
    cells = [None] * ((today.replace(day=1).weekday() + 1) % 7)
    shown = 0
    for day in range(1, monthrange(today.year, today.month)[1] + 1):
        current = today.replace(day=day)
        is_class = current.weekday() in class_days
        status = ''
        if is_class and current < today:
            status = cycle[shown % len(cycle)]
            shown += 1
        cells.append({'day': day, 'status': status, 'time': '15:02' if status and status != '결석' else '',
                      'class_day': is_class, 'future': current > today, 'today': current == today})
    absent = sum(1 for c in cells if c and c['status'] == '결석')
    late = sum(1 for c in cells if c and c['status'] == '지각')
    present = shown - absent - late
    held = max(shown, 1)
    rate = round((present + late) * 100 / held)
    recent = [{'date': today.isoformat(), 'weekday': '월', 'status': '출석', 'time': '15:02', 'dismissed': '16:31'},
              {'date': today.isoformat(), 'weekday': '수', 'status': '지각', 'time': '15:14', 'dismissed': '16:30'},
              {'date': today.isoformat(), 'weekday': '월', 'status': '결석', 'time': '', 'dismissed': ''}]
    return {
        'class_id': 0,
        'today': {'status': '출석', 'time': '15:02', 'dismissed': '', 'class_day': True},
        'total': {'present': present, 'late': late, 'absent': absent, 'held': held, 'rate': rate},
        'month': {'key': today.strftime('%Y-%m'), 'label': f'{today.year}년 {today.month}월', 'cells': cells,
                  'previous': '', 'next': '', 'present': present, 'late': late, 'absent': absent, 'held': held,
                  'rate': rate},
        'recent': recent, 'has_records': True,
    }


def demo_home_context(conn, portal, tab, design):
    """디자인 미리보기용 학부모 홈 화면 자료. 예시 자녀·과목을 쓰고 실제 학생 정보는 담지 않는다."""
    today = sa._now().date()
    school = portal['school_name']
    classes = [
        {'id': 1, 'class_name': '로봇과학A', 'department': '로봇과학', 'school_name': school, 'instructor_name': '이새담',
         'schedule': '월·수 15:00~16:30', 'notify_parents': 1, 'weekdays': '0,2'},
        {'id': 2, 'class_name': '창의미술B', 'department': '미술', 'school_name': school, 'instructor_name': '박미술',
         'schedule': '금 14:30~16:00', 'notify_parents': 1, 'weekdays': '4'},
    ]
    shown = {'attendance': True, 'profile': True, 'leaflet': True, 'guide': True}
    child = {'id': 1, 'name': '김새담', 'grade': '3', 'classroom': '2', 'school_name': school, 'classes': classes,
             'schools': [school], 'portal': portal, 'show': shown}
    tabs = [('home', '홈', 'fa-house'), ('attendance', '출석', 'fa-calendar-check'),
            ('teachers', '강사', 'fa-chalkboard-user'), ('leaflet', '리플렛', 'fa-book-open-reader'),
            ('guide', '교실안내', 'fa-map-location-dot'), ('notices', '알림', 'fa-bell')]
    tab = tab if tab in {key for key, _label, _icon in tabs} else 'home'
    notices = [
        {'kind': '출석', 'title': '김새담 학생이 도착했어요', 'body': '로봇과학A 수업에 15:02에 도착했어요.',
         'created_at': f'{today} 15:02'},
        {'kind': '공지', 'title': '이번 주 수업 안내', 'body': '금요일은 학교 행사로 수업이 오후 3시에 시작해요.',
         'created_at': f'{today} 09:10'},
    ]
    pages, toc = leaflet_pages(conn, portal)
    guide = _guide_for_school(conn, portal)
    lead = {'school_name': school, 'notice': portal['notice'] or '학부모 안내 문구가 이 자리에 보여요.'}
    return {
        **_design_context(design), 'demo': True, 'demo_portal_id': portal['id'],
        'token': 'demo', 'guardian_name': '새담', 'tab': tab, 'tabs': tabs, 'children': [child], 'selected': child,
        'schools': [{'name': school, 'portal': portal}], 'show': shown,
        'push': {'registered': False, 'count': 0, 'last_received_at': '', 'warn': False}, 'push_configured': False,
        'notices': notices, 'month': '', 'weekday_labels': WEEKDAY_FIRST_SUNDAY, 'portal_notices': [lead],
        'reports': {(1, info['id']): _demo_report(today) for info in classes} if tab in ('home', 'attendance') else {},
        'more_tabs': [item for item in tabs if item[0] in ('teachers', 'leaflet', 'guide')],
        'teachers': [{'child': child, 'department': '로봇과학', 'classes': ['로봇과학A'], 'people': [
            {'account_id': None, 'name': '이새담', 'has_photo': False, 'prepared': True,
             'bio': '로봇 코딩과 센서 실험을 함께 하며 아이들의 호기심을 키웁니다.'}]}] if tab == 'teachers' else [],
        'leaflets': [{'school': school, 'portal': portal, 'pages': len(pages), 'toc': toc,
                      'cover': _leaflet_page_url(portal['token'], pages[0]) if pages else '',
                      'url': url_for('parent_hub.hub_leaflet', token=portal['token'])}] if tab == 'leaflet' else [],
        'guides': [{'school': school, 'path': guide['path'] if guide else '', 'rooms': []}] if tab == 'guide' else [],
    }


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/design-preview')
@menu_permission_required(MENU_KEY)
def admin_design_preview(portal_id):
    """고르기 전에 디자인을 예시 화면으로 먼저 본다(보호자·학생 정보 없이, 읽기 전용)."""
    design = request.args.get('design')
    if design not in DESIGNS:
        abort(404)
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
        if not portal:
            abort(404)
        context = demo_home_context(conn, portal, request.args.get('tab') or 'home', design)
        return _no_store(Response(render_template('parent_hub/home.html', **context)))
    finally:
        conn.close()


def _delete_leaflet_files(conn, portal_id):
    for page in _hq_pages(conn, portal_id):
        delete_file(page['path'])
    conn.execute('DELETE FROM ph_leaflet_pages WHERE portal_id=?', (portal_id,))


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/<action>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_portal_action(portal_id, action):
    conn = get_db()
    try:
        if not conn.execute('SELECT 1 FROM ph_portals WHERE id=?', (portal_id,)).fetchone():
            abort(404)
        if action == 'regenerate':
            conn.execute('UPDATE ph_portals SET token=? WHERE id=?', (secrets.token_urlsafe(18), portal_id))
            flash('링크를 새로 만들었습니다. 예전 링크·QR은 더 이상 열리지 않습니다.', 'success')
        elif action == 'toggle':
            conn.execute('UPDATE ph_portals SET is_active=1-is_active WHERE id=?', (portal_id,))
            flash('공개 상태를 바꿨습니다.', 'success')
        elif action == 'delete':
            _delete_leaflet_files(conn, portal_id)
            conn.execute('DELETE FROM ph_portals WHERE id=?', (portal_id,))
            flash('포털과 본사가 올린 리플렛 쪽을 삭제했습니다. 강사 홍보 자료는 그대로 남아 있습니다.', 'success')
        else:
            abort(404)
        conn.commit()
    finally:
        conn.close()
    return _admin_redirect('portal')


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/qr.svg')
@menu_permission_required(MENU_KEY)
def admin_portal_qr(portal_id):
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
    finally:
        conn.close()
    if not portal:
        abort(404)
    return sa._svg_response(_portal_url(portal))


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/leaflet', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_leaflet_upload(portal_id):
    """본사가 학교 리플렛의 앞쪽·뒤쪽에 그림·PDF를 덧붙인다(쪽은 파일 이름 순으로 들어간다)."""
    section = request.form.get('section') if request.form.get('section') in LEAFLET_SECTIONS else 'front'
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
        if not portal:
            abort(404)
        back = _admin_redirect('portal', portal['school_name'])
        existing = _hq_pages(conn, portal_id, section)
        try:
            frames = hub._frames_from_uploads(request.files.getlist('files'))
        except ValueError as exc:
            flash(str(exc), 'error')
            return back
        if len(existing) + len(frames) > MAX_HQ_PAGES:
            flash(f'{LEAFLET_SECTIONS[section]}에는 최대 {MAX_HQ_PAGES}쪽까지 둘 수 있습니다. '
                  f'지금 {len(existing)}쪽이 있어 {len(frames)}쪽을 더할 수 없습니다.', 'error')
            return back
        folder = LEAFLET_ROOT / f'p{portal_id}'
        position = max((row['position'] for row in existing), default=0)
        for jpeg, width, height, name in frames:
            position += 1
            path = hub._store_jpeg(folder, f'{section}{position}', jpeg)
            conn.execute('''
                INSERT INTO ph_leaflet_pages (portal_id, section, position, path, name, width, height, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (portal_id, section, position, path, name, width, height, _admin_name()))
        conn.commit()
        flash(f'{LEAFLET_SECTIONS[section]}에 {len(frames)}쪽을 올렸습니다.', 'success')
        return back
    finally:
        conn.close()


@parent_hub_bp.route('/parent-hub/leaflet/pages/<int:page_id>/<action>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_leaflet_page(page_id, action):
    """본사가 올린 쪽을 지우거나 앞·뒤로 옮긴다."""
    conn = get_db()
    try:
        page = conn.execute('SELECT p.*, o.school_name FROM ph_leaflet_pages p JOIN ph_portals o ON o.id=p.portal_id '
                            'WHERE p.id=?', (page_id,)).fetchone()
        if not page:
            abort(404)
        if action == 'delete':
            delete_file(page['path'])
            conn.execute('DELETE FROM ph_leaflet_pages WHERE id=?', (page_id,))
        elif action in ('up', 'down'):
            siblings = [dict(row) for row in _hq_pages(conn, page['portal_id'], page['section'])]
            index = next(i for i, row in enumerate(siblings) if row['id'] == page_id)
            other = index - 1 if action == 'up' else index + 1
            if 0 <= other < len(siblings):
                siblings[index], siblings[other] = siblings[other], siblings[index]
                for number, row in enumerate(siblings, 1):
                    conn.execute('UPDATE ph_leaflet_pages SET position=? WHERE id=?', (number, row['id']))
        else:
            abort(404)
        conn.commit()
        return _admin_redirect('portal', page['school_name'])
    finally:
        conn.close()


@parent_hub_bp.route('/parent-hub/leaflet/pages/<int:page_id>/image')
@menu_permission_required(MENU_KEY)
def admin_leaflet_image(page_id):
    conn = get_db()
    try:
        page = conn.execute('SELECT * FROM ph_leaflet_pages WHERE id=?', (page_id,)).fetchone()
    finally:
        conn.close()
    if not page:
        abort(404)
    return _serve_leaflet_file(page['path'], page['name'])


# ---------------------------------------------------------------- 학부모 링크 문자 발송 · 인쇄용 안내문

LINK_TARGETS = {'all': '이 학교 보호자 모두', 'no_push': '알림을 아직 안 켠 보호자만',
                'not_sent': '안내 링크를 아직 못 받은 보호자만'}
# 발송 방법: 솔라피 문자(SMS/LMS) 또는 카카오 알림톡. 알림톡 템플릿은 통합관리 > 솔라피설정에 저장한다.
LINK_METHODS = {'kakao': '카카오 알림톡', 'text': '문자'}
LINK_METHODS_WITH = {'kakao': '카카오 알림톡으로', 'text': '문자로'}      # "~으로/로" 조사까지 붙은 말
LINK_SUBJECT = '[새담] 방과후 학부모 안내'
# 알림톡 템플릿 #{변수}에 넣을 값은 학교 이름과 안내 주소 두 가지다. 변수 이름은 솔라피설정에서 정하고(solapi.link_variable_source),
# 정하지 않은 변수는 이름 속 단어(학교·school·url·링크…)로 짐작한다.
DEFAULT_LINK_INTRO = '자녀 출석 알림·출석률, 강사 소개, 방과후 e리플렛, 3D 교실 안내를 한곳에서 확인하세요.'
PHONE_PATTERN = re.compile(r'01\d{8,9}')


def school_student_ids(conn, portal):
    """이 학교 학생(학교가 같거나 이 학교 과목을 듣는 학생)의 id 집합."""
    key = _norm(portal['school_name'])
    active, ids = set(), set()
    for row in conn.execute('SELECT id, school_id, school_name FROM parent_students WHERE is_active=1').fetchall():
        active.add(row['id'])
        if (row['school_name'] and _norm(row['school_name']) == key) \
                or (portal['school_id'] and row['school_id'] == portal['school_id']):
            ids.add(row['id'])
    for row in conn.execute('''
        SELECT cs.student_id, c.school_name FROM parent_class_students cs
        JOIN parent_classes c ON c.id=cs.class_id AND c.is_active=1
    ''').fetchall():
        if row['student_id'] in active and _norm(row['school_name']) == key:
            ids.add(row['student_id'])
    return ids


def _phone_label(value):
    """명단에 보일 휴대폰번호(010-1234-5678). 형식이 틀리면 입력값 그대로 보인다."""
    try:
        return solapi.format_phone(value) or str(value or '')
    except ValueError:
        return str(value or '')


def school_guardians(conn, portal):
    """이 학교 학생의 보호자(한 명이 한 줄): 이름·휴대폰·자녀·알림 등록·링크 문자 발송 여부."""
    ids = sorted(school_student_ids(conn, portal))
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    guardians = conn.execute(f'''
        SELECT g.id, g.name, g.phone, GROUP_CONCAT(DISTINCT s.name) AS children
        FROM parent_guardian_students gs
        JOIN parent_guardians g ON g.id=gs.guardian_id AND g.is_active=1
        JOIN parent_students s ON s.id=gs.student_id
        WHERE gs.student_id IN ({marks}) GROUP BY g.id ORDER BY g.name COLLATE NOCASE, g.id
    ''', ids).fetchall()
    pushing = {row[0] for row in conn.execute(
        'SELECT DISTINCT guardian_id FROM parent_push_subscriptions WHERE is_active=1').fetchall()}
    sent = {row['guardian_id']: row['sms_sent_at'] for row in conn.execute(
        'SELECT guardian_id, MAX(sms_sent_at) AS sms_sent_at FROM parent_invites GROUP BY guardian_id').fetchall()}
    return [{
        'id': row['id'], 'name': row['name'], 'phone': row['phone'], 'children': (row['children'] or '').split(','),
        'push': row['id'] in pushing, 'sms_sent_at': sent.get(row['id']) or '',
        'phone_ok': bool(PHONE_PATTERN.fullmatch(normalize_phone(row['phone']))),
        'phone_label': _phone_label(row['phone']),
    } for row in guardians]


def link_audience(guardians):
    """링크 문자 발송 화면의 대상별 인원."""
    sendable = [g for g in guardians if g['phone_ok']]
    return {
        'all': len(sendable),
        'no_push': sum(1 for g in sendable if not g['push']),
        'not_sent': sum(1 for g in sendable if not g['sms_sent_at']),
        'invalid': len(guardians) - len(sendable),
        'total': len(guardians),
    }


def pick_guardians(guardians, target):
    sendable = [g for g in guardians if g['phone_ok']]
    if target == 'no_push':
        return [g for g in sendable if not g['push']]
    if target == 'not_sent':
        return [g for g in sendable if not g['sms_sent_at']]
    return sendable


def link_message(portal, intro, link):
    return f"[{portal['school_name']}] 방과후 학부모 안내\n{intro}\n{link}"


def kakao_link_values(template, school_name, link, names=None):
    """알림톡 템플릿의 #{변수}에 넣을 값을 정한다(학교 이름·안내 주소 두 가지).

    names는 솔라피설정에서 정한 변수 이름({'school': …, 'url': …}, 없으면 기본 이름).
    버튼 주소가 https://#{url} 처럼 스킴 뒤에 변수를 두면 https://를 뺀 주소를 넣는다.
    반환: (솔라피에 넘길 {'#{변수}': 값}, 화면 표시용 행 목록, 값을 정하지 못한 변수명)
    """
    values, rows, missing = {}, [], []
    for item in template.get('variables') or []:
        source = solapi.link_variable_source(item['name'], names)
        value = {'school': school_name, 'url': link}.get(source, '')
        if source == 'url' and item.get('strip_scheme'):
            value = re.sub(r'^https?://', '', value)
        if not value:
            missing.append(item['name'])
        values[item['key']] = value
        rows.append({'name': item['name'], 'key': item['key'], 'value': value,
                     'source_label': solapi.LINK_VARIABLE_LABELS.get(source, '채울 수 없는 변수')})
    return values, rows, missing


def link_variable_keys(names=None):
    """화면에 보일 변수 표기. 예: {'school': '#{학교명}', 'url': '#{url}'}"""
    return {source: f'#{{{name}}}' for source, name in solapi.effective_link_variable_names(names).items()}


def _missing_variables_message(missing, names=None):
    unknown = ', '.join(f'#{{{name}}}' for name in missing)
    keys = link_variable_keys(names)
    return (f"알림톡 템플릿의 변수 {unknown} 에 넣을 값을 정할 수 없습니다. 템플릿에는 학교 이름 변수 {keys['school']}와 "
            f"안내 주소 변수 {keys['url']} 두 변수만 쓸 수 있습니다. 템플릿의 변수 이름이 다르면 통합관리 > 솔라피설정에서 "
            '[학부모 안내 링크]의 변수 이름을 맞춰 주세요.')


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/kakao-preview')
@menu_permission_required(MENU_KEY)
def admin_kakao_preview(portal_id):
    """링크 발송 화면에서 알림톡을 고르면 템플릿·변수표·미리보기를 보여 준다(조회만 하고 보내지 않는다)."""
    shared = request.args.get('link_kind') == 'school'
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
        if not portal:
            abort(404)
        settings = solapi.get_settings(conn)
    finally:
        conn.close()
    if not settings.get('parent_kakao_configured'):
        return jsonify({'ok': False, 'configured': False,
                        'error': '알림톡 템플릿이 아직 설정되지 않았습니다. 통합관리 > 솔라피설정에서 '
                                 '[학부모 안내 알림톡 템플릿 ID]를 저장해 주세요.'})
    try:
        template = solapi.fetch_kakao_template(settings['parent_template_id'], settings=settings,
                                               refresh=request.args.get('refresh') == '1')
    except RuntimeError as exc:
        return jsonify({'ok': False, 'configured': True, 'error': str(exc)})
    link = _portal_url(portal) if shared else _external_root() + '/parent/home/(보호자마다 다른 개인 주소)'
    names = settings.get('parent_variable_names')
    values, rows, missing = kakao_link_values(template, portal['school_name'], link, names)
    problem = solapi.kakao_template_problem(template) or (_missing_variables_message(missing, names) if missing else '')
    return jsonify({
        'ok': not problem, 'configured': True, 'problem': problem,
        'template': {'name': template['name'], 'status': template['status'], 'template_id': template['template_id']},
        'variables': rows, 'preview': solapi.kakao_template_preview(template, values),
    })


def _form_ids(name):
    ids = set()
    for value in request.form.getlist(name):
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/send', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_send_link(portal_id):
    """학부모 링크를 문자 또는 카카오 알림톡으로 한꺼번에 보낸다(담당자가 확인 창에서 동의했을 때만 호출된다).

    개인 링크: 보호자마다 자기 화면(/parent/home/…)으로 바로 열린다(이름·번호 입력 없음).
    학교 공통 링크: 모두 같은 학교 링크로 들어와 자녀 이름과 보호자 번호를 한 번 확인한다.
    custom=1이면 화면에서 고른 보호자(guardian_ids)에게만 보낸다(대상 안에 있는 사람만 인정).
    """
    target = request.form.get('target') if request.form.get('target') in LINK_TARGETS else 'all'
    method = request.form.get('method') if request.form.get('method') in LINK_METHODS else 'text'
    shared = request.form.get('link_kind') == 'school'
    intro = _multiline(request.form.get('intro'), 300) or DEFAULT_LINK_INTRO
    fallback = (request.form.getlist('fallback') or ['1'])[-1] == '1'
    custom = request.form.get('custom') == '1'
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
        if not portal:
            abort(404)
        back = _admin_redirect('portal', portal['school_name'])
        if not portal['is_active']:
            flash('공개 중이 아닌 포털은 링크를 보낼 수 없습니다. 먼저 [공개로]를 눌러 주세요.', 'error')
            return back
        guardians = school_guardians(conn, portal)
        targets = pick_guardians(guardians, target)
        if custom:
            picked = _form_ids('guardian_ids')
            targets = [g for g in targets if g['id'] in picked]
            if not targets:
                flash('보낼 보호자를 한 명 이상 선택해 주세요. (명단에서 체크한 사람에게만 보냅니다)', 'error')
                return back
        if not targets:
            flash(f"링크를 보낼 보호자가 없습니다. ({LINK_TARGETS[target]} · 보호자 휴대폰번호가 올바른 사람만 보냅니다)", 'error')
            return back
        settings = template = None
        if method == 'kakao':
            settings = solapi.get_settings(conn)
            if not settings.get('parent_kakao_configured'):
                flash('알림톡 템플릿이 아직 설정되지 않았습니다. 통합관리 > 솔라피설정에서 [학부모 안내 알림톡 템플릿 ID]를 '
                      '저장하거나, 발송 방법을 [문자]로 바꿔 주세요.', 'error')
                return back
            try:
                template = solapi.fetch_kakao_template(settings['parent_template_id'], settings=settings)
            except RuntimeError as exc:
                flash(f'알림톡 템플릿을 불러오지 못했습니다: {exc}', 'error')
                return back
            problem = solapi.kakao_template_problem(template)
            if problem:
                flash(problem, 'error')
                return back
        messages = []
        for guardian in targets:
            if shared:
                link = _portal_url(portal)
            else:
                invite = pn._active_invite(conn, guardian['id'])
                link = _external_root() + url_for('parent_hub.parent_home', token=invite['token'])
            text = link_message(portal, intro, link)
            if method == 'kakao':
                values, _, missing = kakao_link_values(template, portal['school_name'], link,
                                                       settings.get('parent_variable_names'))
                if missing:
                    flash(_missing_variables_message(missing, settings.get('parent_variable_names')), 'error')
                    return back
                messages.append({'to': guardian['phone'], 'variables': values,
                                 'fallback_text': text, 'fallback_subject': LINK_SUBJECT})
            else:
                messages.append({'to': guardian['phone'], 'text': text, 'subject': LINK_SUBJECT})
        try:
            if method == 'kakao':
                results = solapi.send_bulk_alimtalk(
                    messages, pf_id=settings['parent_channel_id'], template_id=settings['parent_template_id'],
                    fallback=fallback, settings=settings)
            else:
                results = solapi.send_bulk_text(messages)
        except Exception as exc:  # 솔라피 설정 누락·네트워크 오류 등
            flash(f'{LINK_METHODS[method]} 발송에 실패했습니다: {exc}', 'error')
            return back
        sent, failed = 0, []
        for guardian, result in zip(targets, results):
            if result.get('ok'):
                sent += 1
                invite = pn._active_invite(conn, guardian['id'])
                conn.execute('UPDATE parent_invites SET sms_sent_at=CURRENT_TIMESTAMP, sms_send_count=sms_send_count+1 '
                             'WHERE id=?', (invite['id'],))
            else:
                failed.append(f"{guardian['name']}({result.get('error') or '알 수 없는 오류'})")
        if sent:
            conn.execute('UPDATE ph_portals SET last_sent_at=?, last_sent_count=?, last_sent_method=? WHERE id=?',
                         (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), sent, method, portal_id))
        conn.commit()
    finally:
        conn.close()
    message = f"{portal['school_name']} 보호자 {sent}명에게 학부모 안내 링크를 {LINK_METHODS_WITH[method]} 보냈습니다."
    if method == 'kakao' and sent:
        message += ('\n알림톡은 접수된 기준입니다. ' + ('받지 못한 분께는 문자로 대신 보냅니다.' if fallback else '받지 못해도 문자로 다시 보내지 않습니다.'))
    if failed:
        message += f"\n보내지 못한 보호자 {len(failed)}명: " + ', '.join(failed[:5])
    flash(message, 'error' if failed and not sent else 'success')
    return back


@parent_hub_bp.route('/parent-hub/portals/<int:portal_id>/print')
@menu_permission_required(MENU_KEY)
def admin_portal_print(portal_id):
    """가정통신문에 붙이는 인쇄용 안내문(QR + 주소 + 이용 방법)."""
    conn = get_db()
    try:
        portal = conn.execute('SELECT * FROM ph_portals WHERE id=?', (portal_id,)).fetchone()
    finally:
        conn.close()
    if not portal:
        abort(404)
    return render_template('parent_hub/print.html', portal=portal, url=_portal_url(portal),
                           qr=url_for('parent_hub.admin_portal_qr', portal_id=portal_id))
