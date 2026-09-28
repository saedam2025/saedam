"""학교관리 > 교실안내.

학교별로 건물(본관·후관·별관·연결통로 등), 층별 교실(일반·방과후·특별교실 등),
운동장·정문 같은 바깥 시설을 입력해 두면 화면이 층별 3D 입체 안내도를 그리고,
출발지(정문 등)에서 교실까지의 이동 경로를 안내한다.

배치 정보는 학교마다 JSON 한 벌(layout_json)로 저장한다. 화면에서 편집한 결과를
통째로 받아 이 모듈의 normalize_layout()으로 형식·범위를 검증한 뒤 저장한다.
학부모·수강생은 공유 링크(/school/guide/p/<token>)로 로그인 없이 안내도를 연다.

평면도 자동 그리기: 학교 평면도(이미지·PDF)를 올리면 통합관리 AI 프리셋으로 도면을 읽어
층별 배치도 초안을 만든다(services/floorplan_import.py). 올린 도면은 암호화해 보관하고,
편집 화면에서 밑그림으로 깔아 초안을 고칠 때 쓴다. 전체를 새로 그리면 학교의 도면을 새 도면 한 벌로 바꾸고,
건물만 더하면(후관·별관 등, mode=append) 이전 도면에 그 건물 도면을 보탠다.
"""

from __future__ import annotations

import io
import json
import math
import re
import secrets
from datetime import datetime

import segno
from flask import (
    Blueprint, Response, abort, jsonify, redirect, render_template, request, session, url_for,
)

from services import floorplan_import

from .database import get_db
from .menu_access import menu_is_allowed
from .secure_files import delete_file, encrypt_bytes, encrypted_response, encrypted_storage_name
from .solapi_settings import resolve_public_origin
from .storage import SCHOOL_UPLOADS

FLOORPLAN_ROOT = SCHOOL_UPLOADS / 'classroom_guide_plans'

classroom_guide_bp = Blueprint('classroom_guide', __name__)

VIEW_MENU = 'school_classroom_guide'
EDIT_MENU = 'school_classroom_guide_edit'

LAYOUT_VERSION = 1

ROOM_TYPES = (
    'general', 'afterschool', 'special', 'care', 'office', 'restroom',
    'corridor', 'entrance', 'stairs', 'elevator', 'etc',
)
BUILDING_KINDS = ('main', 'annex', 'wing', 'special', 'gym', 'connector', 'etc')
LANDMARK_TYPES = ('field', 'parking', 'garden', 'playground', 'etc')
GATE_KINDS = ('main', 'back')          # 교문 · 후문 (담장 위). 건물출입문은 건물의 doors 에 둔다.
SIDES = ('n', 'e', 's', 'w')
STAIR_SHAPES = ('u', 'straight')

# 한 학교 배치도의 크기 상한. 3D 화면과 길찾기가 휴대폰에서도 부드럽게 돌도록 제한한다.
MAX_SITE_CELLS = 160
MAX_BUILDINGS = 20
MAX_FLOORS = 12
MAX_BUILDING_CELLS = 80
MAX_ROOMS = 1500
MAX_COURSES_PER_ROOM = 12
MAX_LANDMARKS = 40
MAX_GATES = 6
MAX_DOORS_PER_BUILDING = 12
MAX_GATE_WIDTH = 12
MAX_DOOR_WIDTH = 8
MAX_LAYOUT_BYTES = 1_500_000

_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


class LayoutError(ValueError):
    """화면에 그대로 보여줄 수 있는 배치도 검증 오류."""


# ---------------------------------------------------------------- 스키마
def ensure_classroom_guide_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS classroom_guide_maps (
            school_id INTEGER PRIMARY KEY,
            layout_json TEXT NOT NULL DEFAULT '{}',
            share_token TEXT UNIQUE,
            share_enabled INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 평면도 자동 그리기: 올린 도면(쪽마다 한 줄)과 밑그림 위치 정보, AI 사용 기록.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS classroom_guide_floorplans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 0,
            original_name TEXT NOT NULL DEFAULT '',
            stored_path TEXT NOT NULL,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_classroom_guide_floorplans_school
        ON classroom_guide_floorplans(school_id, position)
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS classroom_guide_floorplan_meta (
            school_id INTEGER PRIMARY KEY,
            meta_json TEXT NOT NULL DEFAULT '{}',
            created_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS classroom_guide_ai_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            emp_no TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def init_classroom_guide_schema():
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 검증
def _text(value, limit):
    return re.sub(r'\s+', ' ', str(value if value is not None else '')).strip()[:limit]


def _multiline(value, limit):
    lines = str(value if value is not None else '').replace('\r\n', '\n').split('\n')
    return '\n'.join(line.rstrip() for line in lines).strip()[:limit]


def _int(value, low, high, default):
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _choice(value, choices, default):
    value = str(value or '').strip()
    return value if value in choices else default


def _color(value, default=''):
    value = str(value or '').strip()
    return value if _COLOR_RE.match(value) else default


class _IdMaker:
    """화면에서 온 id를 유지하되, 비었거나 중복되면 새로 붙인다."""

    def __init__(self):
        self.used = set()

    def __call__(self, raw, prefix):
        value = str(raw or '').strip()
        if not _ID_RE.match(value) or value in self.used:
            value = f'{prefix}{secrets.token_hex(4)}'
            while value in self.used:
                value = f'{prefix}{secrets.token_hex(4)}'
        self.used.add(value)
        return value


def _normalize_course(raw):
    if not isinstance(raw, dict):
        return None
    course = {
        'name': _text(raw.get('name'), 40),
        'teacher': _text(raw.get('teacher'), 30),
        'time': _text(raw.get('time'), 60),
        'grade': _text(raw.get('grade'), 30),
    }
    return course if any(course.values()) else None


def _normalize_room(raw, building, make_id):
    if not isinstance(raw, dict):
        return None
    top_floor = building['baseFloor'] + building['floors'] - 1
    floor = _int(raw.get('floor'), building['baseFloor'], top_floor, building['baseFloor'])
    x = _int(raw.get('x'), 0, building['cols'] - 1, 0)
    z = _int(raw.get('z'), 0, building['rows'] - 1, 0)
    w = _int(raw.get('w'), 1, building['cols'] - x, 1)
    d = _int(raw.get('d'), 1, building['rows'] - z, 1)
    courses = []
    for item in raw.get('courses') or []:
        course = _normalize_course(item)
        if course:
            courses.append(course)
        if len(courses) >= MAX_COURSES_PER_ROOM:
            break
    room = {
        'id': make_id(raw.get('id'), 'r'),
        'floor': floor,
        'type': _choice(raw.get('type'), ROOM_TYPES, 'general'),
        'no': _text(raw.get('no'), 12),
        'name': _text(raw.get('name'), 30),
        'x': x, 'z': z, 'w': w, 'd': d,
        'courses': courses,
        'note': _multiline(raw.get('note'), 300),
    }
    if room['type'] == 'stairs':
        # 오르는 방향·모양. 비어 있으면 화면이 계단 크기로 알아서 정한다.
        room['dir'] = _choice(raw.get('dir'), SIDES, '')
        room['shape'] = _choice(raw.get('shape'), STAIR_SHAPES, '')
    return room


def _normalize_door(raw, building, make_id):
    """건물출입문: 1층 외벽 한 변(side) 위 시작 칸(at)부터 폭(w)칸."""
    if not isinstance(raw, dict):
        return None
    side = _choice(raw.get('side'), SIDES, 's')
    length = building['cols'] if side in ('n', 's') else building['rows']
    w = _int(raw.get('w'), 1, min(MAX_DOOR_WIDTH, length), 2)
    return {
        'id': make_id(raw.get('id'), 'd'),
        'label': _text(raw.get('label'), 20) or '출입구',
        'side': side,
        'at': _int(raw.get('at'), 0, length - w, 0),
        'w': w,
    }


def _normalize_gate(raw, site, make_id):
    """교문·후문: 담장 한 변(side) 위 시작 칸(at)부터 폭(w)칸. x, z 는 가운데 칸.

    예전 자료처럼 side 없이 x, z 한 점만 있으면 가장 가까운 담장으로 붙이고 그 점을 가운데로 둔다.
    """
    if not isinstance(raw, dict):
        return None
    label = _text(raw.get('label'), 20)
    kind = _choice(raw.get('kind'), GATE_KINDS, 'back' if '후문' in label else 'main')
    side = _choice(raw.get('side'), SIDES, '')
    width = _int(raw.get('w'), 1, MAX_GATE_WIDTH, 3)
    if side:
        at = _int(raw.get('at'), 0, MAX_SITE_CELLS, 0)
    else:
        fx = _int(raw.get('x'), 0, site['cols'] - 1, 0) + 0.5
        fz = _int(raw.get('z'), 0, site['rows'] - 1, site['rows'] - 1) + 0.5
        dist = {'n': fz, 's': site['rows'] - fz, 'w': fx, 'e': site['cols'] - fx}
        side = min(('n', 's', 'w', 'e'), key=lambda k: dist[k])   # 화면(model.js snapGate)과 같은 순서
        along = fx if side in ('n', 's') else fz
        at = math.floor(along - width / 2 + 0.5)
    horizontal = side in ('n', 's')
    length = site['cols'] if horizontal else site['rows']
    width = min(width, length)
    at = max(0, min(length - width, at))
    mid = at + width // 2
    return {
        'id': make_id(raw.get('id'), 'g'),
        'kind': kind,
        'label': label or ('후문' if kind == 'back' else '교문'),
        'side': side,
        'at': at,
        'w': width,
        'x': mid if horizontal else (0 if side == 'w' else site['cols'] - 1),
        'z': (0 if side == 'n' else site['rows'] - 1) if horizontal else mid,
    }


def _rooms_overlap(a, b):
    return (
        a['floor'] == b['floor']
        and a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w']
        and a['z'] < b['z'] + b['d'] and b['z'] < a['z'] + a['d']
    )


def _normalize_building(raw, site, make_id, room_budget):
    if not isinstance(raw, dict):
        return None
    cols = _int(raw.get('cols'), 1, min(MAX_BUILDING_CELLS, site['cols']), 20)
    rows = _int(raw.get('rows'), 1, min(MAX_BUILDING_CELLS, site['rows']), 8)
    kind = _choice(raw.get('kind'), BUILDING_KINDS, 'main')
    base_floor = _int(raw.get('baseFloor'), 1, MAX_FLOORS, 1) if kind == 'connector' else 1
    floors = _int(raw.get('floors'), 1, MAX_FLOORS - base_floor + 1, 1)
    building = {
        'id': make_id(raw.get('id'), 'b'),
        'name': _text(raw.get('name'), 20) or '건물',
        'kind': kind,
        'x': _int(raw.get('x'), 0, site['cols'] - cols, 0),
        'z': _int(raw.get('z'), 0, site['rows'] - rows, 0),
        'cols': cols,
        'rows': rows,
        'floors': floors,
        'baseFloor': base_floor,
        'color': _color(raw.get('color')),
        'note': _multiline(raw.get('note'), 300),
        'floorInfo': {},
        'doors': [],
        'rooms': [],
    }
    # 건물출입문은 1층부터 시작하는 건물에만 둔다.
    if base_floor == 1:
        for item in (raw.get('doors') or [])[:MAX_DOORS_PER_BUILDING]:
            door = _normalize_door(item, building, make_id)
            if door:
                building['doors'].append(door)
    raw_info = raw.get('floorInfo') if isinstance(raw.get('floorInfo'), dict) else {}
    for floor in range(base_floor, base_floor + floors):
        info = _multiline(raw_info.get(str(floor)), 200)
        if info:
            building['floorInfo'][str(floor)] = info
    # 연결통로는 전 구간이 복도라 교실을 두지 않는다.
    if kind == 'connector':
        return building
    for item in raw.get('rooms') or []:
        if room_budget[0] <= 0:
            raise LayoutError(f'교실은 학교당 최대 {MAX_ROOMS}개까지 입력할 수 있습니다.')
        room = _normalize_room(item, building, make_id)
        if not room:
            continue
        clash = next((other for other in building['rooms'] if _rooms_overlap(room, other)), None)
        if clash:
            raise LayoutError(
                f"{building['name']} {room['floor']}층의 "
                f"'{room['name'] or room['no'] or '교실'}'과(와) "
                f"'{clash['name'] or clash['no'] or '교실'}'이(가) 겹쳐 있습니다."
            )
        building['rooms'].append(room)
        room_budget[0] -= 1
    return building


def normalize_layout(raw):
    """화면에서 받은 배치도를 저장 가능한 형태로 정리한다. 잘못되면 LayoutError."""
    if not isinstance(raw, dict):
        raise LayoutError('배치도 형식이 올바르지 않습니다.')
    make_id = _IdMaker()
    raw_site = raw.get('site') if isinstance(raw.get('site'), dict) else {}
    site = {
        'cols': _int(raw_site.get('cols'), 10, MAX_SITE_CELLS, 64),
        'rows': _int(raw_site.get('rows'), 10, MAX_SITE_CELLS, 44),
    }
    layout = {
        'version': LAYOUT_VERSION,
        'cell': _int(raw.get('cell'), 1, 10, 3),
        'site': site,
        'north': _int(raw.get('north'), 0, 359, 0),
        'gates': [],
        'landmarks': [],
        'buildings': [],
    }
    # 예전 화면이 적던 planShift(부지 도면 밑그림 이동량)는 버린다. 밑그림은 이제 건물에 붙여 그린다.

    raw_buildings = raw.get('buildings') or []
    if not isinstance(raw_buildings, list):
        raise LayoutError('건물 목록 형식이 올바르지 않습니다.')
    if len(raw_buildings) > MAX_BUILDINGS:
        raise LayoutError(f'건물은 최대 {MAX_BUILDINGS}개까지 입력할 수 있습니다.')
    room_budget = [MAX_ROOMS]
    for item in raw_buildings:
        building = _normalize_building(item, site, make_id, room_budget)
        if building:
            layout['buildings'].append(building)

    for item in (raw.get('landmarks') or [])[:MAX_LANDMARKS]:
        if not isinstance(item, dict):
            continue
        w = _int(item.get('w'), 1, site['cols'], 8)
        d = _int(item.get('d'), 1, site['rows'], 6)
        layout['landmarks'].append({
            'id': make_id(item.get('id'), 'l'),
            'type': _choice(item.get('type'), LANDMARK_TYPES, 'etc'),
            'label': _text(item.get('label'), 20),
            'x': _int(item.get('x'), 0, site['cols'] - w, 0),
            'z': _int(item.get('z'), 0, site['rows'] - d, 0),
            'w': w,
            'd': d,
        })

    for item in (raw.get('gates') or [])[:MAX_GATES]:
        gate = _normalize_gate(item, site, make_id)
        if gate:
            layout['gates'].append(gate)
    return layout


def layout_warnings(layout):
    """저장은 되지만 길찾기가 어려운 배치를 알려준다."""
    warnings = []
    if not layout['gates']:
        warnings.append('교문(출발 지점)이 없어 학교 밖에서 출발하는 길안내를 할 수 없습니다.')
    for building in layout['buildings']:
        if building['kind'] == 'connector':
            continue
        rooms = building['rooms']
        if building['baseFloor'] == 1 and not building['doors'] and not any(r['type'] == 'entrance' for r in rooms):
            warnings.append(f"{building['name']}: 건물출입문이 없어 1층 가장자리 복도 어디로든 들어가는 것으로 안내합니다.")
        if building['floors'] > 1 and not any(r['type'] in ('stairs', 'elevator') for r in rooms):
            warnings.append(f"{building['name']}: 계단·엘리베이터가 없어 위층으로 가는 길을 안내할 수 없습니다.")
    return warnings


# ---------------------------------------------------------------- 권한·조회
def can_view():
    return menu_is_allowed(VIEW_MENU)


def can_edit():
    return menu_is_allowed(VIEW_MENU) and menu_is_allowed(EDIT_MENU)


def _json_error(message, status=400):
    return jsonify({'status': 'error', 'message': message}), status


def _active_schools(conn):
    rows = conn.execute('''
        SELECT id, school_name, year
        FROM schools
        WHERE COALESCE(is_active, 1) = 1
        ORDER BY school_name, year DESC, id
    ''').fetchall()
    return [dict(row) for row in rows]


def _school(conn, school_id):
    row = conn.execute(
        'SELECT id, school_name, year FROM schools WHERE id=? AND COALESCE(is_active, 1) = 1',
        (school_id,),
    ).fetchone()
    return dict(row) if row else None


def _map_row(conn, school_id):
    return conn.execute(
        'SELECT * FROM classroom_guide_maps WHERE school_id=?', (school_id,)
    ).fetchone()


def _load_layout(row):
    if not row:
        return None
    try:
        layout = json.loads(row['layout_json'] or '{}')
    except (TypeError, ValueError):
        return None
    if not isinstance(layout, dict) or not layout.get('buildings'):
        return None
    return layout


def _public_link(token, origin=None):
    base = origin if origin is not None else resolve_public_origin()
    return f"{base}{url_for('classroom_guide.public_view', token=token)}"


def _share_info(row):
    if not row or not row['share_token']:
        return {'enabled': False, 'link': ''}
    return {
        'enabled': bool(row['share_enabled']),
        'link': _public_link(row['share_token']),
    }


def _selected_school_id(schools):
    raw = request.args.get('school', '')
    try:
        wanted = int(raw)
    except (TypeError, ValueError):
        wanted = None
    ids = [school['id'] for school in schools]
    if wanted in ids:
        return wanted
    return ids[0] if ids else None


def _current_user():
    return str(session.get('user_name') or session.get('emp_no') or '').strip()


# ---------------------------------------------------------------- 화면
@classroom_guide_bp.route('')
@classroom_guide_bp.route('/')
def index():
    if not can_view():
        abort(403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        schools = _active_schools(conn)
        school_id = _selected_school_id(schools)
        school = next((s for s in schools if s['id'] == school_id), None)
        row = _map_row(conn, school_id) if school_id else None
        layout = _load_layout(row)
        share = _share_info(row)
        updated = {
            'by': row['updated_by'] if row else '',
            'at': str(row['updated_at'] or '')[:16] if row else '',
        }
    finally:
        conn.close()
    return render_template(
        'classroom_guide/viewer.html',
        schools=schools,
        school=school,
        layout=layout,
        share=share,
        updated=updated,
        can_edit=can_edit(),
    )


@classroom_guide_bp.route('/edit')
def editor():
    if not can_edit():
        abort(403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        schools = _active_schools(conn)
        school_id = _selected_school_id(schools)
        if not school_id:
            return redirect(url_for('classroom_guide.index'))
        school = next(s for s in schools if s['id'] == school_id)
        layout = _load_layout(_map_row(conn, school_id))
        floorplan = _floorplan_info(conn, school_id)
    finally:
        conn.close()
    from . import openai_settings as ai_settings
    try:
        ai_status = ai_settings.public_ai_settings(ai_settings.get_ai_settings())
    except Exception:            # 키 복호화 실패 등으로 편집 화면이 막히지 않게 한다
        ai_status = {'status_text': 'AI API 설정 확인 필요', 'masked_key': ''}
    return render_template(
        'classroom_guide/editor.html',
        schools=schools,
        school=school,
        layout=layout,
        floorplan=floorplan,
        ai_status=ai_status,
    )


@classroom_guide_bp.route('/api/layout/<int:school_id>', methods=['GET'])
def get_layout(school_id):
    if not can_view():
        return _json_error('교실안내를 볼 권한이 없습니다.', 403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        if not _school(conn, school_id):
            return _json_error('학교를 찾을 수 없습니다.', 404)
        layout = _load_layout(_map_row(conn, school_id))
    finally:
        conn.close()
    return jsonify({'status': 'success', 'layout': layout})


@classroom_guide_bp.route('/api/layout/<int:school_id>', methods=['POST'])
def save_layout(school_id):
    if not can_edit():
        return _json_error('교실안내를 편집할 권한이 없습니다.', 403)
    if (request.content_length or 0) > MAX_LAYOUT_BYTES:
        return _json_error('배치도가 너무 큽니다. 건물이나 교실 수를 줄여 주세요.', 413)
    data = request.get_json(silent=True) or {}
    try:
        layout = normalize_layout(data.get('layout'))
    except LayoutError as exc:
        return _json_error(str(exc))
    if not layout['buildings']:
        return _json_error('건물을 1개 이상 입력해 주세요.')

    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        if not _school(conn, school_id):
            return _json_error('학교를 찾을 수 없습니다.', 404)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            INSERT INTO classroom_guide_maps (school_id, layout_json, updated_by, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(school_id) DO UPDATE SET
                layout_json=excluded.layout_json,
                updated_by=excluded.updated_by,
                updated_at=excluded.updated_at
        ''', (school_id, json.dumps(layout, ensure_ascii=False), _current_user(), now))
        conn.commit()
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'layout': layout,
        'warnings': layout_warnings(layout),
        'updated_at': now[:16],
    })


@classroom_guide_bp.route('/api/share/<int:school_id>', methods=['POST'])
def update_share(school_id):
    if not can_edit():
        return _json_error('공유 링크를 관리할 권한이 없습니다.', 403)
    action = str((request.get_json(silent=True) or {}).get('action') or '').strip()
    if action not in {'enable', 'disable', 'regenerate'}:
        return _json_error('알 수 없는 요청입니다.')
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        row = _map_row(conn, school_id)
        if not _school(conn, school_id) or not _load_layout(row):
            return _json_error('배치도를 먼저 저장해 주세요.', 404)
        token = row['share_token']
        if action == 'regenerate' or not token:
            token = secrets.token_urlsafe(12)
        enabled = 0 if action == 'disable' else 1
        conn.execute(
            'UPDATE classroom_guide_maps SET share_token=?, share_enabled=? WHERE school_id=?',
            (token, enabled, school_id),
        )
        conn.commit()
        row = _map_row(conn, school_id)
    finally:
        conn.close()
    return jsonify({'status': 'success', 'share': _share_info(row)})


@classroom_guide_bp.route('/api/share/<int:school_id>/qr.svg')
def share_qr(school_id):
    if not can_view():
        abort(403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        row = _map_row(conn, school_id)
    finally:
        conn.close()
    if not row or not row['share_token'] or not row['share_enabled']:
        abort(404)
    link = _public_link(row['share_token'])
    room_id = str(request.args.get('to') or '').strip()
    if _ID_RE.match(room_id):
        link = f'{link}?to={room_id}'
    buffer = io.BytesIO()
    segno.make(link, error='m').save(buffer, kind='svg', scale=6, border=2, dark='#111827')
    response = Response(buffer.getvalue(), mimetype='image/svg+xml')
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---------------------------------------------------------------- 평면도 자동 그리기
def _floorplan_rows(conn, school_id):
    return conn.execute(
        'SELECT * FROM classroom_guide_floorplans WHERE school_id=? ORDER BY position, id', (school_id,)
    ).fetchall()


def _floorplan_meta_row(conn, school_id):
    return conn.execute(
        'SELECT * FROM classroom_guide_floorplan_meta WHERE school_id=?', (school_id,)
    ).fetchone()


def _floorplan_meta(conn, school_id, row=None):
    """밑그림 위치 정보 {'site', 'floors'}. 없거나 깨졌으면 빈 dict."""
    row = row or _floorplan_meta_row(conn, school_id)
    try:
        meta = json.loads(row['meta_json'] or '{}') if row else {}
    except (TypeError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def _floorplan_info(conn, school_id):
    """편집 화면 밑그림용: 도면 이미지 주소와 부지·층별 위치. 도면이 없으면 None."""
    rows = _floorplan_rows(conn, school_id)
    meta_row = _floorplan_meta_row(conn, school_id)
    if not rows or not meta_row:
        return None
    meta = _floorplan_meta(conn, school_id, meta_row)
    return {
        'images': [{
            'id': row['id'],
            'name': row['original_name'],
            'width': row['width'],
            'height': row['height'],
            'url': url_for('classroom_guide.floorplan_image', school_id=school_id, image_id=row['id']),
        } for row in rows],
        'site': meta.get('site'),
        'floors': meta.get('floors') or [],
        'created_by': meta_row['created_by'] or '',
        'created_at': str(meta_row['created_at'] or '')[:16],
    }


def _delete_floorplans(conn, school_id, keep_ids=()):
    """학교의 예전 도면 파일·기록을 지운다(keep_ids 는 남긴다). 지운 파일 경로 목록을 돌려준다."""
    removed = []
    for row in _floorplan_rows(conn, school_id):
        if row['id'] in keep_ids:
            continue
        conn.execute('DELETE FROM classroom_guide_floorplans WHERE id=?', (row['id'],))
        removed.append(row['stored_path'])
    return removed


def _uploaded_floorplans():
    files = request.files.getlist('files')
    floors = request.form.getlist('floors')
    buildings = request.form.getlist('buildings')      # 파일마다 고른 건물 이름(빈 값 = 자동 인식)
    result = []
    for index, item in enumerate(files):
        if not item or not item.filename:
            continue
        data = item.read(floorplan_import.MAX_FILE_BYTES + 1)
        result.append({
            'name': item.filename,
            'data': data,
            'floor': floors[index] if index < len(floors) else 0,
            'building': _text(buildings[index], 20) if index < len(buildings) else '',
        })
    return result


@classroom_guide_bp.route('/api/floorplan/<int:school_id>/analyze', methods=['POST'])
def analyze_floorplan(school_id):
    """평면도를 AI로 읽어 배치도 초안을 돌려준다. 초안은 저장하지 않으며 편집 화면에서 확인 후 저장한다."""
    if not can_edit():
        return _json_error('교실안내를 편집할 권한이 없습니다.', 403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        if not _school(conn, school_id):
            return _json_error('학교를 찾을 수 없습니다.', 404)
    finally:
        conn.close()

    from . import openai_settings as ai_settings
    settings = ai_settings.get_ai_settings()
    if not settings.get('api_key'):
        return _json_error('통합관리 > AI api설정에 API 키를 먼저 등록해 주세요.')

    # mode=append: 지금 배치도는 두고 건물만 더한다(target 이 있으면 그 건물의 교실을 채운다).
    append = request.form.get('mode') == 'append'
    target = str(request.form.get('target') or '').strip()
    target = target if append and _ID_RE.match(target) else ''
    target_name = _text(request.form.get('target_name'), 20) if target else ''
    try:
        existing = json.loads(request.form.get('existing') or '[]')
    except ValueError:
        existing = []
    existing_names = [_text(name, 20) for name in existing if isinstance(name, str)][:MAX_BUILDINGS] \
        if isinstance(existing, list) else []

    try:
        pages = floorplan_import.prepare_pages(_uploaded_floorplans())
    except floorplan_import.FloorplanError as exc:
        return _json_error(str(exc))
    if target_name:
        for page in pages:
            page['building'] = target_name

    provider = settings.get('provider')
    analyzer = floorplan_import.analyze_with_claude if provider == 'claude' else floorplan_import.analyze_with_openai
    try:
        result, usage = analyzer(
            str(settings['api_key']), str(settings['model']), pages,
            safety_value=str(session.get('emp_no') or school_id),
        )
    except floorplan_import.FloorplanError as exc:
        return _json_error(str(exc), 502)
    except Exception as exc:          # AI 제공사 오류(인증·한도·시간초과 등)는 원문 대신 안내 문구로
        name = exc.__class__.__name__
        if 'Authentication' in name or 'Permission' in name:
            message = 'AI API 키 인증에 실패했습니다. 통합관리 > AI api설정을 확인해 주세요.'
        elif 'RateLimit' in name:
            message = 'AI 사용 한도에 걸렸습니다. 잠시 뒤 다시 시도해 주세요.'
        elif 'Timeout' in name:
            message = 'AI 분석 시간이 초과되었습니다. 도면을 층별로 나눠서 올려 주세요.'
        else:
            message = f'AI 평면도 분석 중 오류가 발생했습니다. ({name})'
        return _json_error(message, 502)

    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        conn.execute('''
            INSERT INTO classroom_guide_ai_history (school_id, emp_no, model, input_tokens, output_tokens, total_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (school_id, str(session.get('emp_no') or ''), str(settings['model']),
              usage['input_tokens'], usage['output_tokens'], usage['total_tokens']))
        conn.commit()
    finally:
        conn.close()

    try:
        draft = floorplan_import.build_layout(result, pages)
        if append:
            floorplan_import.pick_for_append(draft, target, target_name, existing_names)
        layout = normalize_layout(draft['layout'])
    except (floorplan_import.FloorplanError, LayoutError) as exc:
        return _json_error(str(exc), 422)

    # 밑그림용 도면을 암호화해 보관한다. 전체를 새로 그릴 때는 이 학교의 이전 도면을 새 도면이 모두
    # 저장된 뒤에 지우고, 건물만 더할 때는 이전 도면에 새 도면을 보탠다(같은 건물을 다시 그리면 그 건물 도면만 바뀜).
    written = []
    try:
        for page in pages:
            path = FLOORPLAN_ROOT / str(school_id) / encrypted_storage_name('plan.jpg')
            encrypt_bytes(floorplan_import.encode_jpeg(page['image']), path)
            written.append((page, str(path)))
    except OSError:
        for _, path in written:
            delete_file(path)
        return _json_error('도면 파일을 저장하지 못했습니다. 저장 공간을 확인해 주세요.', 500)

    conn = get_db()
    removed = []
    try:
        ensure_classroom_guide_schema(conn)
        old_meta = _floorplan_meta(conn, school_id) if append else {}
        start = conn.execute(
            'SELECT COALESCE(MAX(position), -1) + 1 FROM classroom_guide_floorplans WHERE school_id=?', (school_id,)
        ).fetchone()[0] if append else 0
        ids = []
        for position, (page, path) in enumerate(written, start):
            width, height = page['image'].size
            cursor = conn.execute('''
                INSERT INTO classroom_guide_floorplans (school_id, position, original_name, stored_path, width, height)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (school_id, position, page['name'], path, width, height))
            ids.append(cursor.lastrowid)
        # 밑그림 위치 정보의 도면 순번(page)을 저장된 도면 id(image)로 바꾼다.
        def with_image(item):
            out = {k: v for k, v in item.items() if k != 'page'}
            out['image'] = ids[item['page']]
            return out

        underlay = draft['underlay']
        site = with_image(underlay['site']) if underlay.get('site') else None
        floors = [with_image(item) for item in underlay['floors']]
        keep = set(ids)
        if append:
            # 다시 그린 건물의 예전 층 도면 자리는 새것으로 바꾸고, 부지 도면은 있던 것을 그대로 쓴다.
            redrawn = {b['id'] for b in layout['buildings']}
            floors = [f for f in old_meta.get('floors') or [] if f.get('building') not in redrawn] + floors
            images = {f['image'] for f in floors}
            old_site = old_meta.get('site')
            if old_site and old_site.get('image') in images:
                site = old_site
            keep = images | ({site['image']} if site else set())
        removed = _delete_floorplans(conn, school_id, keep_ids=keep)
        conn.execute('''
            INSERT INTO classroom_guide_floorplan_meta (school_id, meta_json, created_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(school_id) DO UPDATE SET
                meta_json=excluded.meta_json, created_by=excluded.created_by, created_at=excluded.created_at
        ''', (school_id, json.dumps({'site': site, 'floors': floors}, ensure_ascii=False), _current_user(),
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        floorplan = _floorplan_info(conn, school_id)
    except Exception:
        conn.rollback()
        for _, path in written:
            delete_file(path)
        raise
    finally:
        conn.close()
    for path in removed:
        delete_file(path)

    return jsonify({
        'status': 'success',
        'layout': layout,
        'floorplan': floorplan,
        'warnings': draft['warnings'] + layout_warnings(layout),
        'stats': draft['stats'],
        'usage': usage,
    })


@classroom_guide_bp.route('/api/floorplan/<int:school_id>/image/<int:image_id>')
def floorplan_image(school_id, image_id):
    if not can_edit():
        abort(403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        row = conn.execute(
            'SELECT * FROM classroom_guide_floorplans WHERE id=? AND school_id=?', (image_id, school_id)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    try:
        return encrypted_response(row['stored_path'], f'floorplan-{image_id}.jpg',
                                  as_attachment=False, mimetype='image/jpeg')
    except FileNotFoundError:
        abort(404)


@classroom_guide_bp.route('/api/floorplan/<int:school_id>', methods=['DELETE'])
def delete_floorplan(school_id):
    if not can_edit():
        return _json_error('교실안내를 편집할 권한이 없습니다.', 403)
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        removed = _delete_floorplans(conn, school_id)
        conn.execute('DELETE FROM classroom_guide_floorplan_meta WHERE school_id=?', (school_id,))
        conn.commit()
    finally:
        conn.close()
    for path in removed:
        delete_file(path)
    return jsonify({'status': 'success'})


@classroom_guide_bp.route('/p/<token>')
def public_view(token):
    """학부모·수강생용 공개 안내도. 로그인 없이 공유 링크로만 연다."""
    conn = get_db()
    try:
        ensure_classroom_guide_schema(conn)
        row = conn.execute('''
            SELECT m.*, s.school_name
            FROM classroom_guide_maps m
            JOIN schools s ON s.id = m.school_id
            WHERE m.share_token = ? AND m.share_enabled = 1
              AND COALESCE(s.is_active, 1) = 1
        ''', (str(token or '')[:64],)).fetchone()
        layout = _load_layout(row)
    finally:
        conn.close()
    if not layout:
        return render_template('classroom_guide/public.html', layout=None, school_name=''), 404
    response = Response(render_template(
        'classroom_guide/public.html',
        layout=layout,
        school_name=row['school_name'],
    ))
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response
