"""학교지원 > 강사 인성·직무적합성 검사.

관리자가 검사 링크를 만들어 응시자에게 보내면, 링크를 연 사람이 성명·생년월일·연락처·이메일을
입력하고 40문항을 풀어 제출한다. 제출하면 개인정보와 답안지, 채점한 결과지가 저장되고
관리자가 목록·상세 화면에서 다시 볼 수 있다. 응시자는 인트라넷 계정 없이 링크(토큰)만으로 참여한다.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request, session, url_for

from services import personality_test_v1 as legacy_pt
from services import personality_test_v2 as pt

from .database import get_db
from .solapi_settings import format_phone, normalize_phone, resolve_public_origin

personality_bp = Blueprint('personality', __name__)

KST = timezone(timedelta(hours=9))
BRAND_LOGO_FILE = 'logo01.gif'
BRAND_NAME = '사단법인 새담청소년교육문화원'
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
MAX_LINKS = 200
MAX_NAME_LENGTH = 40


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------
def init_personality_schema(conn=None):
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS personality_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                memo TEXT,
                token TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_by_name TEXT,
                created_at TEXT,
                test_version TEXT NOT NULL DEFAULT 'v1'
            );

            CREATE TABLE IF NOT EXISTS personality_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                birth_date TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                consent_at TEXT,
                answers TEXT NOT NULL,
                answer_sheet TEXT,
                scores TEXT,
                report TEXT,
                elapsed_sec INTEGER,
                test_version TEXT,
                submitted_at TEXT,
                FOREIGN KEY (link_id) REFERENCES personality_links(id)
            );
            CREATE INDEX IF NOT EXISTS idx_personality_results_link
                ON personality_results(link_id, submitted_at);
        ''')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(personality_links)')}
        if 'test_version' not in columns:
            conn.execute("ALTER TABLE personality_links ADD COLUMN test_version TEXT NOT NULL DEFAULT 'v1'")
        result_columns = {row['name'] for row in conn.execute('PRAGMA table_info(personality_results)')}
        if 'answer_sheet' not in result_columns:
            conn.execute('ALTER TABLE personality_results ADD COLUMN answer_sheet TEXT')
        if 'report' not in result_columns:
            conn.execute('ALTER TABLE personality_results ADD COLUMN report TEXT')
        conn.commit()
    finally:
        if owns_connection:
            conn.close()


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def _now():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def _json_error(message, code=400):
    return jsonify({'status': 'error', 'message': message}), code


def _brand_context(token=''):
    origin = resolve_public_origin()
    logo = url_for('static', filename=BRAND_LOGO_FILE)
    return {
        'brand_name': BRAND_NAME,
        'logo_url': logo,
        'logo_absolute_url': f'{origin}{logo}',
        'page_url': f"{origin}{url_for('personality.take', token=token)}" if token else origin,
    }


def _public_link(token, origin=None):
    base = origin if origin is not None else resolve_public_origin()
    return f"{base}{url_for('personality.take', token=token)}"


def _load_json(raw, default):
    try:
        value = json.loads(raw or '')
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _answers_of(row):
    """저장된 답안(JSON)을 {문항번호(int): 값(int)}로 되돌린다."""
    stored = _load_json(row['answers'], {})
    result = {}
    for key, value in stored.items():
        try:
            result[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _link_dict(row, origin, result_count):
    return {
        'id': row['id'],
        'title': row['title'],
        'memo': row['memo'] or '',
        'is_active': bool(row['is_active']),
        'link': _public_link(row['token'], origin),
        'result_count': result_count,
        'test_version': row['test_version'],
        'created_by_name': row['created_by_name'] or '',
        'created_at': row['created_at'] or '',
    }


def _result_summary(row):
    """목록 한 줄에 쓸 요약(개인정보 + 영역 점수 + 신뢰도)."""
    scores = _load_json(row['scores'], {})
    bank = _bank_for_version(row['test_version'])
    percents = scores.get('percents', {})
    return {
        'id': row['id'],
        'link_id': row['link_id'],
        'link_title': row['link_title'] if 'link_title' in row.keys() else '',
        'name': row['name'],
        'birth_date': row['birth_date'],
        'phone': format_phone(row['phone']) or row['phone'],
        'email': row['email'],
        'submitted_at': row['submitted_at'] or '',
        'scores': percents,
        'total': scores.get('total'),
        'scales': [{'key': key, 'name': bank.SCALES[key][0], 'percent': percents.get(key)}
                   for key in bank.SCALE_ORDER] if bank else [],
        'test_version': row['test_version'],
        'reliable': bool(scores.get('reliable', True)),
    }


def _bank_for_version(version):
    return {legacy_pt.TEST_VERSION: legacy_pt, pt.TEST_VERSION: pt}.get(version)


def _clean_birth(value):
    text = str(value or '').strip()
    try:
        parsed = datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('생년월일을 올바르게 입력해 주세요.')
    if parsed > date.today() or parsed.year < 1900:
        raise ValueError('생년월일을 올바르게 입력해 주세요.')
    return parsed.strftime('%Y-%m-%d')


def _clean_applicant(payload):
    """응시자가 입력한 개인정보를 검사해 정리한다. 문제가 있으면 ValueError(안내 문구)."""
    name = re.sub(r'\s+', ' ', str(payload.get('name') or '')).strip()
    if not name:
        raise ValueError('성명을 입력해 주세요.')
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError('성명이 너무 깁니다.')
    birth = _clean_birth(payload.get('birth_date'))
    phone = normalize_phone(payload.get('phone'), required=True)
    email = str(payload.get('email') or '').strip()
    if not EMAIL_PATTERN.match(email) or len(email) > 120:
        raise ValueError('이메일 주소를 올바르게 입력해 주세요.')
    return {'name': name, 'birth_date': birth, 'phone': phone, 'email': email}


# ---------------------------------------------------------------------------
# 관리자 화면
# ---------------------------------------------------------------------------
@personality_bp.route('/')
def index():
    return render_template('personality/index.html', question_count=pt.QUESTION_COUNT)


@personality_bp.route('/result/<int:result_id>')
def result_page(result_id):
    conn = get_db()
    try:
        row = conn.execute(
            '''SELECT r.*, l.title AS link_title
                 FROM personality_results r
                 JOIN personality_links l ON l.id = r.link_id
                WHERE r.id=?''',
            (result_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return render_template('personality/result.html', result=None), 404

    answers = _answers_of(row)
    bank = _bank_for_version(row['test_version'])
    complete = bool(bank and len(answers) == bank.QUESTION_COUNT)
    report = (_load_json(row['report'], {}) or bank.build_report(answers, row['elapsed_sec'])) if complete else None
    sheet = (_load_json(row['answer_sheet'], []) or bank.answer_sheet(answers)) if complete else []
    return render_template(
        'personality/result.html',
        result={
            'id': row['id'],
            'link_title': row['link_title'],
            'name': row['name'],
            'birth_date': row['birth_date'],
            'phone': format_phone(row['phone']) or row['phone'],
            'email': row['email'],
            'consent_at': row['consent_at'] or '',
            'submitted_at': row['submitted_at'] or '',
            'elapsed_sec': row['elapsed_sec'],
            'test_version': row['test_version'] or '',
        },
        report=report,
        sheet=sheet,
        likert=bank.LIKERT_LABELS if bank else {},
    )


@personality_bp.route('/api/links')
def api_links():
    origin = resolve_public_origin()
    conn = get_db()
    try:
        rows = conn.execute(
            '''SELECT l.*, (SELECT COUNT(*) FROM personality_results r WHERE r.link_id=l.id) AS cnt
                 FROM personality_links l ORDER BY l.id DESC'''
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'links': [_link_dict(row, origin, row['cnt']) for row in rows],
    })


@personality_bp.route('/api/links', methods=['POST'])
def api_link_create():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get('title') or '').strip()
    memo = str(payload.get('memo') or '').strip()[:300]
    if not title:
        return _json_error('검사 이름을 입력해 주세요. 예) 2026 하반기 강사 채용')
    if len(title) > 80:
        return _json_error('검사 이름은 80자 이내로 입력해 주세요.')
    conn = get_db()
    try:
        total = conn.execute('SELECT COUNT(*) FROM personality_links').fetchone()[0]
        if total >= MAX_LINKS:
            return _json_error('검사 링크가 너무 많습니다. 쓰지 않는 링크는 삭제해 주세요.')
        token = secrets.token_urlsafe(24)
        cursor = conn.execute(
            '''INSERT INTO personality_links
                   (title, memo, token, is_active, created_by, created_by_name, created_at, test_version)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)''',
            (title, memo, token, session.get('emp_no'), session.get('user_name'), _now(), pt.TEST_VERSION),
        )
        conn.commit()
        link_id = cursor.lastrowid
    finally:
        conn.close()
    return jsonify({'status': 'success', 'id': link_id, 'link': _public_link(token)})


@personality_bp.route('/api/links/<int:link_id>/active', methods=['POST'])
def api_link_active(link_id):
    payload = request.get_json(silent=True) or {}
    active = 1 if payload.get('active') else 0
    conn = get_db()
    try:
        cursor = conn.execute(
            'UPDATE personality_links SET is_active=? WHERE id=?', (active, link_id),
        )
        conn.commit()
        if not cursor.rowcount:
            return _json_error('검사 링크를 찾을 수 없습니다.', 404)
    finally:
        conn.close()
    return jsonify({'status': 'success', 'active': bool(active)})


@personality_bp.route('/api/links/<int:link_id>/delete', methods=['POST'])
def api_link_delete(link_id):
    conn = get_db()
    try:
        row = conn.execute('SELECT id FROM personality_links WHERE id=?', (link_id,)).fetchone()
        if not row:
            return _json_error('검사 링크를 찾을 수 없습니다.', 404)
        count = conn.execute(
            'SELECT COUNT(*) FROM personality_results WHERE link_id=?', (link_id,),
        ).fetchone()[0]
        if count:
            return _json_error(
                f'이미 {count}명이 응시한 링크는 삭제할 수 없습니다. [사용 중지]로 막아 주세요.', 409)
        conn.execute('DELETE FROM personality_links WHERE id=?', (link_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success'})


@personality_bp.route('/api/results')
def api_results():
    keyword = str(request.args.get('q') or '').strip()
    try:
        link_id = int(request.args.get('link_id') or 0)
    except ValueError:
        link_id = 0
    where, params = [], []
    if link_id:
        where.append('r.link_id=?')
        params.append(link_id)
    if keyword:
        like = f'%{keyword}%'
        digits = re.sub(r'\D', '', keyword)
        clause = '(r.name LIKE ? OR r.email LIKE ? OR r.phone LIKE ?'
        params.extend([like, like, f'%{digits}%' if digits else like])
        if re.fullmatch(r'[\d-]+', keyword):
            clause += ' OR r.birth_date LIKE ?'
            params.append(like)
        where.append(clause + ')')
    sql = '''SELECT r.*, l.title AS link_title
               FROM personality_results r
               JOIN personality_links l ON l.id = r.link_id'''
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY r.id DESC LIMIT 1000'
    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'results': [_result_summary(row) for row in rows],
    })


@personality_bp.route('/api/results/<int:result_id>/delete', methods=['POST'])
def api_result_delete(result_id):
    conn = get_db()
    try:
        cursor = conn.execute('DELETE FROM personality_results WHERE id=?', (result_id,))
        conn.commit()
        if not cursor.rowcount:
            return _json_error('응시 결과를 찾을 수 없습니다.', 404)
    finally:
        conn.close()
    return jsonify({'status': 'success'})


# ---------------------------------------------------------------------------
# 응시자 화면 (로그인 없음, 토큰으로 접근)
# ---------------------------------------------------------------------------
def _find_link(conn, token):
    return conn.execute(
        'SELECT * FROM personality_links WHERE token=?', (str(token or '').strip(),),
    ).fetchone()


@personality_bp.route('/t/<token>')
def take(token):
    conn = get_db()
    try:
        link = _find_link(conn, token)
    finally:
        conn.close()
    brand = _brand_context(token if link else '')
    if not link:
        return render_template(
            'personality/take.html', link=None, notice='검사 주소가 올바르지 않거나 삭제된 검사입니다.',
            **brand,
        ), 404
    if not link['is_active']:
        return render_template(
            'personality/take.html', link=link, notice='지금은 응시할 수 없는 검사입니다. 담당자에게 문의해 주세요.',
            **brand,
        )
    bank = _bank_for_version(link['test_version'])
    if not bank:
        return render_template('personality/take.html', link=link,
                               notice='검사 문항 버전을 확인할 수 없습니다. 담당자에게 문의해 주세요.',
                               **brand), 503
    return render_template(
        'personality/take.html', link=link, notice='', token=token,
        questions=bank.question_for_page(), likert=bank.LIKERT_LABELS, **brand,
    )


@personality_bp.route('/t/<token>', methods=['POST'])
def submit(token):
    payload = request.get_json(silent=True) or {}
    if not payload.get('consent'):
        return _json_error('개인정보 수집·이용에 동의해 주세요.')
    try:
        applicant = _clean_applicant(payload)
    except ValueError as error:
        return _json_error(str(error))
    try:
        elapsed = max(0, min(int(payload.get('elapsed_sec') or 0), 86400))
    except (TypeError, ValueError):
        elapsed = 0

    conn = get_db()
    try:
        link = _find_link(conn, token)
        if not link:
            return _json_error('검사 주소가 올바르지 않습니다.', 404)
        if not link['is_active']:
            return _json_error('지금은 응시할 수 없는 검사입니다.', 403)
        bank = _bank_for_version(link['test_version'])
        if not bank:
            return _json_error('검사 문항 버전을 확인할 수 없습니다.', 503)
        try:
            answers = bank.validate_answers(payload.get('answers'))
        except ValueError as error:
            return _json_error(str(error))
        same = conn.execute(
            '''SELECT 1 FROM personality_results
                WHERE link_id=? AND name=? AND phone=? LIMIT 1''',
            (link['id'], applicant['name'], applicant['phone']),
        ).fetchone()
        if same:
            return _json_error('이미 제출하셨습니다. 다시 응시해야 한다면 담당자에게 문의해 주세요.', 409)

        report = bank.build_report(answers, elapsed)
        scores = {
            'percents': {item['key']: item['percent'] for item in report['scales']},
            'total': report.get('total'),
            'validity_sum': report['validity_sum'],
            'reliable': report['reliable'],
        }
        now = _now()
        conn.execute(
            '''INSERT INTO personality_results
                   (link_id, name, birth_date, phone, email, consent_at, answers, answer_sheet,
                    scores, report, elapsed_sec, test_version, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (link['id'], applicant['name'], applicant['birth_date'], applicant['phone'],
             applicant['email'], now,
             json.dumps({str(no): value for no, value in answers.items()}),
             json.dumps(bank.answer_sheet(answers), ensure_ascii=False),
             json.dumps(scores, ensure_ascii=False), json.dumps(report, ensure_ascii=False),
             elapsed, bank.TEST_VERSION, now),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success'})
