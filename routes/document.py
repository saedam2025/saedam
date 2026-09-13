import os
import pdfkit
import yagmail
import smtplib
import shutil
import platform
import hmac
import secrets
import base64
import mimetypes
import tempfile
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash, abort
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from jinja2 import Template
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from .ai_mail import _csrf_required, _csrf_token, _owner_emp_no
from .database import (
    ensure_certificate_schema,
    get_db,
    migrate_legacy_certificates,
)
from .payroll import (
    _ensure_sender_schema,
    _payroll_sender_dict,
    _sender_from_header,
    _smtp_login_for_sender,
    _verify_smtp_sender,
)
from .security import menu_permission_required
from .storage import APP_ROOT, DATA_ROOT
from .secure_files import (
    delete_file,
    encrypted_response,
    encrypted_storage_name,
    encrypt_stream,
    encrypt_upload,
    original_filename,
    read_decrypted,
    temporary_decrypted_path,
)

# Blueprint 설정
document_bp = Blueprint('document', __name__)

# 한국 시간 설정 함수
def now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

# --- [경로 및 환경 설정] ---
BASE_DIR = str(DATA_ROOT)
PDF_FOLDER = os.path.join(BASE_DIR, "output_pdfs")       # 생성된 PDF 보관 폴더
CERT_SEAL_FOLDER = os.path.join(BASE_DIR, "certificate_seals")
CERT_LOGO_FOLDER = os.path.join(BASE_DIR, "certificate_logos")
SEAL_IMAGE = str(APP_ROOT / "static" / "seal.gif") # 도장 이미지
CERT_BACKGROUND_IMAGE = str(APP_ROOT / "static" / "cer_bg.png")

# 템플릿 경로 설정
TEMPLATE_PATH = str(APP_ROOT / "templates" / "certificate" / "certificate_template.html")

os.makedirs(PDF_FOLDER, exist_ok=True)
os.makedirs(CERT_SEAL_FOLDER, exist_ok=True)
os.makedirs(CERT_LOGO_FOLDER, exist_ok=True)

ADMIN_NOTIFICATION_EMAIL = "edu197@naver.com"
CERTIFICATE_FORM_PASSWORD = "0070"
CERTIFICATE_FORM_SESSION_KEYS = {
    "document.apply": "certificate_apply_verified",
    "document.apply2": "certificate_apply2_verified",
    "document.apply_excellent": "certificate_apply_excellent_verified",
}
CERTIFICATE_FORM_AUTH_TOKEN = "certificate-form-v2"

# =====================================================================
# [수정된 부분: PDF 엔진 설정] - 윈도우 에러 방지 처리 추가
# =====================================================================
if platform.system() == 'Windows':
    # 윈도우 로컬 환경에서는 기본적으로 C드라이브 경로를 찾거나, 못 찾으면 None으로 처리
    WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
else:
    # Render (리눅스) 환경
    WKHTMLTOPDF_PATH = shutil.which("wkhtmltopdf") or "/usr/bin/wkhtmltopdf"

try:
    PDF_CONFIG = pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)
except OSError:
    PDF_CONFIG = None
    print("⚠️ [안내] wkhtmltopdf 실행 파일을 찾을 수 없어 PDF 변환 기능이 비활성화됩니다. (기본 앱 실행에는 문제 없음)")
# =====================================================================

# --- [환경변수 이름 contract.py와 동일하게 통일] ---
def get_email_credentials():
    email = os.environ.get("MAIL_USERNAME", "")
    pw = os.environ.get("MAIL_PASSWORD", "")
    return email.strip(), pw.strip()


def require_certificate_form_password(workgroup=None, company=None):
    if session.get("emp_no"):
        return None

    session_key = CERTIFICATE_FORM_SESSION_KEYS.get(
        request.endpoint,
        "certificate_form_verified",
    )
    if session.get(session_key) == CERTIFICATE_FORM_AUTH_TOKEN:
        return None

    if request.method == "POST":
        supplied_password = str(request.form.get("password", ""))
        if hmac.compare_digest(supplied_password, CERTIFICATE_FORM_PASSWORD):
            session[session_key] = CERTIFICATE_FORM_AUTH_TOKEN
            return redirect(request.path)
        flash("비밀번호가 올바르지 않습니다.")

    return render_template(
        "certificate/form_login.html",
        certificate_workgroup=dict(workgroup) if workgroup else None,
        certificate_company=dict(company) if company else None,
    )


def clear_certificate_form_password():
    if session.get("emp_no"):
        return
    session_key = CERTIFICATE_FORM_SESSION_KEYS.get(request.endpoint)
    if session_key:
        session.pop(session_key, None)


def certificate_form_template_context(
    workgroup=None, company=None, force_standalone=False,
):
    """증명서 신청 폼의 레이아웃 정보를 만든다.

    force_standalone=True 면 사내 사용자로 로그인한 상태여도 인트라넷 테마를
    입히지 않고 외부 신청자와 똑같은 독립 화면으로 보여준다.
    """
    is_intranet_user = bool(session.get("emp_no"))
    try:
        user_level = int(session.get("user_level", 99))
    except (TypeError, ValueError):
        user_level = 99
    use_intranet_layout = (
        not force_standalone and is_intranet_user and user_level <= 5
    )
    return {
        "certificate_layout": (
            "base.html"
            if use_intranet_layout
            else "certificate/form_standalone_base.html"
        ),
        "intranet_layout": use_intranet_layout,
        "current_user_level": user_level,
        "certificate_workgroup": dict(workgroup) if workgroup else None,
        "certificate_company": dict(company) if company else None,
    }


def _certificate_workgroup_by_token(token, applicant_type):
    """공개 신청 링크의 활성 작업그룹과 회사를 조회한다."""
    if not token:
        return None, None
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        row = conn.execute('''
            SELECT w.*, c.company_name, c.representative_name, c.business_number,
                   c.address, c.phone, c.seal_path, c.logo_filename, c.logo_path
            FROM certificate_workgroups w
            JOIN certificate_companies c ON c.id=w.company_id
            WHERE w.access_token=? AND w.is_active=1 AND c.is_active=1
        ''', (str(token).strip(),)).fetchone()
        if not row:
            return None, None
        if applicant_type == '우수강사':
            allowed = row['allow_excellent_instructor']
        elif applicant_type == '강사':
            allowed = row['allow_instructor']
        else:
            allowed = row['allow_employee']
        if not allowed:
            return None, None
        company = {
            key: row[key]
            for key in (
                'company_id', 'company_name', 'representative_name',
                'business_number', 'address', 'phone', 'seal_path',
                'logo_filename', 'logo_path',
            )
        }
        company['id'] = company.pop('company_id')
        company['logo_url'] = (
            url_for(
                'document.company_logo', company_id=company['id'],
                v=os.path.basename(company['logo_path']),
            )
            if company.get('logo_path') else ''
        )
        return row, company
    finally:
        conn.close()


# --- [내부 데이터베이스 관리 함수] ---
CERTIFICATE_FIELD_MAP = {
    '신청일': 'applied_date',
    '신청구분': 'applicant_type',
    '증명서종류': 'certificate_type',
    '성명': 'applicant_name',
    '주민번호': 'resident_number',
    '자택주소': 'home_address',
    '근무시작일': 'work_start_date',
    '근무종료일': 'work_end_date',
    '근무장소': 'workplace',
    '강의과목': 'subject_or_duty',
    '용도': 'purpose',
    '직책': 'position',
    '이메일주소': 'email',
    '상태': 'status',
    '발급일': 'issued_date',
    '발급번호': 'issue_number',
    '종료사유': 'termination_reason',
    '파일명': 'filename',
}


def ensure_db_initialized():
    """증명발급 테이블과 기존 엑셀 데이터의 1회 이관을 보장한다."""
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        migrate_legacy_certificates(conn)
        conn.commit()
    finally:
        conn.close()


def _clean_certificate_value(value):
    return '' if value is None else str(value).strip()


def _certificate_record(row):
    source = dict(row)
    result = {
        korean_name: _clean_certificate_value(source.get(db_name, ''))
        for korean_name, db_name in CERTIFICATE_FIELD_MAP.items()
    }
    result['index'] = int(source['id'])
    result['id'] = int(source['id'])
    result['작업그룹ID'] = source.get('workgroup_id')
    result['회사ID'] = source.get('company_id')
    result['작업그룹명'] = _clean_certificate_value(source.get('workgroup_name'))
    result['회사명'] = _clean_certificate_value(source.get('company_name'))
    return result


def _insert_certificate_request(form_data, conn=None, commit=True):
    values = {
        field: _clean_certificate_value(form_data.get(field, ''))
        for field in CERTIFICATE_FIELD_MAP
    }
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        cursor = conn.execute('''
            INSERT INTO certificate_requests (
                applied_date, applicant_type, certificate_type,
                applicant_name, resident_number, home_address,
                work_start_date, work_end_date, workplace,
                subject_or_duty, purpose, position, email, status,
                issued_date, issue_number, termination_reason, filename,
                workgroup_id, company_id, workgroup_name, company_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            *tuple(values[field] for field in CERTIFICATE_FIELD_MAP),
            form_data.get('_workgroup_id'), form_data.get('_company_id'),
            _clean_certificate_value(form_data.get('_workgroup_name')),
            _clean_certificate_value(form_data.get('_company_name')),
        ))
        if commit:
            conn.commit()
        return int(cursor.lastrowid)
    finally:
        if owns_connection:
            conn.close()

def get_next_issue_number():
    """연도별 발급 번호 자동 생성"""
    year_prefix = now_kst().strftime('%y')
    num_file = os.path.join(BASE_DIR, f"last_cert_num_{year_prefix}.txt")
    
    last_num = 0
    if os.path.exists(num_file):
        with open(num_file, 'r') as f:
            try: last_num = int(f.read().strip())
            except: last_num = 0
    
    next_num = last_num + 1
    with open(num_file, 'w') as f:
        f.write(str(next_num))
    
    return f"제{year_prefix}-{next_num:04d}호"


def validate_dismissal_end_date(form_data):
    """해촉증명서는 실제 계약 종료일이 반드시 있어야 한다."""
    cert_type = str(form_data.get("증명서종류", "")).strip()
    end_date_type = str(form_data.get("종료일선택", "")).strip()
    end_date = str(form_data.get("근무종료일", "")).strip()

    if "해촉증명서" in cert_type and (
        end_date_type == "현재까지"
        or end_date == "현재까지"
        or not end_date
    ):
        return "해촉증명서는 '현재까지'로 신청할 수 없습니다. 정확한 계약 종료일을 입력해 주세요."
    return None


def _normalize_resident_number(value):
    """주민번호 비교용으로 숫자만 남긴다."""
    return re.sub(r'\D', '', _clean_certificate_value(value))


def _format_resident_number(value):
    digits = _normalize_resident_number(value)
    return f'{digits[:6]}-{digits[6:]}' if len(digits) == 13 else digits


def _unique_join(values):
    unique = []
    for value in values:
        cleaned = _clean_certificate_value(value)
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    return ', '.join(unique)


def _validity_period(valid_from, valid_until):
    start_text = _clean_certificate_value(valid_from)
    end_text = _clean_certificate_value(valid_until)
    if not start_text or not end_text:
        raise ValueError('명단 유효 시작일과 종료일을 모두 입력해 주세요.')
    try:
        start_date = datetime.strptime(start_text, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_text, '%Y-%m-%d').date()
    except ValueError as exc:
        raise ValueError('명단 유효기간의 날짜 형식을 확인해 주세요.') from exc
    if end_date < start_date:
        raise ValueError('명단 유효 종료일은 시작일보다 빠를 수 없습니다.')
    return start_date.isoformat(), end_date.isoformat()


def _excellent_group_status(row):
    group = dict(row)
    today = now_kst().date().isoformat()
    group['is_valid'] = bool(
        group.get('is_active') and group.get('valid_from')
        and group.get('valid_until')
        and group['valid_from'] <= today <= group['valid_until']
    )
    group['is_upcoming'] = bool(
        group.get('is_active') and group.get('valid_from')
        and today < group['valid_from']
    )
    group['today'] = today
    return group


def _excellent_roster_summary(conn):
    today = now_kst().date().isoformat()
    row = conn.execute('''
        SELECT
            (SELECT COUNT(*) FROM excellent_instructor_roster_groups
             WHERE is_active=1) AS group_count,
            (SELECT COUNT(*) FROM excellent_instructor_roster_groups
             WHERE is_active=1 AND valid_from<=? AND valid_until>=?) AS valid_group_count,
            (SELECT COUNT(*) FROM excellent_instructor_eligibility e
             JOIN excellent_instructor_roster_groups g ON g.id=e.group_id
             WHERE e.is_active=1 AND g.is_active=1) AS member_count
    ''', (today, today)).fetchone()
    return dict(row)


def _masked_resident_number(value):
    digits = _normalize_resident_number(value)
    if len(digits) != 13:
        return ''
    return f'{digits[:6]}-{digits[6]}******'


# --- [외부 라우트: 강사 신청용] ---
@document_bp.route('/apply', defaults={'token': None}, methods=['GET', 'POST'])
@document_bp.route('/apply/<token>', methods=['GET', 'POST'])
def apply(token=None):
    ensure_db_initialized()
    workgroup, company = _certificate_workgroup_by_token(token, '강사')
    if token and not workgroup:
        return render_template(
            'certificate/link_unavailable.html',
            message='사용할 수 없거나 종료된 강사 증명서 신청 링크입니다.',
        ), 404
    login_response = require_certificate_form_password(workgroup, company)
    if login_response is not None:
        return login_response
    if request.method == 'POST':
        try:
            form_data = dict(request.form)
            validation_error = validate_dismissal_end_date(form_data)
            if validation_error:
                return validation_error, 400

            if form_data.get("종료일선택") == "현재까지":
                form_data["근무종료일"] = "현재까지"
            
            form_data["신청일"] = now_kst().strftime("%Y-%m-%d")
            form_data["상태"] = "대기"
            form_data["발급일"] = ""
            form_data["발급번호"] = ""
            form_data["파일명"] = ""
            form_data.pop("종료일선택", None)
            if workgroup:
                form_data['_workgroup_id'] = int(workgroup['id'])
                form_data['_company_id'] = int(workgroup['company_id'])
                form_data['_workgroup_name'] = workgroup['name']
                form_data['_company_name'] = workgroup['company_name']

            _insert_certificate_request(form_data)

            send_admin_alert(
                form_data['성명'], form_data['증명서종류'], role="강사님",
                sender_id=workgroup['sender_id'] if workgroup else None,
                company_name=workgroup['company_name'] if workgroup else '',
            )
            clear_certificate_form_password()
            return render_template(
                'certificate/success.html', data=form_data,
                certificate_workgroup=dict(workgroup) if workgroup else None,
                certificate_company=company,
            )
        except Exception as e:
            return f"신청 중 오류가 발생했습니다: {str(e)}", 500
            
    return render_template(
        'certificate/form.html',
        **certificate_form_template_context(workgroup, company),
    )

# --- [외부 라우트: 임직원 신청용] ---
@document_bp.route('/apply2', defaults={'token': None}, methods=['GET', 'POST'])
@document_bp.route('/apply2/<token>', methods=['GET', 'POST'])
def apply2(token=None):
    ensure_db_initialized()
    workgroup, company = _certificate_workgroup_by_token(token, '임직원')
    if token and not workgroup:
        return render_template(
            'certificate/link_unavailable.html',
            message='사용할 수 없거나 종료된 임직원 증명서 신청 링크입니다.',
        ), 404
    login_response = require_certificate_form_password(workgroup, company)
    if login_response is not None:
        return login_response
    if request.method == 'POST':
        try:
            form_data = dict(request.form)
            validation_error = validate_dismissal_end_date(form_data)
            if validation_error:
                return validation_error, 400

            if form_data.get("종료일선택") == "현재까지":
                form_data["근무종료일"] = "현재까지"
            
            form_data["신청일"] = now_kst().strftime("%Y-%m-%d")
            form_data["상태"] = "대기"
            form_data["발급일"] = ""
            form_data["발급번호"] = ""
            form_data["파일명"] = ""
            form_data.pop("종료일선택", None)
            if workgroup:
                form_data['_workgroup_id'] = int(workgroup['id'])
                form_data['_company_id'] = int(workgroup['company_id'])
                form_data['_workgroup_name'] = workgroup['name']
                form_data['_company_name'] = workgroup['company_name']

            _insert_certificate_request(form_data)

            # 관리자 알림 시 임직원임을 명시
            send_admin_alert(
                form_data['성명'], form_data['증명서종류'], role="임직원",
                sender_id=workgroup['sender_id'] if workgroup else None,
                company_name=workgroup['company_name'] if workgroup else '',
            )
            clear_certificate_form_password()
            return render_template(
                'certificate/success.html', data=form_data,
                certificate_workgroup=dict(workgroup) if workgroup else None,
                certificate_company=company,
            )
        except Exception as e:
            return f"신청 중 오류가 발생했습니다: {str(e)}", 500
            
    return render_template(
        'certificate/form2.html',
        **certificate_form_template_context(workgroup, company),
    )


# --- [외부 라우트: 우수강사인증서 전용 신청] ---
@document_bp.route('/apply-excellent/<token>', methods=['GET', 'POST'])
def apply_excellent(token):
    ensure_db_initialized()
    workgroup, company = _certificate_workgroup_by_token(token, '우수강사')
    if not workgroup:
        return render_template(
            'certificate/link_unavailable.html',
            message='사용할 수 없거나 종료된 우수강사인증서 신청 링크입니다.',
        ), 404

    login_response = require_certificate_form_password(workgroup, company)
    if login_response is not None:
        return login_response

    if request.method == 'POST':
        applicant_name = _clean_certificate_value(request.form.get('성명'))
        resident_number_normalized = _normalize_resident_number(
            request.form.get('주민번호')
        )
        selected_ids = [
            int(value) for value in request.form.getlist('대상항목')
            if str(value).isdigit()
        ]
        if not applicant_name or len(resident_number_normalized) != 13:
            return '성명과 주민등록번호를 정확히 입력해 주세요.', 400
        if not selected_ids:
            return '신청할 학교와 강의과목을 한 건 선택해 주세요.', 400
        if len(set(selected_ids)) != 1:
            return (
                '한번에 한 건씩 발급 가능합니다. '
                '한 건을 신청한 후 다른 건을 신청해 주세요.'
            ), 400
        required_fields = (
            '용도', '이메일주소', '자택주소', '근무시작일', '종료일선택',
        )
        if any(not _clean_certificate_value(request.form.get(field)) for field in required_fields):
            return '필수 신청 정보를 모두 입력해 주세요.', 400
        if request.form.get('종료일선택') != '현재까지' and not request.form.get('근무종료일'):
            return '근무 종료일을 입력해 주세요.', 400

        placeholders = ','.join('?' for _ in selected_ids)
        conn = get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            today = now_kst().date().isoformat()
            rows = conn.execute(f'''
                SELECT e.id, e.group_id, e.school_name, e.position, e.subject,
                       g.name AS group_name
                FROM excellent_instructor_eligibility e
                JOIN excellent_instructor_roster_groups g ON g.id=e.group_id
                WHERE e.applicant_name=? AND e.resident_number_normalized=?
                  AND e.id IN ({placeholders}) AND e.is_active=1
                  AND g.company_id=?
                  AND g.is_active=1 AND g.valid_from<=? AND g.valid_until>=?
                ORDER BY e.id
            ''', (
                applicant_name, resident_number_normalized, *selected_ids,
                int(workgroup['company_id']),
                today, today,
            )).fetchall()
            if len(rows) != len(set(selected_ids)):
                conn.rollback()
                return '선택한 신청 대상 정보를 확인할 수 없습니다. 다시 조회해 주세요.', 400

            selected_group_ids = sorted({int(row['group_id']) for row in rows})
            group_placeholders = ','.join('?' for _ in selected_group_ids)
            duplicate = conn.execute(f'''
                SELECT rg.group_id, g.name, r.status
                FROM excellent_instructor_request_groups rg
                JOIN certificate_requests r ON r.id=rg.request_id
                JOIN excellent_instructor_roster_groups g ON g.id=rg.group_id
                WHERE rg.applicant_name=?
                  AND rg.resident_number_normalized=?
                  AND rg.group_id IN ({group_placeholders})
                  AND REPLACE(r.certificate_type, ' ', '')='우수강사인증서'
                ORDER BY CASE WHEN r.status='발급완료' THEN 0 ELSE 1 END, r.id DESC
                LIMIT 1
            ''', (
                applicant_name, resident_number_normalized, *selected_group_ids,
            )).fetchone()
            if duplicate:
                conn.rollback()
                duplicate_status = (
                    '발급완료' if duplicate['status'] == '발급완료' else '신청중'
                )
                return (
                    f'{duplicate["name"]} 그룹은 이미 {duplicate_status}입니다. '
                    '신청 대상을 다시 조회해 주세요.'
                ), 409

            form_data = dict(request.form)
            form_data.update({
                '신청일': now_kst().strftime('%Y-%m-%d'),
                '신청구분': '강사',
                '증명서종류': '우수강사인증서',
                '성명': applicant_name,
                '주민번호': _format_resident_number(resident_number_normalized),
                '근무장소': _unique_join(row['school_name'] for row in rows),
                '직책': _unique_join(row['position'] for row in rows),
                '강의과목': _unique_join(row['subject'] for row in rows),
                '근무종료일': (
                    '현재까지' if request.form.get('종료일선택') == '현재까지'
                    else _clean_certificate_value(request.form.get('근무종료일'))
                ),
                '상태': '대기',
                '발급일': '',
                '발급번호': '',
                '파일명': '',
                '_workgroup_id': int(workgroup['id']),
                '_company_id': int(workgroup['company_id']),
                '_workgroup_name': workgroup['name'],
                '_company_name': workgroup['company_name'],
            })
            form_data.pop('종료일선택', None)
            form_data.pop('대상항목', None)
            request_id = _insert_certificate_request(
                form_data, conn=conn, commit=False,
            )
            conn.executemany('''
                INSERT INTO excellent_instructor_request_groups (
                    request_id, group_id, applicant_name,
                    resident_number_normalized
                ) VALUES (?, ?, ?, ?)
            ''', [(
                request_id, group_id, applicant_name,
                resident_number_normalized,
            ) for group_id in selected_group_ids])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        send_admin_alert(
            applicant_name, '우수강사인증서', role='강사님',
            sender_id=workgroup['sender_id'],
            company_name=workgroup['company_name'],
        )
        clear_certificate_form_password()
        return render_template(
            'certificate/success.html', data=form_data,
            certificate_workgroup=dict(workgroup), certificate_company=company,
        )

    return render_template(
        'certificate/excellent_form.html',
        lookup_url=url_for('document.lookup_excellent_instructor', token=token),
        # 우수강사인증서 신청은 외부인이 쓰는 화면이라 사내 테마를 입히지 않는다.
        **certificate_form_template_context(
            workgroup, company, force_standalone=True,
        ),
    )


@document_bp.post('/apply-excellent/<token>/lookup')
def lookup_excellent_instructor(token):
    ensure_db_initialized()
    workgroup, _company = _certificate_workgroup_by_token(token, '우수강사')
    if not workgroup:
        return jsonify({'status': 'error', 'message': '사용할 수 없는 신청 링크입니다.'}), 404
    if not session.get('emp_no') and session.get(
        CERTIFICATE_FORM_SESSION_KEYS['document.apply_excellent']
    ) != CERTIFICATE_FORM_AUTH_TOKEN:
        return jsonify({'status': 'error', 'message': '신청 비밀번호 인증이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    applicant_name = _clean_certificate_value(data.get('name'))
    resident_number_normalized = _normalize_resident_number(data.get('resident_number'))
    if not applicant_name or len(resident_number_normalized) != 13:
        return jsonify({
            'status': 'error',
            'message': '성명과 주민등록번호를 정확히 입력해 주세요.',
        }), 400

    conn = get_db()
    try:
        today = now_kst().date().isoformat()
        rows = conn.execute('''
            SELECT e.id, e.group_id, e.school_name, e.position, e.subject,
                   g.name AS group_name, g.valid_from, g.valid_until
            FROM excellent_instructor_eligibility e
            JOIN excellent_instructor_roster_groups g ON g.id=e.group_id
            WHERE e.applicant_name=? AND e.resident_number_normalized=?
              AND e.is_active=1 AND g.is_active=1 AND g.company_id=?
            ORDER BY g.valid_from DESC, g.id DESC,
                     e.school_name, e.subject, e.id
        ''', (
            applicant_name, resident_number_normalized,
            int(workgroup['company_id']),
        )).fetchall()
        history_rows = conn.execute('''
            SELECT rg.group_id, r.id AS request_id, r.status AS request_status,
                   r.applied_date, r.issued_date, r.issue_number
            FROM excellent_instructor_request_groups rg
            JOIN certificate_requests r ON r.id=rg.request_id
            JOIN excellent_instructor_roster_groups g ON g.id=rg.group_id
            WHERE rg.applicant_name=?
              AND rg.resident_number_normalized=?
              AND g.company_id=?
              AND REPLACE(r.certificate_type, ' ', '')='우수강사인증서'
            ORDER BY CASE WHEN r.status='발급완료' THEN 0 ELSE 1 END,
                     r.id DESC
        ''', (
            applicant_name, resident_number_normalized,
            int(workgroup['company_id']),
        )).fetchall()
    finally:
        conn.close()
    if not rows:
        return jsonify({
            'status': 'error',
            'message': '우수강사인증서 신청 대상자가 아닙니다.',
        }), 404
    history_by_group = {}
    for history in history_rows:
        history_by_group.setdefault(int(history['group_id']), dict(history))

    groups_by_id = {}
    for row in rows:
        item = dict(row)
        group_id = int(item.pop('group_id'))
        group = groups_by_id.setdefault(group_id, {
            'id': group_id,
            'name': item.pop('group_name'),
            'valid_from': item.pop('valid_from'),
            'valid_until': item.pop('valid_until'),
            'items': [],
        })
        group['items'].append(item)

    status_counts = {
        'available': 0, 'pending': 0, 'issued': 0,
        'upcoming': 0, 'expired': 0,
    }
    status_labels = {
        'available': '발급 가능', 'pending': '신청중',
        'issued': '발급완료', 'upcoming': '신청기간 전',
        'expired': '신청기간 만료',
    }
    for group in groups_by_id.values():
        history = history_by_group.get(group['id'])
        if history and history['request_status'] == '발급완료':
            group_status = 'issued'
        elif history:
            group_status = 'pending'
        elif group['valid_from'] <= today <= group['valid_until']:
            group_status = 'available'
        elif today < group['valid_from']:
            group_status = 'upcoming'
        else:
            group_status = 'expired'
        group['application_status'] = group_status
        group['status_label'] = status_labels[group_status]
        group['selectable'] = group_status == 'available'
        group['history'] = history
        status_counts[group_status] += 1

    return jsonify({
        'status': 'success',
        'groups': list(groups_by_id.values()),
        'summary': status_counts,
    })


# --- [내부 라우트: 관리자용] ---
@document_bp.route('/admin')
@menu_permission_required("document_admin")
def admin_list():
    """인트라넷 관리자용 신청 현황 목록 (페이징 및 검색 추가)"""
    if 'emp_no' not in session:
        return redirect(url_for('login_page'))
    
    ensure_db_initialized()
    page = request.args.get('page', 1, type=int)
    per_page = 10
    search_keyword = request.args.get('search', '').strip()
    where_sql = ''
    where_params = []
    if search_keyword:
        where_sql = '''
            WHERE applicant_name LIKE ?
               OR workplace LIKE ?
               OR subject_or_duty LIKE ?
               OR purpose LIKE ?
               OR certificate_type LIKE ?
               OR position LIKE ?
        '''
        like_keyword = f'%{search_keyword}%'
        where_params = [like_keyword] * 6

    conn = get_db()
    try:
        stats = conn.execute('''
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='대기' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='경력증명서' THEN 1 ELSE 0 END) AS career,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='재직증명서' THEN 1 ELSE 0 END) AS employment,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='해촉증명서' THEN 1 ELSE 0 END) AS dismissal,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='강사활동증명서' THEN 1 ELSE 0 END) AS activity,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='강사해촉증명서' THEN 1 ELSE 0 END) AS instructor_dismissal,
                SUM(CASE WHEN REPLACE(certificate_type, ' ', '')='우수강사인증서' THEN 1 ELSE 0 END) AS excellent
            FROM certificate_requests
        ''').fetchone()
        filtered_count = int(conn.execute(
            f'SELECT COUNT(*) FROM certificate_requests {where_sql}',
            where_params,
        ).fetchone()[0])
        total_pages = (filtered_count + per_page - 1) // per_page
        if page < 1:
            page = 1
        if page > total_pages and total_pages > 0:
            page = total_pages
        offset = (page - 1) * per_page
        rows = conn.execute(
            f'''
                SELECT * FROM certificate_requests
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            ''',
            [*where_params, per_page, offset],
        ).fetchall()
        paginated_submissions = [_certificate_record(row) for row in rows]
        workgroups = conn.execute('''
            SELECT w.id, w.name, w.company_id, w.sender_id, w.access_token,
                   w.allow_instructor, w.allow_employee,
                   w.allow_excellent_instructor, c.company_name
            FROM certificate_workgroups w
            JOIN certificate_companies c ON c.id=w.company_id
            WHERE w.is_active=1 AND c.is_active=1
            ORDER BY c.company_name, w.name
        ''').fetchall()
    finally:
        conn.close()

    block_size = 10
    current_block = (page - 1) // block_size + 1
    start_page = (current_block - 1) * block_size + 1
    end_page = min(start_page + block_size - 1, total_pages)
    
    return render_template('certificate/admin.html', 
                           submissions=paginated_submissions,
                           total=int(stats['total'] or 0),
                           pending=int(stats['pending'] or 0),
                           count_career=int(stats['career'] or 0),
                           count_employment=int(stats['employment'] or 0),
                           count_dismissal=int(stats['dismissal'] or 0),
                           count_activity=int(stats['activity'] or 0),
                           count_inst_dismissal=int(stats['instructor_dismissal'] or 0),
                           count_excellent=int(stats['excellent'] or 0),
                           page=page,
                           total_pages=total_pages,
                           start_page=start_page,
                           end_page=end_page,
                           workgroups=[dict(row) for row in workgroups])


def _workgroup_bundle(conn, workgroup_id):
    if not workgroup_id:
        return None
    row = conn.execute('''
        SELECT w.*, c.company_name, c.representative_name, c.business_number,
               c.address, c.phone, c.seal_filename, c.seal_path
        FROM certificate_workgroups w
        JOIN certificate_companies c ON c.id=w.company_id
        WHERE w.id=? AND w.is_active=1 AND c.is_active=1
    ''', (int(workgroup_id),)).fetchone()
    return dict(row) if row else None

@document_bp.route('/generate/<int:idx>')
@menu_permission_required("document_admin")
def generate_certificate(idx):
    """관리자가 발급 버튼을 눌렀을 때 실행"""
    if 'emp_no' not in session: return abort(403)
    
    conn = None
    pdf_path = None
    try:
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM certificate_requests WHERE id=?',
            (idx,),
        ).fetchone()
        if not row:
            flash("데이터를 찾을 수 없습니다.")
            return redirect(url_for('document.admin_list'))

        record = _certificate_record(row)
        if record['상태'] == '발급완료':
            flash("이미 발급이 완료된 요청입니다.")
            return redirect(url_for('document.admin_list'))

        selected_group_id = request.args.get('workgroup_id', type=int) or record.get('작업그룹ID')
        bundle = _workgroup_bundle(conn, selected_group_id)
        configured_group_count = int(conn.execute(
            'SELECT COUNT(*) FROM certificate_workgroups WHERE is_active=1'
        ).fetchone()[0])
        if configured_group_count and not bundle:
            flash('발송할 작업그룹(회사·발송계정)을 먼저 선택해 주세요.')
            return redirect(url_for('document.admin_list'))
        if bundle:
            applicant_type = record.get('신청구분', '')
            is_excellent = record.get('증명서종류') == '우수강사인증서'
            if is_excellent and not bundle.get('allow_excellent_instructor'):
                flash('선택한 작업그룹은 우수강사인증서 발급을 허용하지 않습니다.')
                return redirect(url_for('document.admin_list'))
            if applicant_type == '강사' and not is_excellent and not bundle.get('allow_instructor'):
                flash('선택한 작업그룹은 강사 증명서 발급을 허용하지 않습니다.')
                return redirect(url_for('document.admin_list'))
            if applicant_type == '임직원' and not bundle.get('allow_employee'):
                flash('선택한 작업그룹은 임직원 증명서 발급을 허용하지 않습니다.')
                return redirect(url_for('document.admin_list'))

        sender = None
        if bundle and bundle.get('sender_id'):
            sender_row = conn.execute(
                'SELECT * FROM ai_mail_senders WHERE id=? AND is_active=1',
                (bundle['sender_id'],),
            ).fetchone()
            sender = dict(sender_row) if sender_row else None
            if not sender:
                flash('작업그룹에 연결된 발송계정을 사용할 수 없습니다. 작업그룹 설정을 확인해 주세요.')
                return redirect(url_for('document.admin_list'))

        issue_no = get_next_issue_number()
        pdf_path = create_pdf_file(record, issue_no, company=bundle)
        
        # 메일 발송 전 DB 상태를 먼저 업데이트하여 발급 자체는 보존한다.
        conn.execute('''
            UPDATE certificate_requests
            SET status='발급완료',
                issued_date=?,
                issue_number=?,
                filename=?,
                workgroup_id=?, company_id=?, workgroup_name=?, company_name=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (
            now_kst().strftime("%Y-%m-%d"),
            issue_no,
            os.path.basename(pdf_path),
            bundle['id'] if bundle else None,
            bundle['company_id'] if bundle else None,
            bundle['name'] if bundle else '',
            bundle['company_name'] if bundle else '',
            idx,
        ))
        conn.commit()

        # 메일 발송 (성공 여부와 상세 에러 메시지 반환)
        mail_success, err_msg = send_email_to_instructor(
            record.get('이메일주소', ''),
            record['성명'],
            pdf_path,
            record['증명서종류'],
            sender=sender,
            company=bundle,
        )
        
        if mail_success:
            flash(f"{record['성명']} 님께 증명서 발송을 완료했습니다.")
        else:
            flash(f"발급은 완료되었으나, 메일 전송이 실패했습니다.\n사유: {err_msg}")
            
    except Exception as e:
        if pdf_path:
            delete_file(pdf_path)
        flash(f"발급 중 오류 발생: {str(e)}")
    finally:
        if conn is not None:
            conn.close()
        
    return redirect(url_for('document.admin_list'))

# --- [보조 기능 함수들] ---
def create_pdf_file(row, issue_no, company=None):
    """HTML 템플릿을 읽어 PDF 파일 생성"""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = Template(f.read())

    data = dict(row)
    
    ssn = str(data.get('주민번호', '')).replace("-", "")
    if len(ssn) >= 7:
        masked_ssn = f"{ssn[:6]}-{ssn[6]}******"
    else:
        masked_ssn = ssn
    data['주민번호'] = masked_ssn

    data['발급번호'] = issue_no
    data['발급일자'] = now_kst().strftime("%Y년 %m월 %d일")
    cert_type = ''.join(str(data.get('증명서종류', '')).split())
    data['우수강사인증서여부'] = cert_type == '우수강사인증서'
    data['인증서배경'] = f"file:///{os.path.abspath(CERT_BACKGROUND_IMAGE).replace(os.sep, '/')}"
    company = company or {}
    data['발급회사명'] = company.get('company_name') or '사단법인 새담청소년교육문화원'
    data['대표자명'] = company.get('representative_name') or ''
    data['사업자등록번호'] = company.get('business_number') or '144-82-00397'
    data['회사주소'] = company.get('address') or '경기도 수원시 팔달구 매산로116번길 18'
    data['회사연락처'] = company.get('phone') or '031-8016-1900'

    html_content = template.render(**data)

    seal_path = company.get('seal_path') or SEAL_IMAGE
    if company.get('seal_path') and os.path.isfile(seal_path):
        mime_type = mimetypes.guess_type(company.get('seal_filename') or '')[0] or 'image/png'
        seal_uri = f"data:{mime_type};base64,{base64.b64encode(read_decrypted(seal_path)).decode('ascii')}"
    else:
        seal_uri = f"file:///{os.path.abspath(seal_path).replace(os.sep, '/')}"
    html_content = html_content.replace('src="seal.gif"', f'src="{seal_uri}"')

    file_name = f"{issue_no}_{row['성명']}.pdf".replace("/", "_")
    output_path = os.path.join(PDF_FOLDER, file_name)
    descriptor, temporary_path = tempfile.mkstemp(prefix='saedam-certificate-', suffix='.pdf')
    os.close(descriptor)
    
    options = {
        'enable-local-file-access': None,
        'background': None,
        'encoding': 'UTF-8',
        'margin-top': '0', 'margin-bottom': '0', 'margin-left': '0', 'margin-right': '0'
    }
    
    # 윈도우에서 PDF_CONFIG가 없어도(None) 에러가 나지 않도록 조건 처리
    try:
        if PDF_CONFIG:
            pdfkit.from_string(html_content, temporary_path, configuration=PDF_CONFIG, options=options)
        else:
            # PDF_CONFIG가 없다면 (로컬 개발 환경) 테스트 내용을 임시파일에 생성한다.
            with open(temporary_path, "w", encoding="utf-8") as text_file:
                text_file.write("PDF 생성 환경이 설정되지 않았습니다. (로컬 테스트용 텍스트 파일)")
        with open(temporary_path, 'rb') as source:
            encrypt_stream(source, output_path)
    finally:
        delete_file(temporary_path)
        
    return output_path

# --- [이중 방어벽이 적용된 이메일 발송 함수들] ---
def _send_registered_message(sender, message):
    smtp = _smtp_login_for_sender(sender)
    try:
        _verify_smtp_sender(smtp, sender)
        # ZeptoMail은 헤더에서 추론한 주소보다 인증 도메인의 envelope-from을
        # 명시했을 때 안정적으로 발송된다. 스마트명세서와 같은 주소를 사용한다.
        smtp.send_message(
            message,
            from_addr=str(sender.get('email') or '').strip().lower(),
        )
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()


def send_email_to_instructor(to_email, name, pdf_path, cert_type, sender=None, company=None):
    """작업그룹의 발송계정으로 생성된 증명서를 첨부 발송한다."""
    if not to_email or str(to_email).strip() == "":
        return False, "수신자의 이메일 주소가 비어있습니다."
        
    company_name = (company or {}).get('company_name') or '새담청소년교육문화원'
    subject = f"[{company_name}] 요청하신 {cert_type} 발송 안내 ({name} 님)"
    contents = f"{name} 님, 안녕하세요.\n\n{company_name}입니다.\n요청하신 {cert_type}를 첨부파일로 보내드립니다.\n\n감사합니다."

    if sender:
        try:
            msg = MIMEMultipart()
            msg['From'] = _sender_from_header(sender)
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(contents, 'plain', 'utf-8'))
            part = MIMEApplication(read_decrypted(pdf_path), _subtype="pdf")
            part.add_header(
                'Content-Disposition', 'attachment',
                filename=os.path.basename(pdf_path),
            )
            msg.attach(part)
            _send_registered_message(sender, msg)
            return True, ""
        except Exception as exc:
            return False, f"등록 발송계정 메일 전송 실패: {str(exc)}"

    email_addr, email_pw = get_email_credentials()
    if not email_addr or not email_pw:
        return False, "작업그룹 발송계정 또는 서버 메일 환경변수가 설정되지 않았습니다."
    
    # [1차 시도] yagmail 사용
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        with temporary_decrypted_path(pdf_path, os.path.basename(pdf_path)) as temporary_pdf:
            yag.send(to=to_email, subject=subject, contents=contents, attachments=[temporary_pdf])
        return True, ""
    except Exception as yag_err:
        # [2차 시도] yagmail 실패 시 smtplib 포트 587 (TLS) 직접 연결 방식으로 우회 발송
        try:
            msg = MIMEMultipart()
            msg['From'] = email_addr
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(contents, 'plain'))
            
            part = MIMEApplication(read_decrypted(pdf_path), _subtype="pdf")
            part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_path))
            msg.attach(part)
                
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()  # 보안 연결
            server.login(email_addr, email_pw)
            server.send_message(msg)
            server.quit()
            return True, ""
        except Exception as smtp_err:
            return False, f"서버 거부 (yagmail: {str(yag_err)} / smtplib: {str(smtp_err)})"

def send_admin_alert(name, cert_type, role="강사님", sender_id=None, company_name=''):
    """신청 발생 시 관리자에게 간단 알림"""
    sender = None
    if sender_id:
        conn = get_db()
        try:
            _ensure_sender_schema(conn)
            row = conn.execute(
                'SELECT * FROM ai_mail_senders WHERE id=? AND is_active=1',
                (int(sender_id),),
            ).fetchone()
            sender = dict(row) if row else None
        finally:
            conn.close()
    try:
        subject = f"[신청접수] {name} {role} - {cert_type}"
        contents = f"새로운 증명서 신청이 들어왔습니다.\n\n발급회사: {company_name or '기본'}\n신청자: {name} ({role})\n종류: {cert_type}\n인트라넷에서 확인 후 발급해 주세요."
        if sender:
            msg = MIMEText(contents, 'plain', 'utf-8')
            msg['From'] = _sender_from_header(sender)
            msg['To'] = ADMIN_NOTIFICATION_EMAIL
            msg['Subject'] = subject
            _send_registered_message(sender, msg)
            return
        email_addr, email_pw = get_email_credentials()
        if not email_addr or not email_pw:
            return
        yag = yagmail.SMTP(email_addr, email_pw)
        yag.send(to=ADMIN_NOTIFICATION_EMAIL, subject=subject, contents=contents)
    except Exception:
        pass


def _certificate_settings_payload():
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        _ensure_sender_schema(conn)
        companies = conn.execute('''
            SELECT c.*,
                   (SELECT COUNT(*) FROM certificate_workgroups w
                    WHERE w.company_id=c.id AND w.is_active=1) AS workgroup_count
            FROM certificate_companies c
            WHERE c.is_active=1
            ORDER BY c.updated_at DESC, c.id DESC
        ''').fetchall()
        workgroups = conn.execute('''
            SELECT w.*, c.company_name, c.representative_name,
                   s.label AS sender_label, s.email AS sender_email,
                   COALESCE(s.provider, 'gmail') AS sender_provider
            FROM certificate_workgroups w
            JOIN certificate_companies c ON c.id=w.company_id
            LEFT JOIN ai_mail_senders s ON s.id=w.sender_id
            WHERE w.is_active=1 AND c.is_active=1
            ORDER BY w.updated_at DESC, w.id DESC
        ''').fetchall()
        senders = conn.execute('''
            SELECT * FROM ai_mail_senders
            WHERE owner_emp_no=? AND is_active=1
            ORDER BY updated_at DESC, id DESC
        ''', (_owner_emp_no(),)).fetchall()
        excellent_roster_summary = _excellent_roster_summary(conn)
        company_items = []
        for row in companies:
            item = dict(row)
            item['seal_url'] = (
                url_for(
                    'document.company_seal', company_id=item['id'],
                    v=os.path.basename(item['seal_path']),
                )
                if item.get('seal_path') else ''
            )
            item['logo_url'] = (
                url_for(
                    'document.company_logo', company_id=item['id'],
                    v=os.path.basename(item['logo_path']),
                )
                if item.get('logo_path') else ''
            )
            company_items.append(item)
        group_items = []
        for row in workgroups:
            item = dict(row)
            token = item['access_token']
            item['instructor_path'] = url_for('document.apply', token=token)
            item['employee_path'] = url_for('document.apply2', token=token)
            item['excellent_instructor_path'] = url_for(
                'document.apply_excellent', token=token,
            )
            group_items.append(item)
        return {
            'companies': company_items,
            'workgroups': group_items,
            'senders': [_payroll_sender_dict(row) for row in senders],
            'excellent_roster_summary': excellent_roster_summary,
            'csrf_token': _csrf_token(),
        }
    finally:
        conn.close()


@document_bp.route('/admin/settings')
@menu_permission_required("document_admin")
def certificate_settings():
    return render_template('certificate/settings.html')


@document_bp.route('/api/settings')
@menu_permission_required("document_admin")
def certificate_settings_api():
    return jsonify({'status': 'success', **_certificate_settings_payload()})


@document_bp.get('/api/excellent-instructor-groups')
@menu_permission_required("document_admin")
def excellent_instructor_groups_api():
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        rows = conn.execute('''
            SELECT g.*, c.company_name,
                   COUNT(CASE WHEN e.is_active=1 THEN 1 END) AS member_count
            FROM excellent_instructor_roster_groups g
            LEFT JOIN excellent_instructor_eligibility e ON e.group_id=g.id
            LEFT JOIN certificate_companies c ON c.id=g.company_id
            WHERE g.is_active=1
            GROUP BY g.id
            ORDER BY g.created_at DESC, g.id DESC
        ''').fetchall()
        creator_names = {}
        has_users_table = conn.execute('''
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='users'
        ''').fetchone()
        creator_ids = sorted({
            _clean_certificate_value(row['created_by'])
            for row in rows if _clean_certificate_value(row['created_by'])
        })
        if has_users_table and creator_ids:
            placeholders = ','.join('?' for _ in creator_ids)
            creator_names = {
                str(row['emp_no']): _clean_certificate_value(row['name'])
                for row in conn.execute(f'''
                    SELECT emp_no, name FROM users
                    WHERE CAST(emp_no AS TEXT) IN ({placeholders})
                ''', creator_ids).fetchall()
            }
    finally:
        conn.close()
    groups = []
    for row in rows:
        group = dict(row)
        creator_id = _clean_certificate_value(group.get('created_by'))
        group['created_by_name'] = creator_names.get(
            creator_id, creator_id or '알 수 없음',
        )
        group['created_date'] = _clean_certificate_value(
            group.get('created_at')
        )[:10]
        groups.append(_excellent_group_status(group))
    return jsonify({
        'status': 'success',
        'groups': groups,
    })


@document_bp.get('/api/excellent-instructor-groups/<int:group_id>/members')
@menu_permission_required("document_admin")
def excellent_instructor_group_members_api(group_id):
    page = max(request.args.get('page', 1, type=int), 1)
    per_page = 50
    search = _clean_certificate_value(request.args.get('q'))
    conditions = ['group_id=?', 'is_active=1']
    params = [group_id]
    if search:
        pattern = f'%{search}%'
        conditions.append('''(
            applicant_name LIKE ? OR school_name LIKE ?
            OR position LIKE ? OR subject LIKE ?
        )''')
        params.extend([pattern, pattern, pattern, pattern])
    where_sql = ' AND '.join(conditions)
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        group_row = conn.execute('''
            SELECT * FROM excellent_instructor_roster_groups
            WHERE id=? AND is_active=1
        ''', (group_id,)).fetchone()
        if not group_row:
            return jsonify({'status': 'error', 'message': '명단 그룹을 찾을 수 없습니다.'}), 404
        total = int(conn.execute(
            f'SELECT COUNT(*) FROM excellent_instructor_eligibility WHERE {where_sql}',
            params,
        ).fetchone()[0])
        total_pages = max((total + per_page - 1) // per_page, 1)
        page = min(page, total_pages)
        rows = conn.execute(f'''
            SELECT e.id, e.applicant_name, e.resident_number, e.school_name,
                   e.position, e.subject,
                   CASE
                     WHEN EXISTS (
                       SELECT 1
                       FROM excellent_instructor_request_groups rg
                       JOIN certificate_requests r ON r.id=rg.request_id
                       WHERE rg.group_id=e.group_id
                         AND rg.applicant_name=e.applicant_name
                         AND rg.resident_number_normalized=e.resident_number_normalized
                         AND REPLACE(r.certificate_type, ' ', '')='우수강사인증서'
                         AND r.status='발급완료'
                     ) THEN '발급완료'
                     WHEN EXISTS (
                       SELECT 1
                       FROM excellent_instructor_request_groups rg
                       JOIN certificate_requests r ON r.id=rg.request_id
                       WHERE rg.group_id=e.group_id
                         AND rg.applicant_name=e.applicant_name
                         AND rg.resident_number_normalized=e.resident_number_normalized
                         AND REPLACE(r.certificate_type, ' ', '')='우수강사인증서'
                     ) THEN '신청중'
                     ELSE '미신청'
                   END AS application_status
            FROM excellent_instructor_eligibility e
            WHERE {where_sql}
            ORDER BY e.applicant_name, e.school_name, e.subject, e.id
            LIMIT ? OFFSET ?
        ''', (*params, per_page, (page - 1) * per_page)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        item['resident_number_masked'] = _masked_resident_number(item['resident_number'])
        items.append(item)
    return jsonify({
        'status': 'success', 'group': _excellent_group_status(group_row),
        'items': items, 'total': total, 'page': page,
        'total_pages': total_pages,
    })


@document_bp.route('/api/excellent-instructor-groups/<int:group_id>', methods=['PATCH', 'DELETE'])
@menu_permission_required("document_admin")
@_csrf_required
def update_excellent_instructor_group(group_id):
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        group_row = conn.execute('''
            SELECT * FROM excellent_instructor_roster_groups
            WHERE id=? AND is_active=1
        ''', (group_id,)).fetchone()
        if not group_row:
            return jsonify({'status': 'error', 'message': '명단 그룹을 찾을 수 없습니다.'}), 404
        if request.method == 'DELETE':
            conn.execute('''
                UPDATE excellent_instructor_roster_groups
                SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?
            ''', (group_id,))
            conn.commit()
            return jsonify({'status': 'success', 'message': '명단 그룹을 삭제했습니다.'})
        data = request.get_json(silent=True) or {}
        name = _clean_certificate_value(data.get('name'))
        if not name:
            return jsonify({'status': 'error', 'message': '그룹명을 입력해 주세요.'}), 400
        if len(name) > 100:
            return jsonify({'status': 'error', 'message': '그룹명은 100자 이하로 입력해 주세요.'}), 400
        try:
            valid_from, valid_until = _validity_period(
                data.get('valid_from'), data.get('valid_until'),
            )
        except ValueError as exc:
            return jsonify({'status': 'error', 'message': str(exc)}), 400
        company_id = data.get('company_id', group_row['company_id'])
        try:
            company_id = int(company_id)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': '발급 회사를 선택해 주세요.'}), 400
        company = conn.execute('''
            SELECT id FROM certificate_companies WHERE id=? AND is_active=1
        ''', (company_id,)).fetchone()
        if not company:
            return jsonify({'status': 'error', 'message': '선택한 발급 회사를 찾을 수 없습니다.'}), 400
        conn.execute('''
            UPDATE excellent_instructor_roster_groups
            SET name=?, company_id=?, valid_from=?, valid_until=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (name, company_id, valid_from, valid_until, group_id))
        conn.commit()
        return jsonify({'status': 'success', 'message': '명단 그룹을 수정했습니다.'})
    finally:
        conn.close()


@document_bp.route('/api/excellent-instructors/<int:member_id>', methods=['PATCH', 'DELETE'])
@menu_permission_required("document_admin")
@_csrf_required
def update_excellent_instructor(member_id):
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        row = conn.execute('''
            SELECT e.id FROM excellent_instructor_eligibility e
            JOIN excellent_instructor_roster_groups g ON g.id=e.group_id
            WHERE e.id=? AND e.is_active=1 AND g.is_active=1
        ''', (member_id,)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '명단 항목을 찾을 수 없습니다.'}), 404
        if request.method == 'DELETE':
            conn.execute('''
                UPDATE excellent_instructor_eligibility
                SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?
            ''', (member_id,))
            conn.commit()
            return jsonify({'status': 'success', 'message': '명단 항목을 삭제했습니다.'})
        data = request.get_json(silent=True) or {}
        values = {
            'applicant_name': _clean_certificate_value(data.get('applicant_name')),
            'resident_number': _clean_certificate_value(data.get('resident_number')),
            'school_name': _clean_certificate_value(data.get('school_name')),
            'position': _clean_certificate_value(data.get('position')),
            'subject': _clean_certificate_value(data.get('subject')),
        }
        if any(not value for value in values.values()):
            return jsonify({'status': 'error', 'message': '명단 정보를 모두 입력해 주세요.'}), 400
        resident_digits = _normalize_resident_number(values['resident_number'])
        if len(resident_digits) != 13:
            return jsonify({'status': 'error', 'message': '주민번호는 숫자 13자리여야 합니다.'}), 400
        conn.execute('''
            UPDATE excellent_instructor_eligibility
            SET applicant_name=?, resident_number=?, resident_number_normalized=?,
                school_name=?, position=?, subject=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (
            values['applicant_name'], _format_resident_number(resident_digits),
            resident_digits, values['school_name'], values['position'],
            values['subject'], member_id,
        ))
        conn.commit()
        return jsonify({'status': 'success', 'message': '명단 항목을 수정했습니다.'})
    finally:
        conn.close()


def _excellent_upload_file():
    if request.content_length and request.content_length > 10 * 1024 * 1024:
        raise ValueError('엑셀 파일은 10MB 이하만 등록할 수 있습니다.')
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        raise ValueError('업로드할 엑셀 파일을 선택해 주세요.')
    if os.path.splitext(uploaded.filename)[1].lower() != '.xlsx':
        raise ValueError('XLSX 형식의 엑셀 파일만 등록할 수 있습니다.')
    return uploaded


def _parse_excellent_instructor_upload(uploaded):
    expected_headers = ['학교명', '성명', '주민번호', '직책', '강의과목']
    workbook = None
    try:
        workbook = load_workbook(uploaded.stream, read_only=True, data_only=True)
        worksheet = workbook.active
        headers = [
            _clean_certificate_value(cell.value)
            for cell in next(worksheet.iter_rows(min_row=1, max_row=1))
        ]
        header_positions = {}
        for header in expected_headers:
            if header not in headers:
                raise ValueError(f'필수 열 [{header}]이 없습니다. 샘플 양식을 사용해 주세요.')
            header_positions[header] = headers.index(header)

        records = []
        errors = []
        seen = set()
        for row_number, row in enumerate(
            worksheet.iter_rows(min_row=2, values_only=True), start=2,
        ):
            if row_number > 20001:
                errors.append('등록 가능한 명단은 최대 20,000행입니다.')
                break
            values = {
                header: _clean_certificate_value(
                    row[index] if index < len(row) else ''
                )
                for header, index in header_positions.items()
            }
            if not any(values.values()):
                continue
            resident_digits = _normalize_resident_number(values['주민번호'])
            missing = [header for header in expected_headers if not values[header]]
            if missing:
                errors.append(f'{row_number}행: {", ".join(missing)} 누락')
                continue
            if len(resident_digits) != 13:
                errors.append(f'{row_number}행: 주민번호는 숫자 13자리여야 합니다.')
                continue
            record_key = (
                values['성명'], resident_digits, values['학교명'],
                values['직책'], values['강의과목'],
            )
            if record_key in seen:
                continue
            seen.add(record_key)
            records.append({
                'applicant_name': values['성명'],
                'resident_number': _format_resident_number(resident_digits),
                'resident_number_normalized': resident_digits,
                'school_name': values['학교명'],
                'position': values['직책'],
                'subject': values['강의과목'],
            })
    except (InvalidFileException, OSError, StopIteration) as exc:
        raise ValueError(f'엑셀 파일을 읽을 수 없습니다: {exc}') from exc
    finally:
        if workbook is not None:
            workbook.close()

    if errors:
        preview = ' / '.join(errors[:5])
        more = f' 외 {len(errors) - 5}건' if len(errors) > 5 else ''
        raise ValueError(f'{preview}{more}')
    if not records:
        raise ValueError('등록할 강사 데이터가 없습니다.')
    return records


@document_bp.post('/api/excellent-instructors/preview')
@menu_permission_required("document_admin")
@_csrf_required
def preview_excellent_instructors():
    try:
        records = _parse_excellent_instructor_upload(_excellent_upload_file())
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    preview_limit = 500
    return jsonify({
        'status': 'success',
        'count': len(records),
        'preview_limit': preview_limit,
        'items': [{
            'number': index,
            'school_name': record['school_name'],
            'applicant_name': record['applicant_name'],
            'resident_number_masked': _masked_resident_number(record['resident_number']),
            'position': record['position'],
            'subject': record['subject'],
        } for index, record in enumerate(records[:preview_limit], start=1)],
    })


@document_bp.route('/api/excellent-instructors/upload', methods=['POST'])
@menu_permission_required("document_admin")
@_csrf_required
def upload_excellent_instructors():
    try:
        uploaded = _excellent_upload_file()
        records = _parse_excellent_instructor_upload(uploaded)
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    try:
        valid_from, valid_until = _validity_period(
            request.form.get('valid_from'), request.form.get('valid_until'),
        )
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400

    group_name = _clean_certificate_value(request.form.get('group_name'))
    if not group_name:
        group_name = f'{valid_from} ~ {valid_until}'
    if len(group_name) > 100:
        return jsonify({'status': 'error', 'message': '그룹명은 100자 이하로 입력해 주세요.'}), 400
    source_filename = original_filename(uploaded.filename, '우수강사명단.xlsx')
    try:
        company_id = int(request.form.get('company_id', ''))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '발급 회사를 선택해 주세요.'}), 400

    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        company = conn.execute('''
            SELECT id FROM certificate_companies WHERE id=? AND is_active=1
        ''', (company_id,)).fetchone()
        if not company:
            return jsonify({'status': 'error', 'message': '선택한 발급 회사를 찾을 수 없습니다.'}), 400
        group_cursor = conn.execute('''
            INSERT INTO excellent_instructor_roster_groups (
                name, company_id, valid_from, valid_until,
                original_filename, created_by
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            group_name, company_id, valid_from, valid_until, source_filename,
            _owner_emp_no(),
        ))
        group_id = int(group_cursor.lastrowid)
        conn.executemany('''
            INSERT INTO excellent_instructor_eligibility (
                group_id, applicant_name, resident_number, resident_number_normalized,
                school_name, position, subject, uploaded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', [(
            group_id, record['applicant_name'], record['resident_number'],
            record['resident_number_normalized'], record['school_name'],
            record['position'], record['subject'], _owner_emp_no(),
        ) for record in records])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify({
        'status': 'success',
        'message': f'새 명단 그룹에 우수강사 {len(records)}건을 추가했습니다.',
        'count': len(records), 'valid_from': valid_from,
        'valid_until': valid_until, 'group_id': group_id,
    })


@document_bp.route('/api/senders/<int:sender_id>', methods=['DELETE'])
@menu_permission_required("document_admin")
@_csrf_required
def delete_certificate_sender(sender_id):
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        sender = conn.execute('''
            SELECT id FROM ai_mail_senders
            WHERE id=? AND owner_emp_no=? AND is_active=1
        ''', (sender_id, _owner_emp_no())).fetchone()
        if not sender:
            return jsonify({'status': 'error', 'message': '발송계정을 찾을 수 없습니다.'}), 404
        in_use = int(conn.execute('''
            SELECT COUNT(*) FROM certificate_workgroups
            WHERE sender_id=? AND is_active=1
        ''', (sender_id,)).fetchone()[0])
        if in_use:
            return jsonify({
                'status': 'error',
                'message': '사용 중인 작업그룹이 있어 발송계정을 삭제할 수 없습니다.',
            }), 409
        conn.execute('''
            UPDATE ai_mail_senders SET is_active=0, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND owner_emp_no=?
        ''', (sender_id, _owner_emp_no()))
        conn.commit()
        return jsonify({'status': 'success', 'message': '발송계정을 삭제했습니다.'})
    finally:
        conn.close()


def _save_company_image(upload, folder, label):
    if not upload or not upload.filename:
        return '', ''
    # secure_filename()은 파일명이 전부 한글이면 확장자 앞의 점까지 제거할 수
    # 있으므로, 표시용 원본명에서 직접 확장자를 추출하고 저장명만 난수화한다.
    original_name = original_filename(upload.filename, 'image')
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in {'.png', '.jpg', '.jpeg', '.gif', '.webp'}:
        raise ValueError(f'{label} 이미지는 PNG, JPG, GIF, WEBP 파일만 등록할 수 있습니다.')
    stored_name = encrypted_storage_name(original_name)
    stored_path = os.path.join(folder, stored_name)
    encrypt_upload(upload, stored_path)
    return original_name, stored_path


def _company_form_values(current=None):
    current = current or {}
    company_name = _clean_certificate_value(request.form.get('company_name'))
    representative_name = _clean_certificate_value(request.form.get('representative_name'))
    if not company_name:
        raise ValueError('회사명을 입력해 주세요.')
    if not representative_name:
        raise ValueError('대표자 이름을 입력해 주세요.')
    seal_filename = seal_path = logo_filename = logo_path = ''
    try:
        seal_filename, seal_path = _save_company_image(
            request.files.get('seal'), CERT_SEAL_FOLDER, '인감',
        )
        logo_filename, logo_path = _save_company_image(
            request.files.get('logo'), CERT_LOGO_FOLDER, '회사 로고',
        )
    except Exception:
        delete_file(seal_path)
        delete_file(logo_path)
        raise
    return {
        'company_name': company_name,
        'representative_name': representative_name,
        'business_number': _clean_certificate_value(request.form.get('business_number')),
        'address': _clean_certificate_value(request.form.get('address')),
        'phone': _clean_certificate_value(request.form.get('phone')),
        'seal_filename': seal_filename or current.get('seal_filename', ''),
        'seal_path': seal_path or current.get('seal_path', ''),
        'logo_filename': logo_filename or current.get('logo_filename', ''),
        'logo_path': logo_path or current.get('logo_path', ''),
    }


@document_bp.route('/api/companies', methods=['POST'])
@menu_permission_required("document_admin")
@_csrf_required
def create_certificate_company():
    values = None
    try:
        values = _company_form_values()
        conn = get_db()
        try:
            ensure_certificate_schema(conn)
            cursor = conn.execute('''
                INSERT INTO certificate_companies (
                    company_name, representative_name, business_number,
                    address, phone, seal_filename, seal_path,
                    logo_filename, logo_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', tuple(values[key] for key in (
                'company_name', 'representative_name', 'business_number',
                'address', 'phone', 'seal_filename', 'seal_path',
                'logo_filename', 'logo_path',
            )))
            conn.commit()
            return jsonify({
                'status': 'success', 'message': '발급 회사를 등록했습니다.',
                'company_id': int(cursor.lastrowid),
            })
        finally:
            conn.close()
    except ValueError as exc:
        if values:
            delete_file(values.get('seal_path'))
            delete_file(values.get('logo_path'))
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    except Exception as exc:
        if values:
            delete_file(values.get('seal_path'))
            delete_file(values.get('logo_path'))
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@document_bp.route('/api/companies/<int:company_id>', methods=['POST', 'PATCH', 'DELETE'])
@menu_permission_required("document_admin")
@_csrf_required
def update_certificate_company(company_id):
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        row = conn.execute(
            'SELECT * FROM certificate_companies WHERE id=? AND is_active=1',
            (company_id,),
        ).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': '회사를 찾을 수 없습니다.'}), 404
        if request.method == 'DELETE':
            in_use = int(conn.execute('''
                SELECT COUNT(*) FROM certificate_workgroups
                WHERE company_id=? AND is_active=1
            ''', (company_id,)).fetchone()[0])
            if in_use:
                return jsonify({
                    'status': 'error',
                    'message': '사용 중인 작업그룹이 있어 회사를 삭제할 수 없습니다.',
                }), 409
            conn.execute('''
                UPDATE certificate_companies
                SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?
            ''', (company_id,))
            conn.commit()
            delete_file(row['seal_path'])
            delete_file(row['logo_path'])
            return jsonify({'status': 'success', 'message': '회사를 삭제했습니다.'})

        values = _company_form_values(dict(row))
        new_seal_path = values['seal_path'] if values['seal_path'] != row['seal_path'] else ''
        new_logo_path = values['logo_path'] if values['logo_path'] != row['logo_path'] else ''
        conn.execute('''
            UPDATE certificate_companies
            SET company_name=?, representative_name=?, business_number=?,
                address=?, phone=?, seal_filename=?, seal_path=?,
                logo_filename=?, logo_path=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        ''', (
            values['company_name'], values['representative_name'],
            values['business_number'], values['address'], values['phone'],
            values['seal_filename'], values['seal_path'],
            values['logo_filename'], values['logo_path'], company_id,
        ))
        conn.commit()
        if new_seal_path:
            delete_file(row['seal_path'])
        if new_logo_path:
            delete_file(row['logo_path'])
        return jsonify({'status': 'success', 'message': '회사 정보를 수정했습니다.'})
    except ValueError as exc:
        conn.rollback()
        delete_file(locals().get('new_seal_path'))
        delete_file(locals().get('new_logo_path'))
        return jsonify({'status': 'error', 'message': str(exc)}), 400
    except Exception as exc:
        conn.rollback()
        delete_file(locals().get('new_seal_path'))
        delete_file(locals().get('new_logo_path'))
        return jsonify({'status': 'error', 'message': str(exc)}), 500
    finally:
        conn.close()


@document_bp.route('/company-seal/<int:company_id>')
@menu_permission_required("document_admin")
def company_seal(company_id):
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        row = conn.execute(
            'SELECT seal_filename,seal_path FROM certificate_companies WHERE id=? AND is_active=1',
            (company_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['seal_path'] or not os.path.isfile(row['seal_path']):
        abort(404)
    response = encrypted_response(
        row['seal_path'], row['seal_filename'] or 'seal.png', as_attachment=False
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@document_bp.route('/company-logo/<int:company_id>')
def company_logo(company_id):
    """공개 신청 화면에서 사용하는 활성 회사 로고."""
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        row = conn.execute(
            'SELECT logo_filename,logo_path FROM certificate_companies WHERE id=? AND is_active=1',
            (company_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['logo_path'] or not os.path.isfile(row['logo_path']):
        abort(404)
    response = encrypted_response(
        row['logo_path'], row['logo_filename'] or 'logo.png', as_attachment=False
    )
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _json_bool(data, key, default=True):
    value = data.get(key, default)
    if isinstance(value, bool):
        return int(value)
    return int(str(value).strip().lower() in {'1', 'true', 'yes', 'on'})


@document_bp.route('/api/workgroups', methods=['POST'])
@menu_permission_required("document_admin")
@_csrf_required
def create_certificate_workgroup():
    data = request.get_json(silent=True) or {}
    return _save_certificate_workgroup(None, data)


@document_bp.route('/api/workgroups/<int:workgroup_id>', methods=['PATCH', 'PUT', 'DELETE'])
@menu_permission_required("document_admin")
@_csrf_required
def update_certificate_workgroup(workgroup_id):
    if request.method == 'DELETE':
        conn = get_db()
        try:
            ensure_certificate_schema(conn)
            changed = conn.execute('''
                UPDATE certificate_workgroups
                SET is_active=0, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND is_active=1
            ''', (workgroup_id,)).rowcount
            conn.commit()
            if not changed:
                return jsonify({'status': 'error', 'message': '작업그룹을 찾을 수 없습니다.'}), 404
            return jsonify({'status': 'success', 'message': '작업그룹과 공개 신청 링크를 종료했습니다.'})
        finally:
            conn.close()
    return _save_certificate_workgroup(workgroup_id, request.get_json(silent=True) or {})


def _save_certificate_workgroup(workgroup_id, data):
    name = _clean_certificate_value(data.get('name'))
    company_id = data.get('company_id')
    sender_id = data.get('sender_id')
    allow_instructor = _json_bool(data, 'allow_instructor')
    allow_employee = _json_bool(data, 'allow_employee')
    allow_excellent_instructor = _json_bool(
        data, 'allow_excellent_instructor', default=False,
    )
    if not name:
        return jsonify({'status': 'error', 'message': '작업그룹명을 입력해 주세요.'}), 400
    if not str(company_id or '').isdigit():
        return jsonify({'status': 'error', 'message': '발급 회사를 선택해 주세요.'}), 400
    if not str(sender_id or '').isdigit():
        return jsonify({'status': 'error', 'message': '발송계정을 선택해 주세요.'}), 400
    if not allow_instructor and not allow_employee and not allow_excellent_instructor:
        return jsonify({'status': 'error', 'message': '신청 대상 유형을 하나 이상 선택해 주세요.'}), 400

    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        company = conn.execute(
            'SELECT id FROM certificate_companies WHERE id=? AND is_active=1',
            (int(company_id),),
        ).fetchone()
        sender = conn.execute(
            'SELECT id FROM ai_mail_senders WHERE id=? AND is_active=1',
            (int(sender_id),),
        ).fetchone()
        if not company or not sender:
            return jsonify({'status': 'error', 'message': '회사 또는 발송계정을 사용할 수 없습니다.'}), 400
        if workgroup_id:
            exists = conn.execute(
                'SELECT id FROM certificate_workgroups WHERE id=? AND is_active=1',
                (workgroup_id,),
            ).fetchone()
            if not exists:
                return jsonify({'status': 'error', 'message': '작업그룹을 찾을 수 없습니다.'}), 404
            conn.execute('''
                UPDATE certificate_workgroups
                SET name=?, company_id=?, sender_id=?, allow_instructor=?,
                    allow_employee=?, allow_excellent_instructor=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (
                name, int(company_id), int(sender_id), allow_instructor,
                allow_employee, allow_excellent_instructor, workgroup_id,
            ))
            message = '작업그룹을 수정했습니다.'
        else:
            cursor = conn.execute('''
                INSERT INTO certificate_workgroups (
                    name, company_id, sender_id, access_token,
                    allow_instructor, allow_employee, allow_excellent_instructor,
                    created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                name, int(company_id), int(sender_id), secrets.token_urlsafe(18),
                allow_instructor, allow_employee, allow_excellent_instructor,
                _owner_emp_no(),
            ))
            workgroup_id = int(cursor.lastrowid)
            message = '작업그룹과 신청 링크를 생성했습니다.'
        conn.commit()
        return jsonify({
            'status': 'success', 'message': message,
            'workgroup_id': int(workgroup_id),
        })
    except Exception as exc:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(exc)}), 500
    finally:
        conn.close()

@document_bp.route('/pdf/<int:idx>')
@menu_permission_required("document_admin")
def serve_pdf(idx):
    """관리자 페이지에서 발급된 PDF를 개인정보 없는 URL로 제공한다."""
    if 'emp_no' not in session: return abort(403)

    conn = get_db()
    try:
        row = conn.execute(
            '''
            SELECT filename
            FROM certificate_requests
            WHERE id=? AND status='발급완료' AND filename IS NOT NULL
            ''',
            (idx,),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row['filename']:
        return abort(404)

    safe_name = os.path.basename(row['filename'])
    pdf_path = os.path.join(PDF_FOLDER, safe_name)
    if not os.path.isfile(pdf_path):
        return abort(404)

    return encrypted_response(
        pdf_path, 'certificate.pdf',
        as_attachment=False, mimetype='application/pdf'
    )

@document_bp.route('/delete/<int:idx>')
@menu_permission_required("document_admin")
def delete_record(idx):
    """신청 기록 및 파일 단건 삭제"""
    if 'emp_no' not in session: return abort(403)
    conn = None
    try:
        conn = get_db()
        row = conn.execute(
            'SELECT filename FROM certificate_requests WHERE id=?',
            (idx,),
        ).fetchone()
        if row:
            filename = row['filename']
            conn.execute(
                'DELETE FROM certificate_requests WHERE id=?',
                (idx,),
            )
            conn.commit()
            if filename:
                delete_file(os.path.join(PDF_FOLDER, filename))
            flash("기록이 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"삭제 중 오류: {str(e)}")
    finally:
        if conn is not None:
            conn.close()
    return redirect(url_for('document.admin_list'))

@document_bp.route('/delete_multiple', methods=['POST'])
@menu_permission_required("document_admin")
def delete_multiple():
    """여러 건 동시 선택 삭제"""
    if 'emp_no' not in session: return abort(403)
    conn = None
    try:
        selected_ids = [
            int(value) for value in request.form.getlist('chk_ids')
            if str(value).isdigit()
        ]
        if not selected_ids:
            flash("삭제할 항목이 선택되지 않았습니다.")
            return redirect(url_for('document.admin_list'))

        conn = get_db()
        placeholders = ','.join('?' for _ in selected_ids)
        rows = conn.execute(
            f'SELECT id, filename FROM certificate_requests WHERE id IN ({placeholders})',
            selected_ids,
        ).fetchall()
        conn.execute(
            f'DELETE FROM certificate_requests WHERE id IN ({placeholders})',
            selected_ids,
        )
        deleted_count = int(conn.execute('SELECT changes()').fetchone()[0])
        conn.commit()
        for row in rows:
            if row['filename']:
                delete_file(os.path.join(PDF_FOLDER, row['filename']))
        flash(f"총 {deleted_count}건의 기록이 성공적으로 삭제되었습니다.")
    except Exception as e:
        flash(f"선택 삭제 중 오류: {str(e)}")
    finally:
        if conn is not None:
            conn.close()
        
    return redirect(url_for('document.admin_list'))


# 안내 메일 전송 기능 (admin.html 모달 전송용)
@document_bp.route('/send_simple_email', methods=['POST'])
@menu_permission_required("document_admin")
def send_simple_email():
    if 'emp_no' not in session: return abort(403)
    
    to_email = request.form.get('email', '').strip()
    subject = request.form.get('subject', '')
    body = request.form.get('body', '')
    workgroup_id = request.form.get('workgroup_id', type=int)
    
    if not to_email:
        flash("발송 실패: 수신자 이메일 주소를 확인해주세요.")
        return redirect(url_for('document.admin_list'))
        
    conn = get_db()
    try:
        ensure_certificate_schema(conn)
        bundle = _workgroup_bundle(conn, workgroup_id)
        sender_row = None
        if bundle and bundle.get('sender_id'):
            sender_row = conn.execute(
                'SELECT * FROM ai_mail_senders WHERE id=? AND is_active=1',
                (bundle['sender_id'],),
            ).fetchone()
    finally:
        conn.close()

    if workgroup_id and (not bundle or not sender_row):
        flash('발송 실패: 선택한 작업그룹의 회사 또는 발송계정을 사용할 수 없습니다.')
        return redirect(url_for('document.admin_list'))

    if sender_row:
        try:
            sender = dict(sender_row)
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = subject
            msg['From'] = _sender_from_header(sender)
            msg['To'] = to_email
            _send_registered_message(sender, msg)
            flash('이메일이 등록된 발송계정으로 전송되었습니다.')
        except Exception as exc:
            flash(f'메일 발송 실패: {str(exc)}')
        return redirect(url_for('document.admin_list'))

    email_addr, email_pw = get_email_credentials()
    if not email_addr or not email_pw:
        flash("발송 실패: 작업그룹 발송계정 또는 서버 환경변수가 설정되지 않았습니다.")
        return redirect(url_for('document.admin_list'))
        
    # [1차 시도] yagmail
    try:
        yag = yagmail.SMTP(email_addr, email_pw)
        yag.send(to=to_email, subject=subject, contents=body)
        flash("이메일이 성공적으로 발송되었습니다.")
    except Exception as yag_e:
        # [2차 시도] smtplib 587
        try:
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = email_addr
            msg['To'] = to_email
            
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(email_addr, email_pw)
            server.send_message(msg)
            server.quit()
            flash("이메일이 성공적으로 발송되었습니다. (보조 발송 라인 이용)")
        except Exception as smtp_e:
            flash(f"메일 발송 완전 실패. 상세 원인:\n{str(smtp_e)}")
            
    return redirect(url_for('document.admin_list'))

@document_bp.route('/edit', methods=['POST'])
@menu_permission_required("document_admin")
def edit_record_post():
    """모달창에서 전송된 수정 데이터를 SQLite에 반영"""
    if 'emp_no' not in session: return abort(403)
    
    conn = None
    try:
        idx = int(request.form.get('idx'))
        fields = [
            '증명서종류', '성명', '주민번호', '자택주소',
            '근무시작일', '근무종료일', '근무장소', '강의과목',
            '직책', '용도', '종료사유', '이메일주소',
        ]
        updates = []
        values = []
        for field in fields:
            if field in request.form:
                updates.append(f'{CERTIFICATE_FIELD_MAP[field]}=?')
                values.append(_clean_certificate_value(request.form.get(field)))

        conn = get_db()
        exists = conn.execute(
            'SELECT 1 FROM certificate_requests WHERE id=?',
            (idx,),
        ).fetchone()
        if exists and updates:
            values.append(idx)
            conn.execute(
                f'''
                    UPDATE certificate_requests
                    SET {', '.join(updates)}, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                ''',
                values,
            )
            conn.commit()
            flash("신청 정보가 성공적으로 수정되었습니다.")
        else:
            flash("해당 데이터를 찾을 수 없습니다.")
    except Exception as e:
        flash(f"수정 중 오류 발생: {str(e)}")
    finally:
        if conn is not None:
            conn.close()
        
    return redirect(url_for('document.admin_list'))
