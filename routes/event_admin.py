"""이벤트관리 — 센터장 업무공간 이벤트 탭의 생성·수정·삭제·추첨.

이벤트 정보와 응모(추첨번호 발급) 내역, 추첨 결과를 모두 DB에 기록해
회차가 바뀌어도 지난 이벤트 기록이 그대로 남게 한다.
"""

import secrets
import sqlite3
from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request, session

from .database import get_db
from .security import menu_permission_required

event_bp = Blueprint('event_admin', __name__)

EVENT_MENU_KEY = 'event_admin'

DEFAULT_EVENT = {
    'title': '센터장 회의 상품 이벤트!',
    'description': '추첨번호를 핸드폰으로 찍어오시면\n상품추첨에 응모하실 수 있습니다!',
    'number_min': 1,
    'number_max': 99,
}

DEFAULT_PRIZES = (
    (1, '1등', 1),
    (2, '2등', 2),
    (3, '3등', 3),
)


# ---------------------------------------------------------------- 스키마

def ensure_event_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS center_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            title TEXT NOT NULL,
            description TEXT,
            start_date TEXT,
            end_date TEXT,
            number_min INTEGER DEFAULT 1,
            number_max INTEGER DEFAULT 99,
            is_active INTEGER DEFAULT 1,
            created_by TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(center_events)').fetchall()}
    for name, ddl in (
        ('start_date', 'ALTER TABLE center_events ADD COLUMN start_date TEXT'),
        ('end_date', 'ALTER TABLE center_events ADD COLUMN end_date TEXT'),
        ('created_by', 'ALTER TABLE center_events ADD COLUMN created_by TEXT'),
    ):
        if name not in columns:
            conn.execute(ddl)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS center_event_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            emp_no TEXT,
            user_name TEXT,
            department TEXT,
            school_id INTEGER,
            draw_number INTEGER NOT NULL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(event_id, draw_number),
            UNIQUE(event_id, emp_no)
        )
    """)
    entry_columns = {
        row[1] for row in conn.execute('PRAGMA table_info(center_event_entries)').fetchall()
    }
    if 'department' not in entry_columns:
        conn.execute('ALTER TABLE center_event_entries ADD COLUMN department TEXT')

    # 등수별 당첨 인원 설정
    conn.execute("""
        CREATE TABLE IF NOT EXISTS center_event_prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            rank_no INTEGER NOT NULL,
            rank_label TEXT,
            winner_count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(event_id, rank_no)
        )
    """)
    # 추첨 결과
    conn.execute("""
        CREATE TABLE IF NOT EXISTS center_event_winners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            entry_id INTEGER,
            rank_no INTEGER NOT NULL,
            rank_label TEXT,
            user_name TEXT,
            department TEXT,
            draw_number INTEGER,
            drawn_by TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()


# ---------------------------------------------------------------- 공통 조회

def _today():
    return date.today().isoformat()


def event_is_running(event, today=None):
    """기간이 지정된 경우 오늘이 기간 안에 있어야 진행중으로 본다."""
    if not event or not int(event.get('is_active') or 0):
        return False
    today = today or _today()
    start = str(event.get('start_date') or '').strip()
    end = str(event.get('end_date') or '').strip()
    if start and today < start:
        return False
    if end and today > end:
        return False
    return True


def event_status(event, today=None):
    if not int(event.get('is_active') or 0):
        return 'paused'
    today = today or _today()
    start = str(event.get('start_date') or '').strip()
    end = str(event.get('end_date') or '').strip()
    if start and today < start:
        return 'upcoming'
    if end and today > end:
        return 'ended'
    return 'running'


DATE_MIN = '0000-01-01'
DATE_MAX = '9999-12-31'


def _period_bounds(start_date, end_date):
    """비어 있는 날짜는 '제한 없음'이므로 무한대로 본다."""
    start = str(start_date or '').strip() or DATE_MIN
    end = str(end_date or '').strip() or DATE_MAX
    return start, end


def find_overlapping_event(conn, start_date, end_date, exclude_id=None):
    """진행중(중지하지 않은) 이벤트와 기간이 겹치면 그 이벤트를 돌려준다."""
    new_start, new_end = _period_bounds(start_date, end_date)
    rows = conn.execute(
        'SELECT * FROM center_events WHERE is_active = 1 ORDER BY id'
    ).fetchall()
    for row in rows:
        event = dict(row)
        if exclude_id and int(event['id']) == int(exclude_id):
            continue
        old_start, old_end = _period_bounds(event['start_date'], event['end_date'])
        if new_start <= old_end and old_start <= new_end:
            return event
    return None


def _period_label(event):
    start = str(event.get('start_date') or '').strip()
    end = str(event.get('end_date') or '').strip()
    if not start and not end:
        return '기간 제한 없음'
    return (start or '제한 없음') + ' ~ ' + (end or '제한 없음')


def get_running_event(conn):
    """센터장 업무공간 탭에 노출할 진행중 이벤트 1건."""
    ensure_event_schema(conn)
    rows = conn.execute("""
        SELECT * FROM center_events
        WHERE is_active = 1
        ORDER BY id DESC
    """).fetchall()
    today = _today()
    for row in rows:
        event = dict(row)
        if event_is_running(event, today):
            return event
    return None


def build_center_event_context(conn, school_id=None):
    """센터장 업무공간 템플릿에 넘길 이벤트 정보(응모 전에는 번호 없음)."""
    event = get_running_event(conn)
    if not event:
        return None
    owner_key = str(session.get('emp_no') or session.get('user_name') or '').strip()
    entry = None
    if owner_key:
        row = conn.execute(
            'SELECT * FROM center_event_entries WHERE event_id = ? AND emp_no = ?',
            (event['id'], owner_key),
        ).fetchone()
        entry = dict(row) if row else None
    return {
        'id': event['id'],
        'title': event['title'],
        'description': event['description'] or '',
        'start_date': event['start_date'] or '',
        'end_date': event['end_date'] or '',
        'draw_number': entry['draw_number'] if entry else None,
        'issued_at': entry['created_at'] if entry else None,
    }


# ---------------------------------------------------------------- 번호 발급

def _pick_number(conn, event):
    used = {
        int(r['draw_number'])
        for r in conn.execute(
            'SELECT draw_number FROM center_event_entries WHERE event_id = ?',
            (event['id'],),
        ).fetchall()
    }
    low = int(event.get('number_min') or 1)
    high = int(event.get('number_max') or 99)
    if high < low:
        high = low
    candidates = [n for n in range(low, high + 1) if n not in used]
    if not candidates:
        # 번호가 모두 소진되면 구간을 넓혀 계속 발급한다.
        candidates = [n for n in range(high + 1, high + 101) if n not in used]
    return secrets.choice(candidates) if candidates else None


def issue_event_number(conn, event, emp_no, user_name, department=None, school_id=None):
    """응모 버튼을 누른 사람에게 겹치지 않는 추첨번호를 발급한다."""
    owner_key = str(emp_no or user_name or '').strip()
    if not owner_key:
        return None, '로그인 정보를 확인할 수 없습니다.'

    existing = conn.execute(
        'SELECT * FROM center_event_entries WHERE event_id = ? AND emp_no = ?',
        (event['id'], owner_key),
    ).fetchone()
    if existing:
        return dict(existing), None

    for _ in range(12):
        number = _pick_number(conn, event)
        if number is None:
            return None, '발급할 수 있는 추첨번호가 모두 소진되었습니다.'
        try:
            cur = conn.execute(
                """
                INSERT INTO center_event_entries
                    (event_id, emp_no, user_name, department, school_id, draw_number)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event['id'], owner_key, user_name, department, school_id, number),
            )
            conn.commit()
            row = conn.execute(
                'SELECT * FROM center_event_entries WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
            return dict(row), None
        except sqlite3.IntegrityError:
            # 같은 순간에 다른 사람이 같은 번호를 가져간 경우 다시 뽑는다.
            conn.rollback()
            existing = conn.execute(
                'SELECT * FROM center_event_entries WHERE event_id = ? AND emp_no = ?',
                (event['id'], owner_key),
            ).fetchone()
            if existing:
                return dict(existing), None
    return None, '추첨번호 발급에 실패했습니다. 잠시 후 다시 시도해 주세요.'


# ---------------------------------------------------------------- 관리 화면

def _load_prizes(conn, event_id):
    rows = conn.execute(
        'SELECT rank_no, rank_label, winner_count FROM center_event_prizes '
        'WHERE event_id = ? ORDER BY rank_no',
        (event_id,),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]
    return [
        {'rank_no': no, 'rank_label': label, 'winner_count': count}
        for no, label, count in DEFAULT_PRIZES
    ]


def _load_entries(conn, event_id):
    rows = conn.execute(
        'SELECT id, user_name, department, draw_number, created_at '
        'FROM center_event_entries WHERE event_id = ? ORDER BY id',
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_winners(conn, event_id):
    rows = conn.execute(
        'SELECT entry_id, rank_no, rank_label, user_name, department, draw_number, created_at '
        'FROM center_event_winners WHERE event_id = ? ORDER BY rank_no, id',
        (event_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _event_payload(conn, row):
    event = dict(row)
    event['status'] = event_status(event)
    event['entry_count'] = conn.execute(
        'SELECT COUNT(*) AS c FROM center_event_entries WHERE event_id = ?',
        (event['id'],),
    ).fetchone()['c']
    event['winner_count'] = conn.execute(
        'SELECT COUNT(*) AS c FROM center_event_winners WHERE event_id = ?',
        (event['id'],),
    ).fetchone()['c']
    return event


@event_bp.route('/')
@menu_permission_required(EVENT_MENU_KEY)
def event_admin_page():
    conn = get_db()
    try:
        ensure_event_schema(conn)
        rows = conn.execute('SELECT * FROM center_events ORDER BY id DESC').fetchall()
        events = [_event_payload(conn, row) for row in rows]
        return render_template(
            'event_admin.html',
            events=events,
            default_event=DEFAULT_EVENT,
            today=_today(),
        )
    finally:
        conn.close()


@event_bp.route('/<int:event_id>')
@menu_permission_required(EVENT_MENU_KEY)
def event_detail(event_id):
    conn = get_db()
    try:
        ensure_event_schema(conn)
        row = conn.execute('SELECT * FROM center_events WHERE id = ?', (event_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '이벤트를 찾을 수 없습니다.'}), 404
        return jsonify({
            'status': 'success',
            'event': _event_payload(conn, row),
            'prizes': _load_prizes(conn, event_id),
            'entries': _load_entries(conn, event_id),
            'winners': _load_winners(conn, event_id),
        })
    finally:
        conn.close()


def _form_value(data, key, default=''):
    return str(data.get(key, default) or '').strip()


def _valid_date(value):
    if not value:
        return ''
    try:
        return datetime.strptime(value[:10], '%Y-%m-%d').date().isoformat()
    except ValueError:
        return None


@event_bp.route('/save', methods=['POST'])
@menu_permission_required(EVENT_MENU_KEY)
def save_event():
    data = request.get_json(silent=True) or {}
    title = _form_value(data, 'title')
    if not title:
        return jsonify({'status': 'error', 'message': '이벤트 제목을 입력해 주세요.'}), 400

    start_date = _valid_date(_form_value(data, 'start_date'))
    end_date = _valid_date(_form_value(data, 'end_date'))
    if start_date is None or end_date is None:
        return jsonify({'status': 'error', 'message': '날짜 형식을 확인해 주세요.'}), 400
    if start_date and end_date and end_date < start_date:
        return jsonify({'status': 'error', 'message': '종료일이 시작일보다 빠릅니다.'}), 400

    try:
        number_min = int(data.get('number_min') or 1)
        number_max = int(data.get('number_max') or 99)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '추첨번호 범위를 숫자로 입력해 주세요.'}), 400
    if number_min < 0 or number_max < number_min:
        return jsonify({'status': 'error', 'message': '추첨번호 범위를 확인해 주세요.'}), 400

    description = str(data.get('description') or '').strip()
    is_active = 1 if data.get('is_active', True) else 0
    event_id = data.get('id')

    conn = get_db()
    try:
        ensure_event_schema(conn)

        # 진행중인 이벤트와 기간이 겹치면 만들 수 없다.
        # (겹친 기간에 두 이벤트가 동시에 뜨면 어느 쪽에 응모한 건지 알 수 없다.)
        if is_active:
            conflict = find_overlapping_event(conn, start_date, end_date, exclude_id=event_id)
            if conflict:
                return jsonify({
                    'status': 'error',
                    'message': ('같은 기간에 진행중인 이벤트가 있습니다.\n\n'
                                '[' + str(conflict['title']) + ']\n'
                                + _period_label(conflict) + '\n\n'
                                '먼저 위 이벤트를 종료한 뒤에 만들어 주세요.'),
                    'conflict_id': conflict['id'],
                }), 409

        if event_id:
            exists = conn.execute(
                'SELECT id FROM center_events WHERE id = ?', (event_id,)
            ).fetchone()
            if not exists:
                return jsonify({'status': 'error', 'message': '이벤트를 찾을 수 없습니다.'}), 404
            conn.execute(
                """
                UPDATE center_events
                SET title = ?, description = ?, start_date = ?, end_date = ?,
                    number_min = ?, number_max = ?, is_active = ?
                WHERE id = ?
                """,
                (title, description, start_date, end_date,
                 number_min, number_max, is_active, event_id),
            )
            message = '이벤트를 수정했습니다.'
        else:
            cur = conn.execute(
                """
                INSERT INTO center_events
                    (title, description, start_date, end_date, number_min, number_max,
                     is_active, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (title, description, start_date, end_date, number_min, number_max,
                 is_active, session.get('user_name')),
            )
            event_id = cur.lastrowid
            message = '이벤트를 만들었습니다.'
        conn.commit()
        return jsonify({'status': 'success', 'message': message, 'id': event_id})
    finally:
        conn.close()


@event_bp.route('/<int:event_id>/close', methods=['POST'])
@menu_permission_required(EVENT_MENU_KEY)
def close_event(event_id):
    """기간이 남아 있어도 이벤트를 지금 종료한다(응모·추첨 기록은 남는다)."""
    conn = get_db()
    try:
        ensure_event_schema(conn)
        row = conn.execute('SELECT * FROM center_events WHERE id = ?', (event_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '이벤트를 찾을 수 없습니다.'}), 404

        today = _today()
        end_date = str(row['end_date'] or '').strip()
        # 종료일이 비었거나 아직 남아 있으면 오늘로 당긴다.
        if not end_date or end_date > today:
            end_date = today
        conn.execute(
            'UPDATE center_events SET is_active = 0, end_date = ? WHERE id = ?',
            (end_date, event_id),
        )
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': '이벤트를 종료했습니다. 센터장 화면에서 이벤트 탭이 사라집니다.',
        })
    finally:
        conn.close()


@event_bp.route('/<int:event_id>/delete', methods=['POST'])
@menu_permission_required(EVENT_MENU_KEY)
def delete_event(event_id):
    conn = get_db()
    try:
        ensure_event_schema(conn)
        row = conn.execute('SELECT id FROM center_events WHERE id = ?', (event_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '이벤트를 찾을 수 없습니다.'}), 404
        conn.execute('DELETE FROM center_event_winners WHERE event_id = ?', (event_id,))
        conn.execute('DELETE FROM center_event_prizes WHERE event_id = ?', (event_id,))
        conn.execute('DELETE FROM center_event_entries WHERE event_id = ?', (event_id,))
        conn.execute('DELETE FROM center_events WHERE id = ?', (event_id,))
        conn.commit()
        return jsonify({'status': 'success', 'message': '이벤트와 응모 기록을 삭제했습니다.'})
    finally:
        conn.close()


@event_bp.route('/<int:event_id>/draw', methods=['POST'])
@menu_permission_required(EVENT_MENU_KEY)
def draw_winners(event_id):
    """등수별 인원수만큼 응모자 중에서 무작위로 당첨자를 뽑는다."""
    data = request.get_json(silent=True) or {}
    raw_prizes = data.get('prizes') or []

    prizes = []
    for index, item in enumerate(raw_prizes, start=1):
        try:
            rank_no = int(item.get('rank_no') or index)
            winner_count = int(item.get('winner_count') or 0)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': '등수와 인원수를 숫자로 입력해 주세요.'}), 400
        if winner_count < 0:
            return jsonify({'status': 'error', 'message': '인원수는 0명 이상이어야 합니다.'}), 400
        label = str(item.get('rank_label') or f'{rank_no}등').strip()
        prizes.append({'rank_no': rank_no, 'rank_label': label, 'winner_count': winner_count})

    if not prizes:
        return jsonify({'status': 'error', 'message': '추첨할 등수를 한 개 이상 지정해 주세요.'}), 400
    if sum(p['winner_count'] for p in prizes) <= 0:
        return jsonify({'status': 'error', 'message': '당첨 인원수를 1명 이상 지정해 주세요.'}), 400

    conn = get_db()
    try:
        ensure_event_schema(conn)
        event = conn.execute('SELECT * FROM center_events WHERE id = ?', (event_id,)).fetchone()
        if not event:
            return jsonify({'status': 'error', 'message': '이벤트를 찾을 수 없습니다.'}), 404

        pool = _load_entries(conn, event_id)
        if not pool:
            return jsonify({'status': 'error', 'message': '아직 응모한 사람이 없습니다.'}), 400
        total_needed = sum(p['winner_count'] for p in prizes)
        if total_needed > len(pool):
            return jsonify({
                'status': 'error',
                'message': f'응모자는 {len(pool)}명인데 당첨 인원이 {total_needed}명입니다.',
            }), 400

        # 등수 설정 저장(다음 추첨에서도 같은 구성을 그대로 쓴다)
        conn.execute('DELETE FROM center_event_prizes WHERE event_id = ?', (event_id,))
        for prize in prizes:
            conn.execute(
                'INSERT INTO center_event_prizes (event_id, rank_no, rank_label, winner_count) '
                'VALUES (?, ?, ?, ?)',
                (event_id, prize['rank_no'], prize['rank_label'], prize['winner_count']),
            )

        # 다시 추첨하면 이전 결과는 지우고 새로 뽑는다.
        conn.execute('DELETE FROM center_event_winners WHERE event_id = ?', (event_id,))

        remaining = list(pool)
        winners = []
        for prize in sorted(prizes, key=lambda p: p['rank_no']):
            for _ in range(prize['winner_count']):
                if not remaining:
                    break
                picked = remaining.pop(secrets.randbelow(len(remaining)))
                conn.execute(
                    """
                    INSERT INTO center_event_winners
                        (event_id, entry_id, rank_no, rank_label, user_name, department,
                         draw_number, drawn_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, picked['id'], prize['rank_no'], prize['rank_label'],
                     picked['user_name'], picked['department'], picked['draw_number'],
                     session.get('user_name')),
                )
                winners.append({
                    'rank_no': prize['rank_no'],
                    'rank_label': prize['rank_label'],
                    'user_name': picked['user_name'],
                    'department': picked['department'],
                    'draw_number': picked['draw_number'],
                })
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': f'{len(winners)}명의 당첨자를 뽑았습니다.',
            'winners': winners,
            'pool': [
                {'draw_number': e['draw_number'], 'user_name': e['user_name']}
                for e in pool
            ],
        })
    finally:
        conn.close()


@event_bp.route('/<int:event_id>/winners/reset', methods=['POST'])
@menu_permission_required(EVENT_MENU_KEY)
def reset_winners(event_id):
    conn = get_db()
    try:
        ensure_event_schema(conn)
        conn.execute('DELETE FROM center_event_winners WHERE event_id = ?', (event_id,))
        conn.commit()
        return jsonify({'status': 'success', 'message': '추첨 결과를 지웠습니다.'})
    finally:
        conn.close()
