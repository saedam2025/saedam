from flask import Flask, session, redirect, url_for, request, render_template, jsonify, abort
from datetime import datetime
import json
import os
import re
import secrets
import sys
import time
import traceback
from routes.socketio_ext import socketio

# 배포 환경에서 모듈 임포트 에러 방지를 위해 현재 디렉토리를 시스템 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 블루프린트 임포트
from routes.main import main_bp
from routes.document import document_bp
from routes.verified_contract import verified_contract_bp
from routes.user_mgmt import send_account_recovery_email, user_mgmt_bp
from routes.approval import approval_bp
from routes.expense import expense_bp
from routes.board import board_bp
from routes.payroll import payroll_bp
from routes.ai_mail import ai_mail_bp
from routes.ai_agent import ai_agent_bp
from routes.smart_document import ensure_smart_document_schema, smart_document_bp
from routes.memo import memo_bp
from routes.mydesk import ensure_mydesk_schema, mydesk_bp
from routes.attendance import attendance_bp
from routes.interview import ensure_interview_schema, interview_bp
from routes.excel_generator import excel_bp
from routes.explorer import explorer_bp
from routes.notifications import emit_notification_refresh, noti_bp
from routes.gallery import gallery_bp
from routes.school_bp import school_bp
from routes.school_task import school_task_bp
from routes.survey import init_survey_schema, survey_bp
from routes.contacts import contacts_bp
from routes.event_admin import ensure_event_schema, event_bp
from routes.school_billing import billing_bp, ensure_billing_schema, portal_bp
from routes.admin_management import admin_bp, get_active_theme
from routes.ebook import ebook_bp, init_ebook_schema
from routes.photobook import init_photobook_schema, photobook_bp
from routes.exhibition import exhibition_bp, init_exhibition_schema
from routes.meeting import init_meeting_schema, meeting_bp
from routes.webtoon import init_webtoon_schema, webtoon_bp
from routes.manual import manual_bp, init_manual_schema
from routes.parent_notifications import (
    ensure_parent_notification_schema,
    parent_notification_bp,
)
from routes.instructor_attendance import (
    init_instructor_attendance_schema,
    instructor_attendance_bp,
)
from routes.points import points_bp, award_response_activity
from routes.unified_search import unified_search_bp

# [수정] gall2.py가 routes 폴더 안에 있다면 아래와 같이 수정해야 합니다.
from routes.gall2 import gall2_bp

# 🚀 새로 분리한 사내 메신저 블루프린트 임포트
from routes.chat import chat_bp

# 데이터베이스 모듈 임포트
from routes.database import get_db, init_db
from routes.storage import PROFILE_ROOT, verify_storage_ready
from routes.secure_files import delete_file, encrypted_storage_name, encrypt_upload, original_filename
from routes.usage_stats import (
    get_login_summary,
    record_login_activity,
    record_page_usage,
    start_usage_session,
)
from routes.security import (
    hash_password,
    load_session_secret,
    migrate_plaintext_passwords,
    upgrade_legacy_password,
    verify_password,
)
from routes.menu_access import (
    build_menu_access,
    center_director_mode_active,
    enforce_request_menu_access,
)

app = Flask(__name__)
app.secret_key = load_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '').lower() in {'1', 'true', 'yes'},
)
socketio.init_app(app)

# =====================================================================
# [DB 초기화 로직] 배포 환경에서도 안전하게 실행
# =====================================================================
with app.app_context():
    try:
        storage_status = verify_storage_ready()
        init_db()
        init_ebook_schema()
        init_webtoon_schema()
        init_photobook_schema()
        init_exhibition_schema()
        init_meeting_schema()
        init_manual_schema()
        ensure_parent_notification_schema()
        ensure_smart_document_schema()
        ensure_interview_schema()
        ensure_mydesk_schema()
        init_survey_schema()
        init_instructor_attendance_schema()
        event_conn = get_db()
        try:
            ensure_event_schema(event_conn)
            ensure_billing_schema(event_conn)
        finally:
            event_conn.close()
        password_conn = get_db()
        try:
            migrated_passwords = migrate_plaintext_passwords(password_conn)
            if migrated_passwords:
                print(f"✅ 기존 사용자 비밀번호 {migrated_passwords}건 해시 전환 완료.")
        finally:
            password_conn.close()
        print(
            "✅ 영구 저장소 확인 완료: "
            f"DB={storage_status['database']}, "
            f"Persistent Disk={storage_status['persistent_disk']}"
        )
        print("✅ 데이터베이스 초기화 및 필수 폴더 생성 완료.")
        # 이력서 얼굴 인식은 OpenCV가 있어야 동작한다. Render 배포 로그에서
        # 준비 상태를 바로 확인할 수 있도록 시작할 때 한 번 남긴다.
        try:
            from services.interview_resume import face_detection_status
            face_status = face_detection_status()
            if face_status['available']:
                print(f"✅ 이력서 얼굴 인식 준비 완료(분류기 {face_status['cascade_count']}종).")
            else:
                print(
                    "⚠️ 이력서 얼굴 인식을 사용할 수 없습니다. 지원자 사진 추출 정확도가 "
                    f"떨어집니다. 탐색 경로={face_status['search_paths']}"
                )
        except Exception as face_error:
            print(f"⚠️ 이력서 얼굴 인식 상태 확인 실패: {face_error}")
    except Exception as e:
        print(f"❌ 데이터베이스 초기화 실패: {e}")
        # Render에서는 저장소/DB 초기화 실패를 숨긴 채 서비스를 시작하면
        # 작업그룹과 광고 이미지가 저장된 것처럼 보였다가 유실될 수 있다.
        # 배포 자체를 실패시켜 Persistent Disk 설정을 먼저 바로잡게 한다.
        if os.name != 'nt' and (
            os.environ.get('RENDER', '').strip().lower() in {'1', 'true', 'yes', 'on'}
            or os.environ.get('RENDER_SERVICE_ID', '').strip()
        ):
            raise

    # 필수 정적 폴더 확인
    os.makedirs('static', exist_ok=True)
# =====================================================================

# 💡 [새담 게시판 연동 추가] 첨부파일 최대 용량을 1.5GB로 설정 
# (이 설정이 없으면 Flask 기본 제한에 걸려 대용량 파일 업로드 시 에러가 발생합니다)
app.config['MAX_CONTENT_LENGTH'] = 1.5 * 1024 * 1024 * 1024

# 로그인 체크 제외 대상 (정적 파일 및 외부 서비스 경로)
EXEMPT_ROUTES = [
    'login_page', 
    'login', 
    'recover_login_account',
    'logout', 
    'user_mgmt.register', 
    'user_mgmt.invite_page', 
    'static',
    'verified_contract.public_contract',
    'verified_contract.send_otp',
    'verified_contract.verify_otp',
    'verified_contract.complete_contract',
    'verified_contract.public_download',
    'document.apply',
    'document.apply2',
    'document.apply_excellent',
    'document.lookup_excellent_instructor',
    'document.company_logo',
    'expense.submit_expense',
    'expense.submit_expense_instructor',
    'expense.preview_expense_upload',
    'expense.expense_template',
    # e리플렛 공유 뷰어와 이미지만 학부모가 로그인 없이 열람한다.
    'ebook.public_reader',
    'ebook.serve_cover',
    'ebook.serve_page_image',
    # 학부모는 인트라넷 계정 없이 비밀 등록 링크와 별도 푸시 구독을 사용한다.
    'parent_notifications.parent_register',
    'parent_notifications.parent_push_public_key',
    'parent_notifications.parent_push_subscribe',
    'parent_notifications.parent_push_worker',
    # 강사 전용 링크의 안내 화면만 공개하고 출결·발송 API는 로그인을 요구한다.
    'parent_notifications.instructor_page',
    # 면접자가 인트라넷 계정 없이 링크로 여는 사전질문지.
    'interview.questionnaire',
    # 설문 대상자는 문자로 받은 링크만으로 응답한다.
    'survey.respond',
    'survey.submit_response',
]

# 엔드포인트 이름과 무관하게 로그인을 요구하지 않는 공개 경로.
# /portal 은 학교회원 전용 포털이다. 인트라넷 계정(emp_no)과 무관하게
# 자체 로그인(billing_member_id)으로 보호하므로 인트라넷 로그인 검사에서 뺀다.
# 강사출결 스캔·QR표시 화면은 강사 휴대폰과 학교 태블릿이 계정 없이 연다.
# (오늘 발급된 QR 토큰과 표시용 비밀키로 보호한다.)
PUBLIC_PATH_PREFIXES = (
    '/survey/r/',
    '/portal',
    '/instructor-attendance/scan/',
    '/instructor-attendance/display/',
)

def _is_script_request() -> bool:
    """브라우저 주소창이 아니라 화면 속 스크립트가 부른 요청인지 판단한다."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    if request.is_json:
        return True
    if request.accept_mimetypes.best == 'application/json':
        return True
    # fetch()는 문서 요청과 달리 Sec-Fetch-Mode: cors 로 온다.
    return request.headers.get('Sec-Fetch-Mode', '') == 'cors'


@app.before_request
def check_login():
    # 예전 센터장 게시판 정적 첨부 경로는 차단하고, 권한검사를 거치는
    # /school/file/<저장명> 라우트에서만 복호화해 제공한다.
    if request.path and request.path.startswith('/static/school_uploads/'):
        abort(404)
    # 1. 예외 경로이거나 정적 파일 요청이면 통과
    if request.endpoint in EXEMPT_ROUTES or (request.path and request.path.startswith('/static')):
        return None

    # 설문 응답 링크는 인트라넷 회원이 아닌 사람이 문자로 받아서 여는 주소다.
    # 배포 직후처럼 라우트를 아직 못 찾아 endpoint가 비는 상황에서도 로그인
    # 화면으로 밀려나지 않도록 경로만 보고 통과시킨다.
    if any((request.path or '').startswith(prefix) for prefix in PUBLIC_PATH_PREFIXES):
        return None
    
    # 2. 세션에 사번(emp_no)이 없으면 로그인 페이지로 이동
    if 'emp_no' not in session:
        # 화면 속 자동저장(fetch/XHR)까지 로그인 화면으로 넘기면, 브라우저는
        # 로그인 페이지 HTML을 200으로 받아 '저장 성공'으로 착각한다.
        # 스크립트가 부른 요청에는 401을 분명히 돌려주어 사용자가 기록을
        # 잃지 않고 다시 로그인하도록 안내한다.
        if _is_script_request():
            return jsonify({
                'status': 'error',
                'code': 'login_required',
                'message': '로그인이 풀렸습니다. 새 창에서 다시 로그인한 뒤 저장해 주세요.',
                'login_url': url_for('login_page'),
            }), 401
        return redirect(url_for('login_page'))
    
    _record_usage_log()


@app.before_request
def check_menu_access():
    return enforce_request_menu_access()


NOTIFICATION_MUTATION_PREFIXES = (
    '/approval',
    '/expense',
    '/school',
    '/document',
)
NOTIFICATION_MUTATING_GET_ENDPOINTS = {
    'document.generate_certificate',
    'document.delete_record',
}


@app.after_request
def push_notification_changes(response):
    """업무 데이터가 바뀐 뒤 연결된 메인 화면에 갱신 신호를 보낸다."""
    try:
        path = request.path or ''
        is_mutating_method = request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}
        is_notification_area = any(
            path.startswith(prefix)
            for prefix in NOTIFICATION_MUTATION_PREFIXES
        )
        is_mutating_get = request.endpoint in NOTIFICATION_MUTATING_GET_ENDPOINTS
        if response.status_code < 400 and (
            (is_mutating_method and is_notification_area)
            or is_mutating_get
        ):
            emit_notification_refresh(request.endpoint or path)
    except Exception as e:
        print(f"업무 알림 WebSocket 전송 오류: {e}")
    return response


@app.after_request
def award_user_activity_points(response):
    """성공한 로그인·게시물 등록·메신저 사용에 정해진 포인트를 적립한다."""
    if response.status_code < 400:
        user_name = str(session.get('user_name') or '').strip()
        try:
            award_response_activity(request.method, request.endpoint, user_name)
        except Exception:
            app.logger.exception('사용자 활동 포인트 적립 실패')
    return response


def _classify_menu(path):
    menu_map = [
        ('/admin', '통합관리'),
        ('/user', '인사관리'),
        ('/board', '게시판'),
        ('/chat', '사내메신저'),
        ('/chat_popup', '사내메신저'),
        ('/school', '학교업무메뉴'),
        ('/survey', '설문조사'),
        ('/document', '증명발급'),
        ('/gall2', '갤러리'),
        ('/gallery', '갤러리'),
        ('/approval', '사내결재'),
        ('/expense', '지출결의'),
        ('/ai-mail', 'AI메일전송'),
        ('/ai-agent', 'AI에이전트'),
        ('/smart-document', '스마트공문발송'),
        ('/payroll', '급여/업무지원'),
        ('/attendance', '근태관리'),
        ('/instructor-attendance', '강사출결시스템'),
        ('/contacts', '본사연락망'),
        ('/memo', '개인화이트보드'),
        ('/meeting', '회의센터'),
        ('/excel-generator', '입금용 엑셀 생성기'),
        ('/manual', '새담메뉴얼'),
        ('/ebook/books', 'eBook'),
        ('/ebook', 'e리플렛'),
        ('/photobook', '웹전자책'),
        ('/exhibition', '3D전시장'),
        ('/notifications', '알림'),
    ]
    if path == '/':
        return '메인메뉴'
    for prefix, label in menu_map:
        if path.startswith(prefix):
            return label
    return '기타'


def _record_usage_log():
    try:
        record_page_usage(request, session, _classify_menu(request.path or ''))
    except Exception as e:
        print(f"이용 로그 기록 오류: {e}")

# =====================================================================
# [전역 변수 설정]
# =====================================================================
@app.context_processor
def inject_user_data():
    try:
        user_level = session.get('user_level', 99)
        menu_access = build_menu_access(user_level)
        center_director_mode = center_director_mode_active(user_level)
    except Exception as e:
        print(f"메뉴 권한 로드 오류: {e}")
        menu_access = {}
        center_director_mode = False
    return {
        'current_user': session.get('user_name'),
        'current_user_profile_path': session.get('profile_path'),
        'current_user_level': session.get('user_level', 99),
        'global_theme': get_active_theme(),
        'menu_access': menu_access,
        'center_director_mode': center_director_mode,
    }

# =====================================================================
# 💡 [템플릿 필터 추가] 게시판 새 글(New) 표시를 위한 날짜 계산 필터
# =====================================================================
@app.template_filter('as_datetime')
def as_datetime_filter(value, format="%Y-%m-%d %H:%M:%S"):
    try:
        if not value:
            return None
        # SQLite에서 가져온 날짜에 밀리초나 불필요한 문자가 붙어있을 경우를 대비해
        # 앞의 19자리(YYYY-MM-DD HH:MM:SS)만 잘라서 안전하게 파싱합니다.
        return datetime.strptime(str(value)[:19], format)
    except:
        return None

# --- 로그인/로그아웃 로직 ---

@app.route('/login_page')
def login_page():
    hidden_theme_keys = []
    login_summary = {'today_users': 0, 'total_logins': 0}
    conn = None
    try:
        conn = get_db()
        admin = conn.execute("SELECT id FROM users WHERE emp_no = 'admin'").fetchone()
        hidden_rows = conn.execute('''
            SELECT DISTINCT theme_key
            FROM theme_catalog_preferences
            WHERE is_hidden=1
        ''').fetchall()
        hidden_theme_keys = [row['theme_key'] for row in hidden_rows]
        login_summary = get_login_summary(conn)
        
        if not admin:
            return render_template('user_list.html', mode='admin_setup')
    except Exception as e:
        print(f"로그인 페이지 관리자 체크 오류: {e}")
    finally:
        if conn is not None:
            conn.close()

    return render_template(
        'login.html',
        hidden_theme_keys=hidden_theme_keys,
        today_login_users=login_summary['today_users'],
        total_login_count=login_summary['total_logins'],
    )

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    
    if data.get('action') == 'setup_admin':
        conn = None
        try:
            password = data.get('password')
            if not password:
                return jsonify({"status": "error", "message": "비밀번호를 입력해주세요."}), 400
                
            conn = get_db()
            conn.execute("BEGIN IMMEDIATE")
            existing_admin = conn.execute(
                "SELECT id FROM users WHERE emp_no='admin' LIMIT 1"
            ).fetchone()
            if existing_admin:
                conn.rollback()
                return jsonify({
                    "status": "error",
                    "message": "관리자 계정이 이미 설정되어 있습니다."
                }), 403
            today = datetime.now().strftime('%Y-%m-%d')
            conn.execute('''
                INSERT INTO users (emp_no, name, password, position, level, rrn, email, status, join_date, profile_icon, department)
                VALUES ('admin', 'admin', ?, '최고관리자', 1, '-', 'admin@admin.com', '승인', ?, '👑', '본부')
            ''', (hash_password(password), today))
            conn.commit()
            return jsonify({"status": "success", "message": "최고관리자 설정 완료! 이제 로그인하세요."})
        except Exception as e:
            if conn is not None:
                conn.rollback()
            return jsonify({"status": "error", "message": str(e)}), 500
        finally:
            if conn is not None:
                conn.close()

    emp_no = str(data.get('emp_no', '')).strip()
    password = str(data.get('password', '')).strip()
    if len(emp_no) > 15 or len(password) > 15:
        return jsonify({"status": "error", "message": "사번과 비밀번호는 15자 이내로 입력해주세요."}), 400
    
    conn = get_db()
    user_row = conn.execute(
        "SELECT * FROM users WHERE emp_no=? LIMIT 1",
        (str(emp_no),),
    ).fetchone()
    
    if not user_row or not verify_password(user_row['password'], password):
        conn.close()
        return jsonify({"status": "error", "message": "사번 또는 비밀번호가 틀립니다."}), 401

    user = dict(user_row)
    upgrade_legacy_password(conn, user['id'], user.get('password'), password)
    
    if int(user.get('level', 99)) == 9:
        conn.close()
        return jsonify({"status": "error", "message": "현재 승인 대기 중입니다. 본사로 문의해 주세요."}), 403

    if user.get('status') != '승인':
        conn.close()
        return jsonify({"status": "error", "message": "승인이 대기 중인 계정입니다."}), 403
    
    session.clear()
    session['emp_no'] = str(user.get('emp_no', ''))
    session['user_name'] = user.get('name', '알수없음')
    session['user_level'] = int(user.get('level', 14))
    
    session['position'] = str(user.get('position', '미지정'))
    session['department'] = str(user.get('department', '소속미지정'))
    session['role'] = str(user.get('position', '미지정'))
    
    session['profile_path'] = user.get('profile_path', '')
    session['profile_icon'] = user.get('profile_icon') or user.get('아이콘') or '👤'

    conn.close()
    start_usage_session(session)
    try:
        record_login_activity(request, session, 'login')
    except Exception as e:
        print(f"로그인 기록 오류: {e}")
    
    return jsonify({"status": "success"})


ACCOUNT_RECOVERY_WINDOW_SECONDS = 15 * 60
ACCOUNT_RECOVERY_MAX_ATTEMPTS = 5
_account_recovery_attempts = {}


def _account_recovery_rate_limited(client_key):
    """프로세스 단위로 로그인 정보 찾기 요청의 과도한 반복을 막는다."""
    now = time.monotonic()
    attempts = [
        attempted_at for attempted_at in _account_recovery_attempts.get(client_key, [])
        if now - attempted_at < ACCOUNT_RECOVERY_WINDOW_SECONDS
    ]
    limited = len(attempts) >= ACCOUNT_RECOVERY_MAX_ATTEMPTS
    if not limited:
        attempts.append(now)
    _account_recovery_attempts[client_key] = attempts
    return limited


def _temporary_login_password():
    letters = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    digits = '23456789'
    tail = ''.join(secrets.choice(letters + digits) for _ in range(7))
    return f'Sd!{secrets.choice(letters)}{secrets.choice(digits)}{tail}'


@app.route('/account-recovery', methods=['POST'])
def recover_login_account():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    rrn_digits = re.sub(r'\D', '', str(data.get('rrn') or ''))
    client_key = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()

    if _account_recovery_rate_limited(client_key):
        response = jsonify({
            'status': 'error',
            'message': '요청 횟수가 많습니다. 15분 후 다시 시도해주세요.',
        })
        response.headers['Cache-Control'] = 'no-store'
        return response, 429
    if not name or len(name) > 50 or len(rrn_digits) != 13:
        response = jsonify({
            'status': 'error',
            'message': '성명과 주민번호 13자리를 정확히 입력해주세요.',
        })
        response.headers['Cache-Control'] = 'no-store'
        return response, 400

    conn = None
    try:
        conn = get_db()
        conn.execute('BEGIN IMMEDIATE')
        user_row = conn.execute('''
            SELECT id, name, emp_no, email
            FROM users
            WHERE name=?
              AND REPLACE(REPLACE(COALESCE(rrn, ''), '-', ''), ' ', '')=?
              AND status='승인'
            LIMIT 1
        ''', (name, rrn_digits)).fetchone()
        if not user_row:
            conn.rollback()
            response = jsonify({
                'status': 'error',
                'message': '가입 된 내용이 존재하지 않습니다. 담당자님께 직접 연락해주세요!',
            })
            response.headers['Cache-Control'] = 'no-store'
            return response, 404

        user = dict(user_row)
        target_email = str(user.get('email') or '').strip().lower()
        emp_no = str(user.get('emp_no') or '').strip()
        if not target_email or not emp_no:
            conn.rollback()
            response = jsonify({
                'status': 'error',
                'message': '가입 이메일 또는 사번이 등록되지 않았습니다. 담당자님께 직접 연락해주세요!',
            })
            response.headers['Cache-Control'] = 'no-store'
            return response, 400

        temporary_password = _temporary_login_password()
        conn.execute(
            'UPDATE users SET password=? WHERE id=?',
            (hash_password(temporary_password), int(user['id'])),
        )
        if not send_account_recovery_email(
            target_email, user.get('name'), emp_no, temporary_password
        ):
            conn.rollback()
            response = jsonify({
                'status': 'error',
                'message': '메일 발송에 실패했습니다. 잠시 후 다시 시도하거나 담당자님께 연락해주세요.',
            })
            response.headers['Cache-Control'] = 'no-store'
            return response, 503

        conn.commit()
        response = jsonify({
            'status': 'success',
            'name': str(user.get('name') or ''),
            'email': target_email,
            'message': '가입된 e-mail로 정보가 전달되었습니다. 확인해주세요.',
        })
        response.headers['Cache-Control'] = 'no-store'
        return response
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        print(f'로그인 정보 찾기 오류: {exc}')
        response = jsonify({
            'status': 'error',
            'message': '정보 확인 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.',
        })
        response.headers['Cache-Control'] = 'no-store'
        return response, 500
    finally:
        if conn is not None:
            conn.close()

# ── 개인 근무시간(월~금) 설정 ────────────────────────────────────────────────
# weekday는 SQLite STRFTIME('%w')와 같은 규칙(1=월 ~ 5=금)을 쓴다.
WORK_SCHEDULE_WEEKDAYS = ((1, '월'), (2, '화'), (3, '수'), (4, '목'), (5, '금'))
DEFAULT_WORK_START = '09:00'
DEFAULT_WORK_END = '18:00'


def _normalize_hhmm(value):
    """'9:5', '09:05:00' 같은 입력을 'HH:MM'으로 맞춘다. 잘못된 값은 None."""
    text = str(value or '').strip()
    if not text:
        return None
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::\d{2})?', text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return '%02d:%02d' % (hour, minute)


def load_work_schedule(emp_no):
    """월~금 5개 행을 항상 채워서 돌려준다(미설정 요일은 기본값 표시용 빈 값)."""
    conn = get_db()
    try:
        rows = {
            int(row['weekday']): row
            for row in conn.execute(
                'SELECT weekday, start_time, end_time, is_off '
                'FROM user_work_schedule WHERE emp_no=?', (str(emp_no),)
            ).fetchall()
        }
    finally:
        conn.close()

    schedule = []
    for weekday, label in WORK_SCHEDULE_WEEKDAYS:
        row = rows.get(weekday)
        schedule.append({
            'weekday': weekday,
            'label': label,
            'start_time': (row['start_time'] if row else None) or '',
            'end_time': (row['end_time'] if row else None) or '',
            'is_off': bool(row['is_off']) if row else False,
            'configured': row is not None,
        })
    return schedule


def save_work_schedule(conn, emp_no, raw_payload):
    """개인정보 수정창에서 넘어온 월~금 근무시간을 저장한다."""
    try:
        entries = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, ValueError):
        raise ValueError('근무시간 형식을 읽을 수 없습니다.')
    if not isinstance(entries, list):
        raise ValueError('근무시간 형식이 올바르지 않습니다.')

    allowed = {weekday for weekday, _ in WORK_SCHEDULE_WEEKDAYS}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            weekday = int(entry.get('weekday'))
        except (TypeError, ValueError):
            continue
        if weekday not in allowed:
            continue

        is_off = bool(entry.get('is_off'))
        start_time = _normalize_hhmm(entry.get('start_time'))
        end_time = _normalize_hhmm(entry.get('end_time'))

        if not is_off and start_time and end_time and end_time <= start_time:
            raise ValueError('%s요일 퇴근시간이 출근시간보다 빠릅니다.' % dict(WORK_SCHEDULE_WEEKDAYS)[weekday])

        # 휴무도 아니고 시간도 비어 있으면 '미설정'으로 되돌린다.
        if not is_off and not start_time and not end_time:
            conn.execute(
                'DELETE FROM user_work_schedule WHERE emp_no=? AND weekday=?',
                (str(emp_no), weekday)
            )
            continue

        conn.execute('''
            INSERT INTO user_work_schedule (emp_no, weekday, start_time, end_time, is_off, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(emp_no, weekday) DO UPDATE SET
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                is_off=excluded.is_off,
                updated_at=CURRENT_TIMESTAMP
        ''', (str(emp_no), weekday, start_time, end_time, 1 if is_off else 0))


@app.route('/user/my_info')
def get_my_info():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    
    try:
        conn = get_db()
        user_row = conn.execute("SELECT * FROM users WHERE emp_no=?", (session['emp_no'],)).fetchone()
        conn.close()
        
        if not user_row:
            return jsonify({"status": "error", "message": "정보를 찾을 수 없습니다."}), 404
            
        info_dict = dict(user_row)
        if 'password' in info_dict:
            del info_dict['password']

        info_dict['work_schedule'] = load_work_schedule(session['emp_no'])

        return jsonify({"status": "success", "data": info_dict})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/user/update_my_info', methods=['POST'])
def update_my_info():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    # 상단 개인 프로필 모달은 사진 업로드를 위해 multipart/form-data를 사용합니다.
    # 기존 JSON 호출도 계속 호환되도록 둘 다 처리합니다.
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        data = request.form
        profile_file = request.files.get('profile_image')
    else:
        data = request.get_json(silent=True) or {}
        profile_file = None

    new_password = data.get('password')
    new_email = data.get('email', '')
    new_phone = data.get('phone', '')
    new_address = data.get('address', '')
    new_profile_icon = data.get('profile_icon', '👤')
    remove_profile_image = str(data.get('remove_profile_image', '')).strip().lower() in ('1', 'true', 'on')

    conn = get_db()
    new_profile_file_path = None
    old_profile_file_path = None
    try:
        # 실제 users 테이블에 컬럼이 있는지 확인
        columns = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]

        update_fields = []
        params = []

        if 'email' in columns:
            update_fields.append("email=?")
            params.append(new_email)

        if 'phone' in columns:
            update_fields.append("phone=?")
            params.append(new_phone)

        if 'address' in columns:
            update_fields.append("address=?")
            params.append(new_address)

        if 'profile_icon' in columns:
            update_fields.append("profile_icon=?")
            params.append(new_profile_icon)
            session['profile_icon'] = new_profile_icon

        if profile_file and profile_file.filename:
            if 'profile_path' not in columns:
                return jsonify({"status": "error", "message": "프로필 사진 저장 컬럼이 없습니다."}), 500

            profile_root = str(PROFILE_ROOT)
            os.makedirs(profile_root, exist_ok=True)
            display_name = original_filename(profile_file.filename, 'profile-image')
            safe_filename = encrypted_storage_name(display_name)
            upload_path = os.path.join(profile_root, safe_filename)
            encrypt_upload(profile_file, upload_path)
            new_profile_file_path = upload_path

            old_row = conn.execute(
                "SELECT profile_path FROM users WHERE emp_no=?", (session['emp_no'],)
            ).fetchone()
            if old_row and old_row['profile_path']:
                old_profile_file_path = os.path.join(
                    profile_root, os.path.basename(str(old_row['profile_path']))
                )

            profile_path = f"/user/profile_img/{safe_filename}"
            update_fields.append("profile_path=?")
            params.append(profile_path)
            session['profile_path'] = profile_path

        elif remove_profile_image and 'profile_path' in columns:
            profile_root = str(PROFILE_ROOT)
            old_row = conn.execute(
                "SELECT profile_path FROM users WHERE emp_no=?", (session['emp_no'],)
            ).fetchone()
            if old_row and old_row['profile_path']:
                old_profile_file_path = os.path.join(
                    profile_root, os.path.basename(str(old_row['profile_path']))
                )
            update_fields.append("profile_path=?")
            params.append(None)
            session['profile_path'] = ''

        if new_password and 'password' in columns:
            update_fields.append("password=?")
            params.append(hash_password(new_password))

        raw_schedule = data.get('work_schedule')

        if not update_fields and raw_schedule is None:
            return jsonify({"status": "error", "message": "수정 가능한 항목이 없습니다."}), 400

        if update_fields:
            params.append(session['emp_no'])
            conn.execute(
                f"UPDATE users SET {', '.join(update_fields)} WHERE emp_no=?",
                params
            )

        if raw_schedule is not None:
            try:
                save_work_schedule(conn, session['emp_no'], raw_schedule)
            except ValueError as schedule_error:
                conn.rollback()
                return jsonify({"status": "error", "message": str(schedule_error)}), 400

        conn.commit()
        if old_profile_file_path and old_profile_file_path != new_profile_file_path:
            delete_file(old_profile_file_path)

        return jsonify({"status": "success", "message": "정보가 성공적으로 수정되었습니다."})

    except Exception as e:
        if new_profile_file_path:
            delete_file(new_profile_file_path)
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        conn.close()

@app.route('/api/activity_feed')
def activity_feed():
    if 'emp_no' not in session:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    activities = []
    try:
        activity_user_level = int(session.get('user_level', 99))
    except (TypeError, ValueError):
        activity_user_level = 99
    is_center_director = center_director_mode_active(activity_user_level, conn)
    activity_emp_no = session.get('emp_no')

    def table_exists(table_name):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()
        return row is not None

    def get_columns(table_name):
        if not table_exists(table_name):
            return []
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]

    def add_activity(kind, icon, color_type, actor, text, created_at, url=None):
        if not created_at:
            created_at = ''
        activities.append({
            "kind": kind,
            "icon": icon,
            "color_type": color_type,
            "actor": actor or "시스템",
            "text": text or "",
            "created_at": created_at,
            "url": url or ""
        })

    try:
        # 1) 사내 게시판
        if table_exists('board_posts'):
            cols = get_columns('board_posts')
            if all(c in cols for c in ['title', 'author', 'created_at']):
                rows = conn.execute("""
                    SELECT id, title, author, created_at
                    FROM board_posts
                    ORDER BY created_at DESC
                    LIMIT 10
                """).fetchall()

                for r in rows:
                    add_activity(
                        kind="board",
                        icon="fa-bullhorn",
                        color_type="yellow",
                        actor=r['author'],
                        text=f"사내게시판에 새 글 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url="/"
                    )

        # 2) 받은 쪽지
        if table_exists('messages'):
            cols = get_columns('messages')
            if all(c in cols for c in ['sender', 'receiver', 'sent_at']):
                rows = conn.execute("""
                    SELECT sender, receiver, sent_at
                    FROM messages
                    WHERE receiver = ?
                    ORDER BY sent_at DESC
                    LIMIT 10
                """, (session.get('user_name'),)).fetchall()

                for r in rows:
                    add_activity(
                        kind="message",
                        icon="fa-envelope",
                        color_type="blue",
                        actor=r['sender'],
                        text="새 쪽지를 보냈습니다.",
                        created_at=r['sent_at'],
                        url=""
                    )

        # 3) 학교업무공간 게시글
        if table_exists('school_posts'):
            cols = get_columns('school_posts')
            if all(c in cols for c in ['title', 'author', 'created_at']):
                school_post_query = """
                    SELECT p.id, p.school_id, p.title, p.author, p.created_at,
                           s.access_key
                    FROM school_posts p
                    JOIN schools s ON s.id = p.school_id
                """
                school_post_params = []
                if is_center_director:
                    school_post_query += " WHERE s.center_director_id = ?"
                    school_post_params.append(activity_emp_no)
                school_post_query += " ORDER BY p.created_at DESC LIMIT 10"
                rows = conn.execute(
                    school_post_query,
                    school_post_params
                ).fetchall()

                for r in rows:
                    add_activity(
                        kind="school_post",
                        icon="fa-school",
                        color_type="green",
                        actor=r['author'],
                        text=f"학교업무공간에 새 글 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url=f"/school/{r['access_key']}" if r['access_key'] else "/school"
                    )

        # 4) 학교업무공간 댓글
        if table_exists('school_post_comments'):
            cols = get_columns('school_post_comments')
            if all(c in cols for c in ['author', 'content', 'created_at']):
                school_comment_query = """
                    SELECT c.id, c.post_id, c.author, c.content, c.created_at
                    FROM school_post_comments c
                    JOIN school_posts p ON p.id = c.post_id
                    JOIN schools s ON s.id = p.school_id
                """
                school_comment_params = []
                if is_center_director:
                    school_comment_query += " WHERE s.center_director_id = ?"
                    school_comment_params.append(activity_emp_no)
                school_comment_query += " ORDER BY c.created_at DESC LIMIT 10"
                rows = conn.execute(
                    school_comment_query,
                    school_comment_params
                ).fetchall()

                for r in rows:
                    content = (r['content'] or '').replace('\n', ' ')
                    if len(content) > 25:
                        content = content[:25] + '...'

                    add_activity(
                        kind="school_comment",
                        icon="fa-comment-dots",
                        color_type="purple",
                        actor=r['author'],
                        text=f"학교업무공간에 댓글을 남겼습니다. 「{content}」",
                        created_at=r['created_at'],
                        url=""
                    )

        # 5) 학교 전용 일정
        if table_exists('school_tasks'):
            cols = get_columns('school_tasks')
            if all(c in cols for c in ['title', 'owner', 'created_at']):
                school_task_query = """
                    SELECT t.id, t.school_id, t.title, t.owner, t.created_at,
                           s.access_key
                    FROM school_tasks t
                    LEFT JOIN schools s ON s.id = t.school_id
                """
                school_task_params = []
                if is_center_director:
                    school_task_query += " WHERE s.center_director_id = ?"
                    school_task_params.append(activity_emp_no)
                school_task_query += " ORDER BY t.created_at DESC LIMIT 10"
                rows = conn.execute(
                    school_task_query,
                    school_task_params
                ).fetchall()

                for r in rows:
                    add_activity(
                        kind="school_task",
                        icon="fa-calendar-check",
                        color_type="green",
                        actor=r['owner'] or "시스템",
                        text=f"학교 일정 「{r['title']}」을 등록했습니다.",
                        created_at=r['created_at'],
                        url=f"/school/{r['access_key']}" if r['access_key'] else "/school"
                    )

        # 최신순 정렬 후 20개만 반환
        activities.sort(key=lambda x: x.get('created_at') or '', reverse=True)

        conn.close()
        return jsonify({
            "status": "success",
            "activities": activities[:20]
        })

    except Exception as e:
        conn.close()
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/chat_popup/<partner>')
def chat_popup(partner):
    current_user = session.get('user_name', '알수없음')
    return render_template('chat_popup.html', partner=partner, current_user=current_user)

@app.route('/logout')
def logout():
    try:
        if session.get('emp_no'):
            record_login_activity(request, session, 'logout')
    except Exception as e:
        print(f"로그아웃 기록 오류: {e}")
    session.clear()
    return redirect(url_for('login_page'))

# =====================================================================
# [Blueprint 등록]
# =====================================================================
app.register_blueprint(chat_bp)
app.register_blueprint(main_bp)
app.register_blueprint(document_bp, url_prefix='/document')
app.register_blueprint(verified_contract_bp, url_prefix='/verified-contract')
app.register_blueprint(user_mgmt_bp, url_prefix='/user')
app.register_blueprint(approval_bp, url_prefix='/approval')
app.register_blueprint(expense_bp, url_prefix='/expense')
app.register_blueprint(board_bp, url_prefix='/board')
app.register_blueprint(payroll_bp, url_prefix='/payroll')
app.register_blueprint(ai_mail_bp, url_prefix='/ai-mail')
app.register_blueprint(ai_agent_bp)
app.register_blueprint(smart_document_bp, url_prefix='/smart-document')
app.register_blueprint(memo_bp, url_prefix='/memo')
app.register_blueprint(mydesk_bp, url_prefix='/mydesk')
app.register_blueprint(attendance_bp)
app.register_blueprint(interview_bp)
app.register_blueprint(excel_bp)       
app.register_blueprint(explorer_bp, url_prefix='/explorer')
app.register_blueprint(noti_bp)  
app.register_blueprint(gallery_bp) 
app.register_blueprint(school_bp, url_prefix='/school')
app.register_blueprint(school_task_bp, url_prefix='/school/tasks')
app.register_blueprint(survey_bp, url_prefix='/survey')
app.register_blueprint(contacts_bp)
app.register_blueprint(event_bp, url_prefix='/event-admin')
app.register_blueprint(billing_bp, url_prefix='/billing')
app.register_blueprint(portal_bp, url_prefix='/portal')
app.register_blueprint(gall2_bp)
app.register_blueprint(admin_bp, url_prefix='/admin')
app.register_blueprint(ebook_bp, url_prefix='/ebook')
app.register_blueprint(webtoon_bp, url_prefix='/webtoon')
app.register_blueprint(photobook_bp, url_prefix='/photobook')
app.register_blueprint(exhibition_bp, url_prefix='/exhibition')
app.register_blueprint(meeting_bp, url_prefix='/meeting')
app.register_blueprint(manual_bp, url_prefix='/manual')
app.register_blueprint(parent_notification_bp)
app.register_blueprint(instructor_attendance_bp)
app.register_blueprint(points_bp)
app.register_blueprint(unified_search_bp)

# 🚀 새로 분리한 메신저 블루프린트 등록 추가

@app.errorhandler(404)
def page_not_found(e):
    return "페이지를 찾을 수 없습니다. 경로를 확인해주세요.", 404

@app.errorhandler(500)
def internal_server_error(e):
    error_details = traceback.format_exc()
    return f"""
    <div style="padding:20px; border: 5px solid red; background-color: #fff0f0; font-family: monospace;">
        <h1 style="color: red;">⚠️ 500 Internal Server Error 발생</h1>
        <p><strong>발생 위치 및 원인:</strong></p>
        <pre style="background: #eee; padding: 10px; overflow-x: auto;">{error_details}</pre>
        <hr>
        <p>💡 <b>도움말:</b> 어느 파일의 몇 번째 줄에서 에러가 났는지 확인해보세요.</p>
        <a href="/">메인으로 돌아가기</a>
    </div>
    """, 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get('APP_DEBUG', '').strip().lower() in {
        '1', 'true', 'yes', 'on'
    }
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=debug_mode,
        use_reloader=debug_mode,
        allow_unsafe_werkzeug=True,
    )
