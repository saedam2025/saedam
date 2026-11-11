"""학교지원 > 강사알림전송.

강사 명단(학교명·부서명·강사명·주민번호·연락처)을 엑셀 또는 직접 입력으로 등록해 두고,
학교별·강사별로 골라 솔라피(SOLAPI) 문자(SMS/LMS)를 보낸다.

- 명단: `in_instructors`. 같은 학교 + 같은 이름 + 같은 휴대폰번호는 한 사람으로 본다(다시 올리면 갱신).
- 주민번호는 Fernet으로 암호화해 저장하고 화면·엑셀에는 앞 7자리까지만 보인다
  (전체는 관리자 레벨 2 이하가 [보기]를 눌렀을 때만 열린다).
- 발송: `solapi_settings.send_bulk_text`(90바이트 EUC-KR 기준 SMS/LMS 자동 전환). 같은 휴대폰번호는 한 번만 보낸다.
  문구의 {이름}·{학교}·{부서}는 받는 사람마다 바꿔 넣는다. 접수 성공은 최종 수신 완료가 아니다.
- 발송 기록(`in_messages`/`in_message_items`)과 자주 쓰는 문구(`in_templates`)를 남긴다.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO

from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, jsonify, render_template, request, send_file, session

from .database import get_db
from .security import is_admin_session, load_credential_secret, menu_permission_required

instructor_notify_bp = Blueprint('instructor_notify', __name__)

KST = timezone(timedelta(hours=9))
MENU_KEY = 'school_instructor_notify'
REVEAL_MAX_LEVEL = 2             # 주민번호 전체 보기는 관리자만
MAX_IMPORT_BYTES = 3 * 1024 * 1024
MAX_IMPORT_ROWS = 3000
MAX_SEND = 500                   # 한 번에 보낼 수 있는 사람 수
MAX_TEXT = 2000
MAX_SUBJECT = 40
MAX_TEMPLATES = 50
MAX_ERRORS_SHOWN = 30
HISTORY_LIMIT = 100
IMPORT_COLUMNS = ('school', 'department', 'name', 'rrn', 'phone')
HEADER_ALIASES = {
    'school': ('학교명', '학교', '기관명', '기관'),
    'department': ('부서명', '부서', '강의부서', '과목', '과목명'),
    'name': ('강사명', '강사', '이름', '성명'),
    'rrn': ('주민번호', '주민등록번호', '주민등록', '주민'),
    'phone': ('연락처', '휴대폰', '휴대폰번호', '휴대전화', '핸드폰', '전화번호', '전화'),
}
VARIABLES = ('이름', '학교', '부서')


# ---------------------------------------------------------------- 스키마

def init_instructor_notify_schema(conn=None):
    owns = conn is None
    if owns:
        conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS in_instructors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                school TEXT NOT NULL,
                department TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                rrn_enc TEXT NOT NULL DEFAULT '',
                rrn_masked TEXT NOT NULL DEFAULT '',
                memo TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE (school, name, phone)
            );
            CREATE INDEX IF NOT EXISTS idx_in_instructors_school ON in_instructors(school, department, name);

            CREATE TABLE IF NOT EXISTS in_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL DEFAULT 0,
                ok_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                sent_by TEXT NOT NULL DEFAULT '',
                sent_by_name TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS in_message_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                instructor_id INTEGER,
                school TEXT NOT NULL DEFAULT '',
                department TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                phone_masked TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                solapi_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (message_id) REFERENCES in_messages(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_in_message_items_message ON in_message_items(message_id);
            CREATE INDEX IF NOT EXISTS idx_in_message_items_instructor ON in_message_items(instructor_id, ok);

            CREATE TABLE IF NOT EXISTS in_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );
        ''')
        conn.commit()
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------- 도우미

def _now():
    return datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')


def _error(message, code=400, **extra):
    return jsonify({'status': 'error', 'message': message, **extra}), code


def _clean(value, limit=100):
    """줄바꿈·연속 공백을 한 칸으로 줄인 글자(학교·부서·이름용)."""
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _cell_text(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value if value is not None else '').strip()


def _cipher():
    digest = hashlib.sha256(f'instructor-notify-rrn:{load_credential_secret()}'.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value):
    return _cipher().encrypt(str(value).encode('utf-8')).decode('ascii') if value else ''


def _decrypt(value):
    try:
        return _cipher().decrypt(str(value or '').encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''


def clean_rrn(value):
    """주민등록번호를 숫자 13자리로 정리한다(비어 있으면 ''). 형식이 틀리면 ValueError."""
    digits = re.sub(r'\D', '', _cell_text(value))
    if not digits:
        return ''
    if len(digits) == 12:                         # 엑셀이 숫자로 저장해 앞의 0이 사라진 경우(예: 05… 출생)
        digits = '0' + digits
    month, day, gender = digits[2:4], digits[4:6], digits[6:7] if len(digits) == 13 else ''
    if len(digits) != 13 or not ('01' <= month <= '12') or not ('01' <= day <= '31') or gender not in set('12345678'):
        raise ValueError('주민번호는 900101-1234567 형식의 13자리로 적어 주세요.')
    return digits


def mask_rrn(digits):
    return f'{digits[:6]}-{digits[6]}******' if len(digits) == 13 else ''


def _phone(value):
    from .solapi_settings import normalize_phone
    return normalize_phone(value, required=True)


def _phone_label(digits):
    from .solapi_settings import format_phone
    try:
        return format_phone(digits)
    except ValueError:
        return str(digits or '')


def _phone_mask(digits):
    from .solapi_settings import mask_phone
    try:
        return mask_phone(digits)
    except ValueError:
        return '-'


def _header_key(value):
    text = re.sub(r'\(.*?\)|\s+', '', _cell_text(value))
    for key, names in HEADER_ALIASES.items():
        if text in names:
            return key
    return ''


def render_text(template, row):
    """문구의 {이름}·{학교}·{부서}를 받는 사람 값으로 바꾼다."""
    values = {'이름': row['name'], '학교': row['school'], '부서': row['department']}
    result = str(template or '')
    for key, value in values.items():
        result = result.replace('{' + key + '}', str(value or ''))
    return result


# ---------------------------------------------------------------- 명단 저장

def _parse_fields(school, department, name, rrn, phone):
    """한 사람 입력을 검사해 정리한 dict를 돌려준다. 틀리면 ValueError(사용자에게 보일 문장)."""
    school, department, name = _clean(school), _clean(department), _clean(name, 40)
    if not school:
        raise ValueError('학교명을 적어 주세요.')
    if not name:
        raise ValueError('강사명을 적어 주세요.')
    try:
        phone_digits = _phone(phone)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    return {'school': school, 'department': department, 'name': name,
            'phone': phone_digits, 'rrn': clean_rrn(rrn)}


def save_instructor(conn, fields, user, *, instructor_id=None, memo=None):
    """명단에 한 명을 더하거나 고친다. 반환: 'created' | 'updated'.

    instructor_id가 없으면 같은 학교·이름·번호가 이미 있을 때 그 사람을 갱신한다.
    주민번호 칸을 비워 올리면 이미 저장된 주민번호는 지우지 않고 유지한다.
    """
    now = _now()
    existing = None
    if instructor_id:
        existing = conn.execute('SELECT * FROM in_instructors WHERE id=?', (instructor_id,)).fetchone()
        if not existing:
            raise ValueError('고칠 강사를 찾을 수 없습니다.')
    else:
        existing = conn.execute('SELECT * FROM in_instructors WHERE school=? AND name=? AND phone=?',
                                (fields['school'], fields['name'], fields['phone'])).fetchone()
    if existing is not None:
        clash = conn.execute('SELECT id FROM in_instructors WHERE school=? AND name=? AND phone=? AND id<>?',
                             (fields['school'], fields['name'], fields['phone'], existing['id'])).fetchone()
        if clash:
            raise ValueError('같은 학교에 같은 이름·연락처의 강사가 이미 있습니다.')
        rrn_enc, rrn_masked = existing['rrn_enc'], existing['rrn_masked']
        if fields['rrn']:
            rrn_enc, rrn_masked = _encrypt(fields['rrn']), mask_rrn(fields['rrn'])
        elif instructor_id and fields.get('clear_rrn'):
            rrn_enc, rrn_masked = '', ''
        conn.execute(
            'UPDATE in_instructors SET school=?, department=?, name=?, phone=?, rrn_enc=?, rrn_masked=?, memo=?, updated_at=? WHERE id=?',
            (fields['school'], fields['department'] or (existing['department'] if not instructor_id else ''),
             fields['name'], fields['phone'], rrn_enc, rrn_masked,
             existing['memo'] if memo is None else memo, now, existing['id']))
        return 'updated'
    conn.execute(
        'INSERT INTO in_instructors (school, department, name, phone, rrn_enc, rrn_masked, memo, created_by, created_at, updated_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (fields['school'], fields['department'], fields['name'], fields['phone'], _encrypt(fields['rrn']),
         mask_rrn(fields['rrn']), memo or '', user, now, now))
    return 'created'


def import_rows(conn, rows, user):
    """엑셀에서 읽은 줄들을 명단에 반영한다. 틀린 줄은 건너뛰고 이유를 모은다. 반환: 요약 dict."""
    summary = {'rows': len(rows), 'created': 0, 'updated': 0, 'errors': []}
    seen = set()
    for row in rows:
        try:
            fields = _parse_fields(row['school'], row['department'], row['name'], row['rrn'], row['phone'])
        except ValueError as exc:
            summary['errors'].append(f"{row['line']}번째 줄 {_clean(row['name'], 40) or '(이름 없음)'}: {exc}")
            continue
        key = (fields['school'], fields['name'], fields['phone'])
        if key in seen:
            summary['errors'].append(f"{row['line']}번째 줄 {fields['name']}: 파일 안에 같은 강사가 또 있어 건너뛰었습니다.")
            continue
        seen.add(key)
        summary[save_instructor(conn, fields, user)] += 1
    conn.commit()
    return summary


def import_message(summary):
    done = summary['created'] + summary['updated']
    parts = [f"{summary['rows']}줄 중 {done}명을 처리했습니다."]
    if summary['created']:
        parts.append(f"새로 등록 {summary['created']}명")
    if summary['updated']:
        parts.append(f"이미 있어 정보 갱신 {summary['updated']}명")
    if summary['errors']:
        parts.append(f"건너뜀 {len(summary['errors'])}줄(아래 이유를 확인해 고친 뒤 다시 올려 주세요)")
    return ' · '.join(parts)


def parse_workbook(upload):
    """엑셀(.xlsx) 첫 시트를 줄 목록으로 읽는다. 머리글이 있으면 순서와 상관없이 찾고,
    없으면 학교명·부서명·강사명·주민번호·연락처 순서로 본다."""
    from openpyxl import load_workbook
    name = str(getattr(upload, 'filename', '') or '')
    if not name.lower().endswith('.xlsx'):
        raise ValueError('엑셀(.xlsx) 파일만 올릴 수 있습니다. .xls 파일은 [다른 이름으로 저장]에서 .xlsx로 바꿔 주세요.')
    data = upload.stream.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError('엑셀 파일은 3MB까지 올릴 수 있습니다.')
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
            values = list(cells[:len(IMPORT_COLUMNS)])
            values += [''] * (len(IMPORT_COLUMNS) - len(values))
        if len(rows) >= MAX_IMPORT_ROWS:
            raise ValueError(f'한 번에 {MAX_IMPORT_ROWS}줄까지 올릴 수 있습니다. 나누어 올려 주세요.')
        item = {key: _cell_text(values[index]) for index, key in enumerate(IMPORT_COLUMNS)}
        item['line'] = number
        rows.append(item)
    if not rows:
        raise ValueError('엑셀에서 읽을 강사 줄이 없습니다. 양식 파일의 머리글 아래에 강사를 적어 주세요.')
    return rows


# ---------------------------------------------------------------- 명단 조회

LIST_SQL = '''
    SELECT i.*,
           (SELECT MAX(m.sent_at) FROM in_message_items x JOIN in_messages m ON m.id = x.message_id
             WHERE x.instructor_id = i.id AND x.ok = 1) AS last_sent_at
      FROM in_instructors i
     ORDER BY i.school, i.department, i.name, i.id
'''


def instructor_view(row):
    return {
        'id': row['id'], 'school': row['school'], 'department': row['department'], 'name': row['name'],
        'phone': _phone_label(row['phone']), 'phone_ok': bool(re.fullmatch(r'01[016789]\d{7,8}', row['phone'] or '')),
        'rrn': row['rrn_masked'] or '', 'memo': row['memo'] or '',
        'last_sent_at': row['last_sent_at'] if 'last_sent_at' in row.keys() else '',
        'updated_at': row['updated_at'],
    }


def list_instructors(conn):
    return [instructor_view(row) for row in conn.execute(LIST_SQL).fetchall()]


# ---------------------------------------------------------------- 발송

def plan_send(conn, ids, text, subject=''):
    """고른 강사들을 실제 발송 대상으로 정리한다(같은 번호는 한 번만, 번호가 틀린 사람은 제외).
    반환: (대상 목록, 제외 목록[{name, school, reason}])."""
    wanted, seen_ids = [], set()
    for value in ids or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number not in seen_ids:
            seen_ids.add(number)
            wanted.append(number)
    rows = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        marks = ','.join('?' * len(chunk))
        for row in conn.execute(f'SELECT * FROM in_instructors WHERE id IN ({marks})', chunk).fetchall():
            rows[row['id']] = row
    targets, skipped, phones = [], [], set()
    for number in wanted:
        row = rows.get(number)
        if row is None:
            skipped.append({'name': f'(삭제된 강사 {number})', 'school': '', 'reason': '명단에서 지워진 강사입니다.'})
            continue
        if not re.fullmatch(r'01[016789]\d{7,8}', row['phone'] or ''):
            skipped.append({'name': row['name'], 'school': row['school'], 'reason': '휴대폰번호가 올바르지 않습니다.'})
            continue
        if row['phone'] in phones:
            skipped.append({'name': row['name'], 'school': row['school'], 'reason': '같은 휴대폰번호가 이미 대상에 있어 한 번만 보냅니다.'})
            continue
        phones.add(row['phone'])
        targets.append({'id': row['id'], 'school': row['school'], 'department': row['department'], 'name': row['name'],
                        'phone': row['phone'], 'text': render_text(text, row)})
    return targets, skipped


def send_messages(conn, targets, subject, text, user, user_name):
    """대상에게 솔라피로 문자를 보내고 기록을 남긴다. 문자 서비스 오류(RuntimeError)는 그대로 올린다."""
    from . import solapi_settings as solapi
    settings = solapi.get_settings(conn)
    results = solapi.send_bulk_text(
        [{'to': t['phone'], 'text': t['text'], 'subject': subject} for t in targets], settings=settings)
    ok_count = sum(1 for result in results if result.get('ok'))
    cursor = conn.execute(
        'INSERT INTO in_messages (subject, body, total, ok_count, fail_count, sent_by, sent_by_name, sent_at) VALUES (?,?,?,?,?,?,?,?)',
        (subject, text, len(targets), ok_count, len(targets) - ok_count, user, user_name, _now()))
    message_id = cursor.lastrowid
    rows = []
    for target, result in zip(targets, results):
        ok = bool(result.get('ok'))
        error = '' if ok else str(result.get('error') or '알 수 없는 오류')
        conn.execute(
            'INSERT INTO in_message_items (message_id, instructor_id, school, department, name, phone_masked, ok, error, solapi_id) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (message_id, target['id'], target['school'], target['department'], target['name'],
             _phone_mask(target['phone']), 1 if ok else 0, error, str(result.get('message_id') or '')))
        rows.append({'id': target['id'], 'name': target['name'], 'school': target['school'], 'ok': ok, 'error': error})
    conn.commit()
    return {'message_id': message_id, 'total': len(targets), 'ok': ok_count,
            'failed': len(targets) - ok_count, 'results': rows}


# ---------------------------------------------------------------- 화면

@instructor_notify_bp.route('/')
@menu_permission_required(MENU_KEY)
def index():
    return render_template('instructor_notify/index.html',
                           can_reveal=is_admin_session(REVEAL_MAX_LEVEL),
                           max_send=MAX_SEND, max_text=MAX_TEXT, max_subject=MAX_SUBJECT,
                           variables=VARIABLES)


@instructor_notify_bp.route('/api/status')
@menu_permission_required(MENU_KEY)
def api_status():
    from . import solapi_settings as solapi
    conn = get_db()
    try:
        ready = bool(solapi.get_settings(conn).get('sms_configured'))
    finally:
        conn.close()
    return jsonify({'status': 'success', 'sms_ready': ready})


@instructor_notify_bp.route('/api/instructors')
@menu_permission_required(MENU_KEY)
def api_list():
    conn = get_db()
    try:
        return jsonify({'status': 'success', 'instructors': list_instructors(conn)})
    finally:
        conn.close()


@instructor_notify_bp.route('/api/instructors', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_save():
    data = request.get_json(silent=True) or {}
    try:
        fields = _parse_fields(data.get('school'), data.get('department'), data.get('name'),
                               data.get('rrn'), data.get('phone'))
    except ValueError as exc:
        return _error(str(exc))
    fields['clear_rrn'] = bool(data.get('clear_rrn'))
    try:
        instructor_id = int(data['id']) if data.get('id') else None
    except (TypeError, ValueError):
        return _error('잘못된 요청입니다.')
    conn = get_db()
    try:
        outcome = save_instructor(conn, fields, str(session.get('emp_no') or ''), instructor_id=instructor_id,
                                  memo=_clean(data.get('memo'), 200) if 'memo' in data else None)
        conn.commit()
    except ValueError as exc:
        return _error(str(exc))
    finally:
        conn.close()
    return jsonify({'status': 'success', 'outcome': outcome,
                    'message': '명단에 등록했습니다.' if outcome == 'created' else '이미 있는 강사라 정보를 고쳤습니다.'})


@instructor_notify_bp.route('/api/instructors/delete', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_delete():
    data = request.get_json(silent=True) or {}
    ids = []
    for value in data.get('ids') or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not ids:
        return _error('지울 강사를 골라 주세요.')
    if not data.get('confirm'):
        return _error('지우기 확인이 필요합니다.')
    conn = get_db()
    try:
        removed = 0
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ','.join('?' * len(chunk))
            # 발송 기록은 이름·학교를 따로 적어 두므로 남기고, 명단 연결만 끊는다.
            conn.execute(f'UPDATE in_message_items SET instructor_id=NULL WHERE instructor_id IN ({marks})', chunk)
            removed += conn.execute(f'DELETE FROM in_instructors WHERE id IN ({marks})', chunk).rowcount
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'removed': removed, 'message': f'{removed}명을 명단에서 지웠습니다.'})


@instructor_notify_bp.route('/api/instructors/<int:instructor_id>/rrn')
@menu_permission_required(MENU_KEY)
def api_reveal_rrn(instructor_id):
    if not is_admin_session(REVEAL_MAX_LEVEL):
        return _error('주민번호 전체 보기는 관리자만 할 수 있습니다.', 403)
    conn = get_db()
    try:
        row = conn.execute('SELECT rrn_enc FROM in_instructors WHERE id=?', (instructor_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return _error('강사를 찾을 수 없습니다.', 404)
    digits = _decrypt(row['rrn_enc'])
    if not digits:
        return _error('저장된 주민번호가 없습니다.', 404)
    response = jsonify({'status': 'success', 'rrn': f'{digits[:6]}-{digits[6:]}'})
    response.headers['Cache-Control'] = 'no-store'
    return response


@instructor_notify_bp.route('/api/import', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_import():
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return _error('올릴 엑셀 파일을 골라 주세요.')
    try:
        rows = parse_workbook(upload)
    except ValueError as exc:
        return _error(str(exc))
    conn = get_db()
    try:
        summary = import_rows(conn, rows, str(session.get('emp_no') or ''))
    finally:
        conn.close()
    errors = summary['errors']
    return jsonify({'status': 'success', 'message': import_message(summary), 'created': summary['created'],
                    'updated': summary['updated'], 'error_count': len(errors),
                    'errors': errors[:MAX_ERRORS_SHOWN]})


def _write_workbook(headers, rows, widths, text_columns):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = '강사명단'
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='E8EEF9')
        cell.alignment = Alignment(horizontal='center')
    for row in rows:
        sheet.append(list(row))
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = 's'                      # '='로 시작하는 글자도 수식이 되지 않게 문자열로 고정
            if cell.column in text_columns:
                cell.number_format = '@'                  # 연락처·주민번호 앞의 0이 사라지지 않게
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[chr(64 + index)].width = width
    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return stream


XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@instructor_notify_bp.route('/template.xlsx')
@menu_permission_required(MENU_KEY)
def download_template():
    stream = _write_workbook(
        ['학교명', '부서명', '강사명', '주민번호', '연락처'],
        [['새담초등학교', '로봇과학', '홍길동', '900101-1234567', '010-1234-5678']],
        (22, 18, 14, 20, 18), {4, 5})
    return send_file(stream, mimetype=XLSX_MIME, as_attachment=True, download_name='강사명단_양식.xlsx')


@instructor_notify_bp.route('/export.xlsx')
@menu_permission_required(MENU_KEY)
def download_roster():
    conn = get_db()
    try:
        people = list_instructors(conn)
    finally:
        conn.close()
    # 주민번호는 앞 7자리까지만 내보낸다(전체는 엑셀로 내려받지 않는다).
    stream = _write_workbook(
        ['학교명', '부서명', '강사명', '주민번호', '연락처', '최근 발송'],
        [[p['school'], p['department'], p['name'], p['rrn'], p['phone'], p['last_sent_at'] or ''] for p in people],
        (22, 18, 14, 20, 18, 20), {4, 5})
    return send_file(stream, mimetype=XLSX_MIME, as_attachment=True,
                     download_name=f"강사명단_{datetime.now(KST).strftime('%Y%m%d')}.xlsx")


@instructor_notify_bp.route('/api/send', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_send():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    subject = _clean(data.get('subject'), MAX_SUBJECT)
    if not text:
        return _error('보낼 문구를 적어 주세요.')
    if len(text) > MAX_TEXT:
        return _error(f'문구는 {MAX_TEXT}자까지 보낼 수 있습니다.')
    ids = data.get('ids') or []
    if not isinstance(ids, list) or not ids:
        return _error('문자를 받을 강사를 골라 주세요.')
    if len(ids) > MAX_SEND:
        return _error(f'한 번에 {MAX_SEND}명까지 보낼 수 있습니다. 나누어 보내 주세요.')
    from . import solapi_settings as solapi
    conn = get_db()
    try:
        if not solapi.get_settings(conn).get('sms_configured'):
            return _error('문자 발송 설정이 아직 없습니다. 통합관리 > 솔라피설정에서 API 키·시크릿·발신번호를 먼저 저장해 주세요.', 409)
        targets, skipped = plan_send(conn, ids, text, subject)
        if not targets:
            return _error('보낼 수 있는 강사가 없습니다. ' + (skipped[0]['reason'] if skipped else ''), 400, skipped=skipped)
        if data.get('dry_run'):
            sample = targets[0]
            return jsonify({'status': 'success', 'dry_run': True, 'total': len(targets), 'skipped': skipped,
                            'sample': {'name': sample['name'], 'school': sample['school'], 'text': sample['text']}})
        if not data.get('confirm'):
            return _error('발송 확인이 필요합니다.')
        try:
            outcome = send_messages(conn, targets, subject, text, str(session.get('emp_no') or ''),
                                    str(session.get('user_name') or ''))
        except RuntimeError as exc:
            return _error(str(exc), 502)
    finally:
        conn.close()
    outcome['skipped'] = skipped
    outcome['status'] = 'success'
    outcome['message'] = (f"{outcome['total']}명 중 {outcome['ok']}명 접수"
                          + (f", {outcome['failed']}명 실패" if outcome['failed'] else '') + '했습니다.')
    return jsonify(outcome)


# ---------------------------------------------------------------- 발송 기록

@instructor_notify_bp.route('/api/history')
@menu_permission_required(MENU_KEY)
def api_history():
    conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM in_messages ORDER BY id DESC LIMIT ?', (HISTORY_LIMIT,)).fetchall()
        history = []
        for row in rows:
            schools = [item['school'] for item in conn.execute(
                'SELECT DISTINCT school FROM in_message_items WHERE message_id=? ORDER BY school', (row['id'],))]
            history.append({
                'id': row['id'], 'sent_at': row['sent_at'], 'sent_by_name': row['sent_by_name'],
                'subject': row['subject'], 'preview': row['body'][:60], 'total': row['total'],
                'ok': row['ok_count'], 'failed': row['fail_count'],
                'schools': schools[:3], 'school_count': len(schools),
            })
    finally:
        conn.close()
    return jsonify({'status': 'success', 'history': history})


@instructor_notify_bp.route('/api/history/<int:message_id>')
@menu_permission_required(MENU_KEY)
def api_history_detail(message_id):
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM in_messages WHERE id=?', (message_id,)).fetchone()
        if not row:
            return _error('발송 기록을 찾을 수 없습니다.', 404)
        items = conn.execute(
            'SELECT i.*, (SELECT 1 FROM in_instructors x WHERE x.id = i.instructor_id) AS alive '
            'FROM in_message_items i WHERE i.message_id=? ORDER BY i.school, i.name, i.id', (message_id,)).fetchall()
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'message': {'id': row['id'], 'sent_at': row['sent_at'], 'sent_by_name': row['sent_by_name'],
                    'subject': row['subject'], 'body': row['body'], 'total': row['total'],
                    'ok': row['ok_count'], 'failed': row['fail_count']},
        'items': [{'instructor_id': item['instructor_id'] if item['alive'] else None, 'school': item['school'],
                   'department': item['department'], 'name': item['name'], 'phone': item['phone_masked'],
                   'ok': bool(item['ok']), 'error': item['error']} for item in items],
    })


# ---------------------------------------------------------------- 자주 쓰는 문구

@instructor_notify_bp.route('/api/templates')
@menu_permission_required(MENU_KEY)
def api_templates():
    conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM in_templates ORDER BY id DESC').fetchall()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'templates': [
        {'id': r['id'], 'title': r['title'], 'subject': r['subject'], 'body': r['body']} for r in rows]})


@instructor_notify_bp.route('/api/templates', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_template_save():
    data = request.get_json(silent=True) or {}
    title = _clean(data.get('title'), 40)
    body = str(data.get('body') or '').replace('\r\n', '\n').strip()[:MAX_TEXT]
    if not title:
        return _error('문구 이름을 적어 주세요.')
    if not body:
        return _error('저장할 문구가 비어 있습니다.')
    subject = _clean(data.get('subject'), MAX_SUBJECT)
    conn = get_db()
    try:
        existing = conn.execute('SELECT id FROM in_templates WHERE title=?', (title,)).fetchone()
        if existing:
            conn.execute('UPDATE in_templates SET subject=?, body=? WHERE id=?', (subject, body, existing['id']))
            template_id = existing['id']
        else:
            if conn.execute('SELECT COUNT(*) FROM in_templates').fetchone()[0] >= MAX_TEMPLATES:
                return _error(f'문구는 {MAX_TEMPLATES}개까지 저장할 수 있습니다. 안 쓰는 문구를 지워 주세요.')
            template_id = conn.execute('INSERT INTO in_templates (title, subject, body, created_at) VALUES (?,?,?,?)',
                                       (title, subject, body, _now())).lastrowid
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'success', 'id': template_id, 'message': f'[{title}] 문구를 저장했습니다.'})


@instructor_notify_bp.route('/api/templates/<int:template_id>/delete', methods=['POST'])
@menu_permission_required(MENU_KEY)
def api_template_delete(template_id):
    conn = get_db()
    try:
        removed = conn.execute('DELETE FROM in_templates WHERE id=?', (template_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not removed:
        return _error('문구를 찾을 수 없습니다.', 404)
    return jsonify({'status': 'success', 'message': '문구를 지웠습니다.'})
