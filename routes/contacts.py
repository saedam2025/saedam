import base64
import json
import os
import re
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from html import escape

from flask import Blueprint, abort, redirect, render_template, render_template_string, request, session, url_for, jsonify

from routes.database import get_db

contacts_bp = Blueprint('contacts', __name__)

MAIL_SETTINGS_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'mail_settings.json'))

HQ_POSITIONS = ['대표이사', '이사', '실장', '팀장', '사원', '계약직']
CENTER_POSITIONS = ['센터장(팀장)', '센터장 팀장', '센터장']
# 연락망에 기본적으로 노출하지 않는 기존 직급.
# 구성원 관리에서 새로 만든 직급은 아래 목록에 없으므로 자동으로 본부 연락망에 포함됩니다.
NON_CONTACT_POSITIONS = {
    '최고관리자', '전담코디', '보조코디', '안전코디',
    '방과후강사', '맞춤형강사', '임시회원',
}
MANUAL_CONTACT_GROUPS = [
    ('headquarters', '본부'),
    ('north_branch', '북부지점'),
    ('partner', '협력사'),
]
MANUAL_CONTACT_GROUP_MAP = dict(MANUAL_CONTACT_GROUPS)


def _columns(conn, table_name):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _dash(value):
    value = str(value or '').strip()
    return value if value else '-'


def _clean_text(value):
    return str(value or '').strip()


def _is_hidden_contact_name(value):
    """연락망에서 숨길 관리자 계정/표시용 이름인지 확인한다.

    '관리자', '관리자5', '관리자03'처럼 성명이 '관리자'로 시작하면
    연락망의 인명 목록에서는 표시하지 않는다.
    """
    return _clean_text(value).startswith('관리자')


def _html_text(value):
    return escape(_clean_text(value))


def _mail_credentials():
    sender_email = os.environ.get('MAIL_USERNAME', '').strip()
    sender_password = os.environ.get('MAIL_PASSWORD', '').strip()

    if (not sender_email or not sender_password) and os.path.exists(MAIL_SETTINGS_PATH):
        try:
            with open(MAIL_SETTINGS_PATH, encoding='utf-8') as f:
                settings = json.load(f)
            sender_email = sender_email or _clean_text(settings.get('MAIL_USERNAME') or settings.get('email'))
            sender_password = sender_password or _clean_text(settings.get('MAIL_PASSWORD') or settings.get('password'))
        except Exception:
            pass

    return sender_email, sender_password.replace(' ', '')


def _decode_card_image(image_data):
    image_data = _clean_text(image_data)
    match = re.match(r'^data:image/(png|jpeg|jpg);base64,(.+)$', image_data, re.I | re.S)
    if not match:
        return None, None

    subtype = match.group(1).lower()
    if subtype == 'jpg':
        subtype = 'jpeg'

    try:
        return base64.b64decode(match.group(2), validate=True), subtype
    except Exception:
        return None, None


def _send_business_card_email(to_email, card_name, position, department, phone, email, image_data):
    to_email = _clean_text(to_email)
    if not to_email or not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$', to_email):
        return False, '받을 이메일 주소 형식이 올바르지 않습니다.'

    image_bytes, image_subtype = _decode_card_image(image_data)
    if not image_bytes:
        return False, '명함 이미지 데이터가 올바르지 않습니다.'

    sender_email, sender_password = _mail_credentials()
    if not sender_email or not sender_password:
        return False, '메일 계정 설정이 없습니다.'

    card_name = _clean_text(card_name) or '담당자'
    position = _clean_text(position)
    department = _clean_text(department)
    phone = _clean_text(phone)
    email = _clean_text(email)

    subject = f"[새담] {card_name} 명함 전달드립니다"
    body = f"""
    <div style="font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif; color:#0f172a; line-height:1.65;">
        <h2 style="margin:0 0 14px; color:#004ea2;">{_html_text(card_name)} 명함</h2>
        <p style="margin:0 0 16px;">안녕하세요. 요청하신 {_html_text(card_name)}님의 명함을 전달드립니다.</p>
        <div style="margin:16px 0 18px;">
            <img src="cid:business_card_image" alt="{_html_text(card_name)} 명함" style="display:block; max-width:520px; width:100%; height:auto; border:1px solid #dbe3ef; border-radius:10px;">
        </div>
        <table cellpadding="0" cellspacing="0" style="border-collapse:collapse; border:1px solid #dbe3ef; min-width:420px; font-size:13px;">
            <tr><th style="background:#f8fafc; border:1px solid #dbe3ef; text-align:left; width:90px; padding:8px 10px;">성명</th><td style="border:1px solid #dbe3ef; padding:8px 10px;">{_html_text(card_name)}</td></tr>
            <tr><th style="background:#f8fafc; border:1px solid #dbe3ef; text-align:left; padding:8px 10px;">직책</th><td style="border:1px solid #dbe3ef; padding:8px 10px;">{_html_text(position)}</td></tr>
            <tr><th style="background:#f8fafc; border:1px solid #dbe3ef; text-align:left; padding:8px 10px;">소속</th><td style="border:1px solid #dbe3ef; padding:8px 10px;">{_html_text(department)}</td></tr>
            <tr><th style="background:#f8fafc; border:1px solid #dbe3ef; text-align:left; padding:8px 10px;">연락처</th><td style="border:1px solid #dbe3ef; padding:8px 10px;">{_html_text(phone)}</td></tr>
            <tr><th style="background:#f8fafc; border:1px solid #dbe3ef; text-align:left; padding:8px 10px;">이메일</th><td style="border:1px solid #dbe3ef; padding:8px 10px;">{_html_text(email)}</td></tr>
        </table>
        <p style="margin-top:18px; color:#64748b; font-size:12px;">본 메일은 새담 인트라넷 명함 기능에서 발송되었습니다.</p>
    </div>
    """

    msg = MIMEMultipart('related')
    msg['From'] = formataddr((str(Header('새담 인트라넷', 'utf-8')), sender_email))
    msg['To'] = to_email
    msg['Subject'] = Header(subject, 'utf-8')

    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(body, 'html', 'utf-8'))
    msg.attach(alternative)

    image_part = MIMEImage(image_bytes, _subtype=image_subtype)
    image_part.add_header('Content-ID', '<business_card_image>')
    image_part.add_header('Content-Disposition', 'inline')
    msg.attach(image_part)

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())
        return True, ''
    except Exception as exc:
        return False, str(exc)


def _find_user_for_role(conn, positions, department_keyword=None):
    placeholders = ','.join(['?'] * len(positions))
    params = list(positions)
    query = f"""
        SELECT name, phone, email
        FROM users
        WHERE emp_no != 'admin'
          AND TRIM(COALESCE(name, '')) NOT LIKE '관리자%'
          AND COALESCE(status, '승인') = '승인'
          AND COALESCE(retire_date, '') = ''
          AND position IN ({placeholders})
    """
    if department_keyword:
        query += " AND COALESCE(department, '') LIKE ?"
        params.append(f"%{department_keyword}%")
    query += " ORDER BY level ASC, id ASC LIMIT 1"
    return conn.execute(query, params).fetchone()


def _init_office_contact_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS office_contact_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_key TEXT NOT NULL,
            category_name TEXT NOT NULL,
            organization_name TEXT,
            role_title TEXT,
            person_name TEXT,
            address TEXT,
            phone TEXT,
            fax TEXT,
            email TEXT,
            extra_contact TEXT,
            memo TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT
        )
    """)
    existing_columns = _columns(conn, 'office_contact_entries')
    for column_name in ('fax', 'extra_contact', 'memo'):
        if column_name not in existing_columns:
            conn.execute(f"ALTER TABLE office_contact_entries ADD COLUMN {column_name} TEXT")

    representative = _find_user_for_role(conn, ['대표이사'])
    branch_director = _find_user_for_role(conn, ['이사'], '북부') or _find_user_for_role(conn, ['이사'])

    seed_rows = [
        ('headquarters', '본부', '본부', '대표이사', representative, 1),
        ('north_branch', '북부지점', '북부지점', '담당이사', branch_director, 1),
        ('partner', '협력사', '협력사', '대표이사', None, 1),
    ]

    for key, category_name, organization_name, role_title, user_row, sort_order in seed_rows:
        exists = conn.execute(
            "SELECT id FROM office_contact_entries WHERE category_key = ? LIMIT 1",
            (key,)
        ).fetchone()
        if exists:
            continue

        conn.execute("""
            INSERT INTO office_contact_entries
                (category_key, category_name, organization_name, role_title, person_name, address,
                 phone, fax, email, extra_contact, memo, sort_order)
            VALUES (?, ?, ?, ?, ?, '', ?, '', ?, '', '', ?)
        """, (
            key,
            category_name,
            organization_name,
            role_title,
            user_row['name'] if user_row else '',
            user_row['phone'] if user_row else '',
            user_row['email'] if user_row else '',
            sort_order,
        ))
    conn.commit()


def _init_center_team_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_center_teams (
            emp_no TEXT PRIMARY KEY,
            team_no INTEGER NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _ensure_contact_user_columns(conn):
    columns = _columns(conn, 'users')
    if 'custom_department' not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN custom_department TEXT DEFAULT ''")
    if 'custom_team' not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN custom_team TEXT DEFAULT ''")
    conn.commit()


def _load_manual_contact_groups(conn):
    rows = conn.execute("""
        SELECT id, category_key, category_name, organization_name, role_title, person_name,
               address, phone, fax, email, extra_contact, memo
        FROM office_contact_entries
        ORDER BY
            CASE category_key
                WHEN 'headquarters' THEN 1
                WHEN 'north_branch' THEN 2
                WHEN 'partner' THEN 3
                ELSE 9
            END,
            sort_order ASC,
            id ASC
    """).fetchall()

    grouped = {key: [] for key, _ in MANUAL_CONTACT_GROUPS}
    for row in rows:
        data = dict(row)
        if _is_hidden_contact_name(data.get('person_name')):
            continue
        data['organization_name'] = _dash(data.get('organization_name'))
        data['role_title'] = _dash(data.get('role_title'))
        data['person_name'] = _dash(data.get('person_name'))
        data['address'] = _dash(data.get('address'))
        data['phone'] = _dash(data.get('phone'))
        data['fax'] = _dash(data.get('fax'))
        data['email'] = _dash(data.get('email'))
        data['extra_contact'] = _dash(data.get('extra_contact'))
        data['memo'] = _dash(data.get('memo'))
        grouped.setdefault(data.get('category_key') or 'partner', []).append(data)

    return [
        {'key': key, 'name': name, 'items': grouped.get(key, [])}
        for key, name in MANUAL_CONTACT_GROUPS
    ]


def _contact_from_user(row):
    data = dict(row)
    return {
        'name': _dash(data.get('name')),
        'position': _dash(data.get('position')),
        'department': _dash(data.get('department')),
        'custom_department': _clean_text(data.get('custom_department')),
        'custom_team': _clean_text(data.get('custom_team')),
        'phone': _dash(data.get('phone')),
        'email': _dash(data.get('email')),
        'emp_no': _dash(data.get('emp_no')),
    }



def _load_contact_position_groups(conn):
    """구성원 관리의 동적 직급을 연락망 분류에 반영한다.

    기존 고정 직급은 그대로 유지하고, hr_positions에서 새로 생성된 직급은
    기본적으로 본부 연락망에 포함한다. 직급명에 '센터장'이 들어간 신규 직급은
    센터장 연락망으로 분류한다.
    """
    headquarters = list(HQ_POSITIONS)
    centers = list(CENTER_POSITIONS)

    # 구버전 DB처럼 hr_positions 테이블이 아직 없는 경우에도 기존 동작을 유지한다.
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hr_positions' LIMIT 1"
    ).fetchone()
    if not table_exists:
        return headquarters, centers

    rows = conn.execute("""
        SELECT name
        FROM hr_positions
        ORDER BY level ASC, sort_order ASC, name ASC
    """).fetchall()

    for row in rows:
        position = _clean_text(row['name'])
        if not position or position in NON_CONTACT_POSITIONS:
            continue

        compact_position = re.sub(r'\s+', '', position)
        if position in centers or '센터장' in compact_position:
            if position not in centers:
                centers.append(position)
            continue

        if position not in headquarters:
            headquarters.append(position)

    return headquarters, centers

def _load_user_contacts(conn, positions):
    placeholders = ','.join(['?'] * len(positions))
    rows = conn.execute(f"""
        SELECT emp_no, name, position, department, custom_department, custom_team,
               phone, email, level
        FROM users
        WHERE emp_no != 'admin'
          AND TRIM(COALESCE(name, '')) NOT LIKE '관리자%'
          AND COALESCE(status, '승인') = '승인'
          AND COALESCE(retire_date, '') = ''
          AND position IN ({placeholders})
        ORDER BY level ASC, name ASC
    """, positions).fetchall()

    order = {position: idx for idx, position in enumerate(positions)}
    contacts = [_contact_from_user(row) for row in rows]
    contacts.sort(key=lambda item: (order.get(item['position'], 99), item['name']))
    return contacts


def _load_custom_contacts(conn):
    rows = conn.execute('''
        SELECT emp_no, name, position, department, custom_department, custom_team,
               phone, email, level
        FROM users
        WHERE emp_no != 'admin'
          AND TRIM(COALESCE(name, '')) NOT LIKE '관리자%'
          AND COALESCE(status, '승인') = '승인'
          AND COALESCE(retire_date, '') = ''
          AND (TRIM(COALESCE(custom_department, '')) <> ''
               OR TRIM(COALESCE(custom_team, '')) <> '')
        ORDER BY custom_department ASC, custom_team ASC, level ASC, name ASC
    ''').fetchall()
    return [_contact_from_user(row) for row in rows]


def _load_center_contacts(conn, positions=None):
    positions = positions or CENTER_POSITIONS
    contacts = _load_user_contacts(conn, positions)
    team_rows = conn.execute("SELECT emp_no, team_no FROM contact_center_teams").fetchall()
    team_map = {str(row['emp_no']): int(row['team_no']) for row in team_rows}
    school_cols = _columns(conn, 'schools')
    location_expr = "office_location" if 'office_location' in school_cols else "''"
    second_director_expr = "center_director_id_2" if 'center_director_id_2' in school_cols else "''"
    query = f"""
        SELECT id, center_director_id, {second_director_expr} AS center_director_id_2,
               school_name, {location_expr} AS office_location
        FROM schools
        WHERE (COALESCE(center_director_id, '') <> ''
           OR COALESCE({second_director_expr}, '') <> '')
    """
    if 'is_active' in school_cols:
        query += " AND COALESCE(is_active, 1) = 1"
    query += " ORDER BY year DESC, school_name ASC"

    assignments = {}
    for row in conn.execute(query).fetchall():
        school_name = str(row['school_name'] or '').strip()
        office_location = str(row['office_location'] or '').strip()
        if not school_name:
            continue
        for emp_no in {row['center_director_id'], row['center_director_id_2']} - {None, ''}:
            bucket = assignments.setdefault(emp_no, {'schools': [], 'locations': [], 'school_ids': []})
            bucket['schools'].append(school_name)
            bucket['locations'].append(office_location if office_location else '-')
            bucket['school_ids'].append(row['id'])

    for contact in contacts:
        assignment = assignments.get(contact['emp_no'], {})
        contact['assigned_schools'] = _dash(', '.join(assignment.get('schools', [])))
        contact['office_locations'] = _dash(', '.join(assignment.get('locations', [])))
        contact['assigned_school_ids'] = assignment.get('school_ids', [])
        contact['team_no'] = team_map.get(contact['emp_no'])
        contact['team_name'] = f"{contact['team_no']}팀" if contact['team_no'] else '미지정'
        contact['is_team_leader'] = contact['position'] in ('센터장(팀장)', '센터장 팀장')
    contacts.sort(key=lambda item: (
        item['team_no'] if item['team_no'] is not None else 999,
        0 if item['is_team_leader'] else 1,
        item['name'],
    ))
    return contacts


def _group_center_contacts(contacts):
    groups = []
    for contact in contacts:
        team_no = contact.get('team_no')
        if not groups or groups[-1]['team_no'] != team_no:
            groups.append({
                'team_no': team_no,
                'team_name': f'{team_no}팀' if team_no else '미지정',
                'contacts': [],
            })
        groups[-1]['contacts'].append(contact)
    return groups


def _builder_contact_records(manual_groups, headquarters, centers, schools, custom_contacts):
    records = []
    for group in manual_groups:
        source = 'partners' if group['key'] == 'partner' else 'headquarters'
        source_label = '협력사' if source == 'partners' else '본부'
        for item in group['items']:
            records.append({
                'key': f"manual:{item['id']}",
                'source': source,
                'source_label': source_label,
                'group': group['name'],
                'name': item['person_name'],
                'role': item['role_title'],
                'organization': item['organization_name'],
                'phone': item['phone'],
                'email': item['email'],
                'detail': item['extra_contact'],
            })
    for item in headquarters:
        source = 'managers' if item['position'] == '실장' else 'staff'
        records.append({
            'key': f"employee:{item['emp_no']}",
            'source': source,
            'source_label': '실장' if source == 'managers' else '직원',
            'group': '본사',
            'name': item['name'],
            'role': item['position'],
            'organization': item['department'],
            'phone': item['phone'],
            'email': item['email'],
            'detail': '',
        })
    for item in centers:
        records.append({
            'key': f"center:{item['emp_no']}",
            'source': 'centers',
            'source_label': '센터장',
            'group': item['team_name'],
            'name': item['name'],
            'role': item['position'],
            'organization': item['department'],
            'phone': item['phone'],
            'email': item['email'],
            'detail': item['assigned_schools'],
        })
    for item in custom_contacts:
        records.append({
            'key': f"custom:{item['emp_no']}",
            'source': 'custom_people',
            'source_label': '별도 조직',
            'group': item['custom_team'] or item['custom_department'] or '별도 조직',
            'name': item['name'],
            'role': item['position'],
            'organization': item['custom_department'] or item['department'],
            'phone': item['phone'],
            'email': item['email'],
            'detail': item['custom_team'],
            'custom_department': item['custom_department'],
            'custom_team': item['custom_team'],
        })
    for item in schools:
        records.append({
            'key': f"school:{item['id']}",
            'source': 'schools',
            'source_label': '학교',
            'group': '학교',
            'name': item['school_name'],
            'role': item['contract_subject'],
            'organization': item['office_location'],
            'phone': item['office_phone'],
            'email': item['email'],
            'detail': item['director_name'],
        })
    return records


def _school_options(conn):
    """센터장 담당학교 배정 모달에서 고를 수 있는 학교 목록(id, 이름)."""
    school_cols = _columns(conn, 'schools')
    query = "SELECT id, school_name FROM schools"
    if 'is_active' in school_cols:
        query += " WHERE COALESCE(is_active, 1) = 1"
    query += " ORDER BY school_name ASC"
    return [dict(row) for row in conn.execute(query).fetchall()]


def _load_school_contacts(conn):
    school_cols = _columns(conn, 'schools')
    has_is_active = 'is_active' in school_cols
    address_expr = "s.school_address" if 'school_address' in school_cols else "s.office_location"
    office_location_expr = "s.office_location" if 'office_location' in school_cols else "''"
    school_phone_expr = "s.school_phone" if 'school_phone' in school_cols else "s.office_phone"
    school_email_expr = "s.school_email" if 'school_email' in school_cols else "''"
    contract_expr = "s.contract_subject" if 'contract_subject' in school_cols else "''"
    has_second_director = 'center_director_id_2' in school_cols
    second_director_fields = """
            u2.name AS director_name_2,
            u2.phone AS director_phone_2,
            u2.email AS director_email_2
    """ if has_second_director else """
            NULL AS director_name_2,
            NULL AS director_phone_2,
            NULL AS director_email_2
    """
    second_director_join = """
        LEFT JOIN users u2
          ON s.center_director_id_2 = u2.emp_no
         AND TRIM(COALESCE(u2.name, '')) NOT LIKE '관리자%'
    """ if has_second_director else ""

    query = f"""
        SELECT
            s.id,
            s.year,
            s.school_name,
            s.office_phone,
            s.neulbom_assistant,
            s.neulbom_manager,
            {office_location_expr} AS office_location,
            {address_expr} AS school_address,
            {school_phone_expr} AS school_phone,
            {school_email_expr} AS school_email,
            {contract_expr} AS contract_subject,
            u.name AS director_name,
            u.phone AS director_phone,
            u.email AS director_email,
            {second_director_fields}
        FROM schools s
        LEFT JOIN users u
          ON s.center_director_id = u.emp_no
         AND TRIM(COALESCE(u.name, '')) NOT LIKE '관리자%'
        {second_director_join}
    """
    if has_is_active:
        query += " WHERE COALESCE(s.is_active, 1) = 1"
    query += " ORDER BY s.school_name ASC, s.year DESC"

    rows = conn.execute(query).fetchall()
    contacts = []
    for row in rows:
        data = dict(row)
        contacts.append({
            'id': data.get('id'),
            'year': _dash(data.get('year')),
            'school_name': _dash(data.get('school_name')),
            'contract_subject': _dash(data.get('contract_subject')),
            'office_location': _dash(data.get('office_location')),
            'address': _dash(data.get('school_address')),
            'school_phone': _dash(data.get('school_phone')),
            'office_phone': _dash(data.get('office_phone')),
            'email': _dash(data.get('school_email')),
            'director_name': _dash(' · '.join(filter(None, [data.get('director_name'), data.get('director_name_2')]))),
            'director_phone': _dash(' · '.join(filter(None, [data.get('director_phone'), data.get('director_phone_2')]))),
            'director_email': _dash(' · '.join(filter(None, [data.get('director_email'), data.get('director_email_2')]))),
            'neulbom_manager': _dash(data.get('neulbom_manager')),
            'neulbom_assistant': _dash(data.get('neulbom_assistant')),
        })
    return contacts


def _is_contact_admin():
    return session.get('user_name') == 'admin' or int(session.get('user_level', 99)) <= 2


def _directory_owner():
    return _clean_text(session.get('emp_no') or session.get('user_name'))


def _init_saved_directory_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS saved_contact_directories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_emp_no TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_emp_no, name)
        )
    ''')
    conn.commit()


def _saved_directory_dict(row):
    data = dict(row)
    try:
        settings = json.loads(data.get('settings_json') or '{}')
    except (TypeError, ValueError):
        settings = {}
    return {
        'id': data['id'],
        'name': data['name'],
        'settings': settings,
        'created_at': data.get('created_at'),
        'updated_at': data.get('updated_at'),
    }


@contacts_bp.route('/contacts')
def contact_list():
    conn = get_db()
    _init_office_contact_table(conn)
    _init_center_team_table(conn)
    _ensure_contact_user_columns(conn)
    _init_saved_directory_table(conn)

    manual_contact_groups = _load_manual_contact_groups(conn)
    partner_manual_groups = [g for g in manual_contact_groups if g['key'] == 'partner']
    headquarters_positions, center_positions = _load_contact_position_groups(conn)
    headquarters_contacts = _load_user_contacts(conn, headquarters_positions)
    center_contacts = _load_center_contacts(conn, center_positions)
    school_contacts = _load_school_contacts(conn)
    school_options = _school_options(conn)
    custom_contacts = _load_custom_contacts(conn)
    center_contact_groups = _group_center_contacts(center_contacts)
    contact_builder_records = _builder_contact_records(
        manual_contact_groups, headquarters_contacts, center_contacts, school_contacts, custom_contacts
    )
    owner = _directory_owner()
    saved_directories = []
    if owner:
        saved_directories = [
            _saved_directory_dict(row)
            for row in conn.execute('''
                SELECT id, name, settings_json, created_at, updated_at
                FROM saved_contact_directories
                WHERE owner_emp_no = ?
                ORDER BY updated_at DESC, id DESC
            ''', (owner,)).fetchall()
        ]
    conn.close()

    return render_template(
        'contacts.html',
        partner_manual_groups=partner_manual_groups,
        headquarters_contacts=headquarters_contacts,
        center_contacts=center_contacts,
        center_contact_groups=center_contact_groups,
        school_contacts=school_contacts,
        school_options=school_options,
        contact_builder_records=contact_builder_records,
        custom_department_options=sorted({item['custom_department'] for item in custom_contacts if item['custom_department']}),
        custom_team_options=sorted({item['custom_team'] for item in custom_contacts if item['custom_team']}),
        saved_contact_directories=saved_directories,
        can_edit_defaults=_is_contact_admin(),
    )


@contacts_bp.route('/contacts/api/directories', methods=['GET', 'POST'])
def saved_directories_api():
    owner = _directory_owner()
    if not owner:
        return jsonify({'success': False, 'error': '로그인이 필요합니다.'}), 401
    conn = get_db()
    _init_saved_directory_table(conn)
    try:
        if request.method == 'GET':
            rows = conn.execute('''
                SELECT id, name, settings_json, created_at, updated_at
                FROM saved_contact_directories WHERE owner_emp_no = ?
                ORDER BY updated_at DESC, id DESC
            ''', (owner,)).fetchall()
            return jsonify({'success': True, 'directories': [_saved_directory_dict(row) for row in rows]})

        data = request.get_json(silent=True) or {}
        name = _clean_text(data.get('name'))
        settings = data.get('settings')
        if not name or len(name) > 80:
            return jsonify({'success': False, 'error': '연락망 이름은 1~80자로 입력해주세요.'}), 400
        if not isinstance(settings, dict):
            return jsonify({'success': False, 'error': '연락망 편집 정보가 올바르지 않습니다.'}), 400
        settings_json = json.dumps(settings, ensure_ascii=False)
        if len(settings_json.encode('utf-8')) > 200000:
            return jsonify({'success': False, 'error': '저장할 연락망 정보가 너무 큽니다.'}), 413
        try:
            cursor = conn.execute('''
                INSERT INTO saved_contact_directories (owner_emp_no, name, settings_json)
                VALUES (?, ?, ?)
            ''', (owner, name, settings_json))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if 'UNIQUE' in str(exc).upper():
                return jsonify({'success': False, 'error': '같은 이름의 저장 연락망이 있습니다.'}), 409
            raise
        row = conn.execute('''
            SELECT id, name, settings_json, created_at, updated_at
            FROM saved_contact_directories WHERE id = ?
        ''', (cursor.lastrowid,)).fetchone()
        return jsonify({'success': True, 'message': '맞춤 연락망을 저장했습니다.', 'directory': _saved_directory_dict(row)}), 201
    finally:
        conn.close()


@contacts_bp.route('/contacts/api/directories/<int:directory_id>', methods=['PUT', 'DELETE'])
def saved_directory_detail_api(directory_id):
    owner = _directory_owner()
    if not owner:
        return jsonify({'success': False, 'error': '로그인이 필요합니다.'}), 401
    conn = get_db()
    _init_saved_directory_table(conn)
    try:
        current = conn.execute('''
            SELECT id, name FROM saved_contact_directories
            WHERE id = ? AND owner_emp_no = ?
        ''', (directory_id, owner)).fetchone()
        if not current:
            return jsonify({'success': False, 'error': '저장된 연락망을 찾을 수 없습니다.'}), 404
        if request.method == 'DELETE':
            conn.execute("DELETE FROM saved_contact_directories WHERE id = ? AND owner_emp_no = ?", (directory_id, owner))
            conn.commit()
            return jsonify({'success': True, 'message': '저장 연락망을 삭제했습니다.'})

        data = request.get_json(silent=True) or {}
        name = _clean_text(data.get('name'))
        settings = data.get('settings')
        if not name or len(name) > 80 or not isinstance(settings, dict):
            return jsonify({'success': False, 'error': '연락망 이름과 편집 정보를 확인해주세요.'}), 400
        settings_json = json.dumps(settings, ensure_ascii=False)
        if len(settings_json.encode('utf-8')) > 200000:
            return jsonify({'success': False, 'error': '저장할 연락망 정보가 너무 큽니다.'}), 413
        try:
            conn.execute('''
                UPDATE saved_contact_directories
                SET name = ?, settings_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND owner_emp_no = ?
            ''', (name, settings_json, directory_id, owner))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if 'UNIQUE' in str(exc).upper():
                return jsonify({'success': False, 'error': '같은 이름의 저장 연락망이 있습니다.'}), 409
            raise
        row = conn.execute('''
            SELECT id, name, settings_json, created_at, updated_at
            FROM saved_contact_directories WHERE id = ?
        ''', (directory_id,)).fetchone()
        return jsonify({'success': True, 'message': '맞춤 연락망 수정 내용을 저장했습니다.', 'directory': _saved_directory_dict(row)})
    finally:
        conn.close()


def _apply_center_team(conn, emp_no, raw_team_no):
    """emp_no의 팀 지정을 갱신한다. 값이 비어있으면 지정을 해제한다."""
    if raw_team_no in (None, '', 0, '0'):
        conn.execute("DELETE FROM contact_center_teams WHERE emp_no = ?", (emp_no,))
        return None
    team_no = int(raw_team_no)
    if team_no < 1 or team_no > 20:
        raise ValueError('팀 번호는 1팀부터 20팀까지 지정할 수 있습니다.')
    conn.execute("""
        INSERT INTO contact_center_teams (emp_no, team_no, updated_at)
        VALUES (?, ?, datetime('now', 'localtime'))
        ON CONFLICT(emp_no) DO UPDATE SET
            team_no = excluded.team_no,
            updated_at = excluded.updated_at
    """, (emp_no, team_no))
    return team_no


def _apply_center_school_assignment(conn, emp_no, school_ids):
    """emp_no가 담당하는 학교 목록을 school_ids(학교 id 집합)에 맞춰 갱신한다.

    schools 테이블은 학교마다 center_director_id(주)/center_director_id_2(부) 두 자리만
    있으므로, 이미 다른 센터장이 두 자리를 모두 채운 학교는 배정하지 못하고 건너뛴다.
    """
    school_cols = _columns(conn, 'schools')
    has_second = 'center_director_id_2' in school_cols

    if has_second:
        current_rows = conn.execute(
            "SELECT id, center_director_id, center_director_id_2 FROM schools "
            "WHERE center_director_id=? OR center_director_id_2=?",
            (emp_no, emp_no)
        ).fetchall()
    else:
        current_rows = conn.execute(
            "SELECT id, center_director_id FROM schools WHERE center_director_id=?",
            (emp_no,)
        ).fetchall()
    current_ids = {row['id'] for row in current_rows}

    for school_id in current_ids - school_ids:
        row = conn.execute("SELECT * FROM schools WHERE id=?", (school_id,)).fetchone()
        if not row:
            continue
        if row['center_director_id'] == emp_no:
            conn.execute("UPDATE schools SET center_director_id=NULL WHERE id=?", (school_id,))
        elif has_second and row['center_director_id_2'] == emp_no:
            conn.execute("UPDATE schools SET center_director_id_2=NULL WHERE id=?", (school_id,))

    skipped = 0
    for school_id in school_ids - current_ids:
        row = conn.execute("SELECT * FROM schools WHERE id=?", (school_id,)).fetchone()
        if not row:
            continue
        if not row['center_director_id']:
            conn.execute("UPDATE schools SET center_director_id=? WHERE id=?", (emp_no, school_id))
        elif has_second and not row['center_director_id_2']:
            conn.execute("UPDATE schools SET center_director_id_2=? WHERE id=?", (emp_no, school_id))
        else:
            skipped += 1
    return skipped


@contacts_bp.route('/contacts/center-assign', methods=['POST'])
def save_center_assign():
    if not _is_contact_admin():
        abort(403)

    data = request.get_json(silent=True) or {}
    emp_no = _clean_text(data.get('emp_no'))
    if not emp_no:
        return jsonify({'success': False, 'error': '센터장 정보가 없습니다.'}), 400

    school_ids = data.get('school_ids')
    if not isinstance(school_ids, list):
        return jsonify({'success': False, 'error': '담당학교 목록이 올바르지 않습니다.'}), 400
    try:
        school_ids = {int(value) for value in school_ids}
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '담당학교 목록이 올바르지 않습니다.'}), 400

    conn = get_db()
    _init_center_team_table(conn)
    _, center_positions = _load_contact_position_groups(conn)
    placeholders = ','.join(['?'] * len(center_positions))
    user = conn.execute(
        f"SELECT emp_no FROM users WHERE emp_no = ? AND position IN ({placeholders})",
        (emp_no, *center_positions),
    ).fetchone()
    if not user:
        conn.close()
        return jsonify({'success': False, 'error': '배정할 센터장을 찾을 수 없습니다.'}), 404

    try:
        _apply_center_team(conn, emp_no, data.get('team_no'))
    except ValueError as exc:
        conn.close()
        return jsonify({'success': False, 'error': str(exc)}), 400

    skipped = _apply_center_school_assignment(conn, emp_no, school_ids)
    conn.commit()
    conn.close()

    message = '담당학교·팀 배정을 저장했습니다.'
    if skipped:
        message += f' (이미 센터장 정원이 찬 {skipped}개 학교는 배정하지 못했습니다.)'
    return jsonify({'success': True, 'message': message})


@contacts_bp.route('/contacts/center-assign/clear', methods=['POST'])
def clear_center_assign():
    if not _is_contact_admin():
        abort(403)

    data = request.get_json(silent=True) or {}
    emp_no = _clean_text(data.get('emp_no'))
    if not emp_no:
        return jsonify({'success': False, 'error': '센터장 정보가 없습니다.'}), 400

    conn = get_db()
    school_cols = _columns(conn, 'schools')
    conn.execute("UPDATE schools SET center_director_id=NULL WHERE center_director_id=?", (emp_no,))
    if 'center_director_id_2' in school_cols:
        conn.execute("UPDATE schools SET center_director_id_2=NULL WHERE center_director_id_2=?", (emp_no,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': '담당학교 배정을 해제했습니다.'})


@contacts_bp.route('/contacts/card')
def business_card():
    card = {
        'name': _dash(request.args.get('name')),
        'position': _dash(request.args.get('position')),
        'department': _dash(request.args.get('department')),
        'phone': _dash(request.args.get('phone')),
        'email': _dash(request.args.get('email')),
    }
    return render_template_string("""
<!doctype html>
<html lang="ko">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ card.name }} 명함</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css">
    
    <!-- PDF 및 캡처를 위한 라이브러리 추가 -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
    
    <style>
        body { margin: 0; font-family: 'Pretendard', sans-serif; background: #f1f5f9; display: flex; align-items: center; justify-content: center; height: 100vh; flex-direction: column; overflow: hidden; }
        .card-wrap { padding: 10px 20px; }
        .card {
            background-color: #fff;
            width: 450px;
            height: 250px;
            box-sizing: border-box;
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            display: flex;
            padding: 30px 25px;
            position: relative;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            overflow: hidden;
            background-position: center;
        }
        /* 배경 설정 시 글씨 가독성을 높이기 위한 반투명 레이어 */
        .card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(255, 255, 255, 0.75);
            z-index: 0;
            pointer-events: none;
            display: none;
        }
        .card.has-bg::before { display: block; }
        .left-col { width: 44%; display: flex; flex-direction: column; justify-content: center; align-items: center; padding-right: 15px; position: relative; z-index: 1; }
        .logo { max-width: 100%; height: auto; margin-bottom: 24px; }
        .motto { background: #eef2f6; color: #005AAB; font-size: 11px; font-weight: 800; padding: 7px 14px; border-radius: 16px; position: relative; letter-spacing: -0.5px; }
        .motto::after { content: ''; position: absolute; top: 100%; left: 18px; border-width: 8px 8px 0 0; border-style: solid; border-color: #eef2f6 transparent transparent transparent; }
        .right-col { width: 56%; display: flex; flex-direction: column; justify-content: center; padding-left: 20px; position: relative; z-index: 1; }
        .position { font-size: 13px; color: #555; margin-bottom: 4px; font-weight: 600; display: flex; align-items: baseline; gap: 8px; letter-spacing: -0.5px; }
        .dept { font-size: 11px; color: #94a3b8; font-weight: 500; }
        .name { font-size: 24px; font-weight: 900; color: #222; letter-spacing: 8px; margin-bottom: 10px; }
        .company { font-size: 16px; font-weight: 800; color: #333; margin-bottom: 16px; letter-spacing: -0.5px; }
        .address { font-size: 11px; color: #666; line-height: 1.5; margin-bottom: 16px; letter-spacing: -0.3px; }
        .contact-info { display: flex; flex-direction: column; gap: 4px; }
        .info-line { font-size: 12px; color: #555; display: flex; align-items: center; letter-spacing: 0.2px; }
        .info-line .label { font-weight: 900; color: #333; width: 18px; margin-right: 4px; font-size: 13px; }
        
        .controls-wrapper { width: 450px; margin-top: 10px; display: flex; flex-direction: column; gap: 10px; }
        .bg-controls, .actions { display: flex; gap: 8px; align-items: center; justify-content: space-between; }
        .bg-controls label { font-size: 13px; font-weight: 700; color: #334155; white-space: nowrap;}
        .bg-controls select { flex: 1; padding: 7px; border-radius: 6px; border: 1px solid #cbd5e1; font-family: inherit; font-size: 12px; outline: none; }
        .bg-controls input[type="color"] { border: 1px solid #cbd5e1; border-radius: 4px; padding: 0; width: 28px; height: 28px; cursor: pointer; background: #fff;}
        
        .custom-bg-list { display: flex; gap: 8px; margin-top: 5px; flex-wrap: wrap; }
        .bg-thumb { width: 36px; height: 36px; border-radius: 6px; background-size: cover; background-position: center; cursor: pointer; border: 2px solid transparent; position: relative; box-shadow: 0 2px 4px rgba(0,0,0,0.1); transition: 0.2s;}
        .bg-thumb:hover { border-color: #004ea2; transform: translateY(-2px); }
        .bg-thumb .del-btn { position: absolute; top: -6px; right: -6px; background: #ef4444; color: white; border: none; border-radius: 50%; width: 16px; height: 16px; font-size: 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
        .bg-thumb .del-btn:hover { background: #dc2626; }
        
        .email-form { display: none; gap: 8px; width: 100%; margin-top: 5px; }
        .email-form input { flex: 1; padding: 8px; border: 1px solid #cbd5e1; border-radius: 6px; outline: none; font-size: 12px; }
        
        button { border: 1px solid #cbd5e1; background: #fff; border-radius: 6px; padding: 8px 12px; cursor: pointer; font-weight: 700; color: #334155; font-size: 12px; transition: all 0.2s; white-space: nowrap;}
        button:hover { background: #f8fafc; }
        button.primary { background: #004ea2; border-color: #004ea2; color: #fff; }
        button.primary:hover { background: #003377; border-color: #003377; }
        
        @media print {
            body { background: #fff; display: block; height: auto; }
            .card-wrap { padding: 0; }
            .controls-wrapper { display: none !important; }
            .card { box-shadow: none; border: 1px solid #e2e8f0; }
            .card { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
        }
    </style>
</head>
<body>
    <div class="card-wrap">
        <div class="card" id="business-card">
            <div class="left-col">
                <img src="{{ url_for('static', filename='logo01.gif') }}" alt="새담 로고" class="logo" onerror="this.src='https://via.placeholder.com/150x80?text=Logo'">
                <div class="motto">방과후학교 프로그램 위탁운영</div>
            </div>
            <div class="right-col">
                <div class="position">{{ card.position }} <span class="dept">{{ card.department }}</span></div>
                <div class="name">{{ card.name }}</div>
                <div class="company">(사)새담청소년교육문화원</div>
                <div class="address">
                    수원시 영통구 하동 1016-1<br>
                    SK뷰 레이크타워 A동 1015호
                </div>
                <div class="contact-info">
                    <div class="info-line"><span class="label">T</span> 031-8016-1900</div>
                    <div class="info-line"><span class="label">F</span> 050-4225-3268</div>
                    <div class="info-line"><span class="label">M</span> {{ card.phone }}</div>
                    <div class="info-line"><span class="label">E</span> {{ card.email }}</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="controls-wrapper">
        <div class="bg-controls">
            <label for="bg-preset">배경 이미지:</label>
            <select id="bg-preset" onchange="changePreset(this)">
                <option value="" data-type="cover">기본 (배경없음)</option>
                <option value="https://www.transparenttextures.com/patterns/cubes.png" data-type="pattern">패턴 (큐브)</option>
                <option value="https://www.transparenttextures.com/patterns/stardust.png" data-type="pattern">패턴 (스타더스트)</option>
                <option value="https://images.unsplash.com/photo-1579546929518-9e396f3cc809?w=500&q=80" data-type="cover">그라데이션 (화사함)</option>
                <option value="https://images.unsplash.com/photo-1557683316-973673baf926?w=500&q=80" data-type="cover">그라데이션 (차분함)</option>
                <option value="custom" data-type="cover">+ 내 이미지 업로드...</option>
            </select>
            <input type="file" id="bg-upload" accept="image/*" style="display:none;" onchange="uploadCustomBackground(this)">
            
            <label for="bg-color" style="margin-left: 10px;">단색 배경:</label>
            <input type="color" id="bg-color" onchange="applyBgColor(this.value)" value="#ffffff" title="단색 배경색 선택">
        </div>
        
        <!-- 내 커스텀 배경 리스트 (최대 10개) -->
        <div class="custom-bg-list" id="custom-bg-list"></div>
        
        <div class="actions">
            <div>
                <button type="button" onclick="savePDF()"><i class="fa-solid fa-file-pdf"></i> PDF저장</button>
                <button type="button" class="primary" onclick="toggleEmailForm()"><i class="fa-solid fa-envelope"></i> 이메일전송</button>
            </div>
            <div>
                <button type="button" class="primary" onclick="window.print()"><i class="fa-solid fa-print"></i> 인쇄</button>
                <button type="button" onclick="window.close()">닫기</button>
            </div>
        </div>
        
        <div class="email-form" id="email-form">
            <input type="email" id="target-email" placeholder="받을 이메일 주소 입력">
            <button type="button" class="primary" onclick="sendEmail()">전송</button>
        </div>
    </div>

    <script>
        const MAX_BGS = 10;
        let myCustomBgs = JSON.parse(localStorage.getItem('saedamBgs') || '[]');

        // 로드 시 내 커스텀 배경 렌더링
        window.onload = () => {
            renderCustomBgs();
        };

        function changePreset(select) {
            const val = select.value;
            const type = select.options[select.selectedIndex].dataset.type;
            
            if (val === 'custom') {
                document.getElementById('bg-upload').click();
                select.value = ''; // 초기화
            } else {
                applyBg(val, type);
            }
        }
        
        // 새로 추가된 단색 배경 적용 함수
        function applyBgColor(color) {
            const card = document.getElementById('business-card');
            card.style.backgroundImage = 'none';
            card.style.backgroundColor = color;
            card.classList.remove('has-bg');
            document.getElementById('bg-preset').value = ''; // 이미지 선택 초기화
        }

        function applyBg(url, type) {
            const card = document.getElementById('business-card');
            if (!url) {
                card.style.backgroundImage = 'none';
                card.style.backgroundColor = '#ffffff';
                document.getElementById('bg-color').value = '#ffffff';
                card.classList.remove('has-bg');
                return;
            }
            card.style.backgroundImage = `url('${url}')`;
            card.classList.add('has-bg');

            // 패턴은 반복, 커버는 꽉 차게 설정 분기
            if (type === 'pattern') {
                card.style.backgroundSize = 'auto';
                card.style.backgroundRepeat = 'repeat';
            } else {
                card.style.backgroundSize = 'cover';
                card.style.backgroundRepeat = 'no-repeat';
            }
            // 이미지가 적용될 경우 배경색은 흰색으로 리셋
            card.style.backgroundColor = '#ffffff';
            document.getElementById('bg-color').value = '#ffffff';
        }

        // 10개 커스텀 배경 업로드 및 최적화 저장 기능 (에러 처리 강화 및 리사이즈 축소)
        function uploadCustomBackground(input) {
            if (myCustomBgs.length >= MAX_BGS) {
                alert(`최대 ${MAX_BGS}개까지만 등록할 수 있습니다. 기존 배경을 삭제해주세요.`);
                input.value = '';
                return;
            }
            if (input.files && input.files[0]) {
                const reader = new FileReader();
                reader.onload = function(e) {
                    // 용량 관리를 위한 캔버스 리사이징 (최대 600px로 축소)
                    const img = new Image();
                    img.onload = function() {
                        const canvas = document.createElement('canvas');
                        const MAX_WIDTH = 600;
                        const MAX_HEIGHT = 600;
                        let width = img.width;
                        let height = img.height;

                        if (width > height) {
                            if (width > MAX_WIDTH) { height *= MAX_WIDTH / width; width = MAX_WIDTH; }
                        } else {
                            if (height > MAX_HEIGHT) { width *= MAX_HEIGHT / height; height = MAX_HEIGHT; }
                        }
                        canvas.width = width;
                        canvas.height = height;
                        const ctx = canvas.getContext('2d');
                        ctx.drawImage(img, 0, 0, width, height);
                        
                        // 용량을 낮추기 위해 jpeg 0.7 포맷으로 압축률 상향
                        const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
                        
                        try {
                            myCustomBgs.push(dataUrl);
                            localStorage.setItem('saedamBgs', JSON.stringify(myCustomBgs));
                            renderCustomBgs();
                            applyBg(dataUrl, 'cover');
                        } catch (err) {
                            alert('저장 공간이 부족합니다. 기존 등록된 이미지를 삭제한 후 다시 시도해주세요.');
                            myCustomBgs.pop();
                        }
                    };
                    img.src = e.target.result;
                }
                reader.readAsDataURL(input.files[0]);
            }
            input.value = '';
        }

        function renderCustomBgs() {
            const container = document.getElementById('custom-bg-list');
            container.innerHTML = '';
            myCustomBgs.forEach((bg, idx) => {
                const div = document.createElement('div');
                div.className = 'bg-thumb';
                div.style.backgroundImage = `url(${bg})`;
                div.title = "클릭하여 배경 적용";
                div.onclick = () => applyBg(bg, 'cover');

                const delBtn = document.createElement('button');
                delBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
                delBtn.className = 'del-btn';
                delBtn.title = "삭제";
                delBtn.onclick = (e) => {
                    e.stopPropagation();
                    if(confirm('이 커스텀 배경을 삭제하시겠습니까?')) {
                        myCustomBgs.splice(idx, 1);
                        localStorage.setItem('saedamBgs', JSON.stringify(myCustomBgs));
                        renderCustomBgs();
                        // 만약 현재 적용된 배경을 지운 경우
                        applyBg('', 'cover'); 
                    }
                };
                div.appendChild(delBtn);
                container.appendChild(div);
            });
        }

        // PDF 다운로드 기능
        function savePDF() {
            const { jsPDF } = window.jspdf;
            html2canvas(document.getElementById('business-card'), {scale: 3, useCORS: true, allowTaint: false, backgroundColor: null}).then(canvas => {
                const imgData = canvas.toDataURL('image/png');
                const pdf = new jsPDF('l', 'mm', [90, 50]);
                pdf.addImage(imgData, 'PNG', 0, 0, 90, 50);
                pdf.save('{{ card.name }}_명함.pdf');
            }).catch(err => alert('PDF를 생성하지 못했습니다. 보안 정책(CORS)이 적용된 외부 이미지 배경 대신, 기본 배경이나 직접 업로드한 이미지를 사용해 주세요.'));
        }

        // 이메일 발송 기능
        function toggleEmailForm() {
            const form = document.getElementById('email-form');
            form.style.display = (form.style.display === 'none' || form.style.display === '') ? 'flex' : 'none';
        }

        // 이메일 캔버스 캡처 예외 처리(CORS 이슈 대비) 보강
        function sendEmail() {
            const targetEmail = document.getElementById('target-email').value;
            if(!targetEmail) { alert('이메일 주소를 입력해주세요.'); return; }
            
            html2canvas(document.getElementById('business-card'), {scale: 2, useCORS: true, allowTaint: false}).then(canvas => {
                let imgData = '';
                try {
                    imgData = canvas.toDataURL('image/jpeg', 0.9);
                } catch (err) {
                    alert('명함 이미지를 캡처할 수 없습니다. 보안 정책이 엄격한 외부 이미지 배경 대신 단색 배경이나 직접 업로드한 이미지를 사용해 주세요.');
                    return;
                }
                
                fetch('/contacts/card/email', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email: targetEmail,
                        image: imgData,
                        name: {{ card.name|tojson }},
                        position: {{ card.position|tojson }},
                        department: {{ card.department|tojson }},
                        phone: {{ card.phone|tojson }},
                        card_email: {{ card.email|tojson }}
                    })
                })
                .then(res => res.json())
                .then(data => {
                    if(data.success) { 
                        alert('명함이 해당 이메일로 성공적으로 전송되었습니다.'); 
                        toggleEmailForm();
                        document.getElementById('target-email').value = '';
                    } else { 
                        alert('메일 전송에 실패했습니다.' + (data.error ? '\\n' + data.error : '')); 
                    }
                })
                .catch(err => alert('서버와의 통신 오류가 발생했습니다. 네트워크를 확인해주세요.'));
            }).catch(err => alert('명함 이미지를 생성하지 못했습니다. 배경 이미지를 확인해주세요. (일부 외부 이미지는 보안 정책상 캡처가 차단됩니다)'));
        }
    </script>
</body>
</html>
    """, card=card)


@contacts_bp.route('/contacts/card/email', methods=['POST'])
def send_card_email():
    data = request.get_json(silent=True) or {}
    success, error = _send_business_card_email(
        data.get('email'),
        data.get('name'),
        data.get('position'),
        data.get('department'),
        data.get('phone'),
        data.get('card_email') or data.get('contact_email') or data.get('email_on_card'),
        data.get('image'),
    )
    status = 200 if success else 400
    return jsonify({'success': success, 'error': error}), status


@contacts_bp.route('/contacts/manual', methods=['POST'])
def save_manual_contact():
    if not _is_contact_admin():
        abort(403)

    conn = get_db()
    _init_office_contact_table(conn)
    contact_id = (request.form.get('contact_id') or '').strip()
    category_key = (request.form.get('category_key') or 'partner').strip()
    category_name = MANUAL_CONTACT_GROUP_MAP.get(category_key, '협력사')

    values = (
        category_key,
        category_name,
        request.form.get('organization_name', '').strip(),
        request.form.get('role_title', '').strip(),
        request.form.get('person_name', '').strip(),
        request.form.get('address', '').strip(),
        request.form.get('phone', '').strip(),
        request.form.get('fax', '').strip(),
        request.form.get('email', '').strip(),
        request.form.get('extra_contact', '').strip(),
        request.form.get('memo', '').strip(),
    )

    if contact_id:
        conn.execute("""
            UPDATE office_contact_entries
            SET category_key = ?, category_name = ?, organization_name = ?, role_title = ?,
                person_name = ?, address = ?, phone = ?, fax = ?, email = ?, extra_contact = ?, memo = ?,
                updated_at = datetime('now', 'localtime')
            WHERE id = ?
        """, (*values, contact_id))
    else:
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM office_contact_entries WHERE category_key = ?",
            (category_key,)
        ).fetchone()[0]
        conn.execute("""
            INSERT INTO office_contact_entries
                (category_key, category_name, organization_name, role_title, person_name, address,
                 phone, fax, email, extra_contact, memo, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (*values, next_order))

    conn.commit()
    conn.close()
    return redirect(url_for('contacts.contact_list', notice='contact_saved'))


@contacts_bp.route('/contacts/manual/<int:contact_id>/delete', methods=['POST'])
def delete_manual_contact(contact_id):
    if not _is_contact_admin():
        abort(403)

    conn = get_db()
    _init_office_contact_table(conn)
    conn.execute("DELETE FROM office_contact_entries WHERE id = ?", (contact_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('contacts.contact_list', notice='contact_deleted'))
