"""상단 메뉴의 레벨별 노출 및 직접 접근 권한 관리."""

from flask import jsonify, redirect, request, session

from .database import get_db
from .organization import normalize_department


SCHOOL_DIRECTOR_SCOPE_SETTING = 'school_director_scope_enabled'
# '본부전용' 메뉴 설정: 소속부서가 북부지점인 회원에게만 메뉴를 숨긴다.
DEPARTMENT_BLOCK_LABEL = '본부전용'
DEPARTMENT_BLOCK_TARGET_LABEL = '북부지점'
DEPARTMENT_BLOCK_FIELD_PREFIX = 'block_north_branch__'
SCHOOL_DIRECTOR_ALLOWED_MENUS = {'school_workspace', 'school_calendar'}
SCHOOL_DIRECTOR_MODE_EXTRA_MENUS = {'memo_main', 'ai_agent_main'}
INSTRUCTOR_EXPENSE_ACCESS_SESSION = 'expense_instructor_access_granted'
UNIFIED_SEARCH_MENU = 'unified_search'
SCHOOL_CENTER_BOARD_MENU = 'school_center_boards'
SCHOOL_CENTER_SHARED_MENU = 'school_center_shared'
# 센터장 전용 업무공간 오른쪽 이벤트 탭(이벤트관리에서 개설한 이벤트의 응모 입구)
SCHOOL_CENTER_EVENT_MENU = 'school_center_event'
SCHOOL_CENTER_SHARED_ACTION_MENUS = {
    'access': SCHOOL_CENTER_SHARED_MENU,
    'read': 'school_center_shared_read',
    'write': 'school_center_shared_write',
    'delete': 'school_center_shared_delete',
    'comment': 'school_center_shared_comment',
}
SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS = {
    'community': SCHOOL_CENTER_SHARED_MENU,
    'notice': SCHOOL_CENTER_BOARD_MENU,
    'weekly_report': SCHOOL_CENTER_BOARD_MENU,
    'open_class': SCHOOL_CENTER_BOARD_MENU,
    'expense': SCHOOL_CENTER_BOARD_MENU,
    'item_request': SCHOOL_CENTER_BOARD_MENU,
    'work_schedule': SCHOOL_CENTER_BOARD_MENU,
    'billing': SCHOOL_CENTER_BOARD_MENU,
    'survey': SCHOOL_CENTER_BOARD_MENU,
    'reference': SCHOOL_CENTER_SHARED_MENU,
    'director_resources': SCHOOL_CENTER_BOARD_MENU,
    'team_review': SCHOOL_CENTER_BOARD_MENU,
}
SCHOOL_WORKSPACE_CATEGORY_MENUS = frozenset(SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS.values())

# board_config.name_en -> 상단메뉴(업무공간)에 실제로 표시되는 이름.
# board_config.name_kr은 관리자가 게시판을 만들 때 자유롭게 입력하는 값이라 상단메뉴 이름과 어긋날 수 있어,
# 화면 제목·검색 등 "메뉴 이름"이 필요한 곳은 DB 값 대신 이 상수를 기준으로 삼는다.
# ('manual'은 board_config에 남아있는 미사용 게시판으로, 상단메뉴 '업무메뉴얼'은 routes/manual.py를 가리키므로 포함하지 않는다.)
BOARD_TOP_MENU_LABELS = {'noti': '사내 게시판', 'archive': '사내 자료실'}

MENU_GROUPS = (
    {
        'key': 'main_home',
        'label': '메인메뉴',
        'icon': 'fa-calendar-days',
        'default_max_level': 14,
        'children': (),
    },
    {
        'key': 'unified_search',
        'label': '통합검색창',
        'icon': 'fa-magnifying-glass',
        'default_max_level': 14,
        'children': (),
    },
    {
        'key': 'approval_group',
        'label': '사내결재',
        'icon': 'fa-file-signature',
        'default_max_level': 14,
        'children': (
            ('approval_main', '사내결재', 'fa-file-signature', 14),
            ('verified_contract_admin', '인증전자계약관리', 'fa-file-signature', 2),
            ('document_admin', '증명서 발급관리', 'fa-file-invoice', 14),
            ('expense_main', '지출결의 관리', 'fa-receipt', 14),
        ),
    },
    {
        'key': 'school_group',
        'label': '학교관리',
        'icon': 'fa-school',
        'default_max_level': 14,
        'children': (
            ('school_workspace', '학교업무공간', 'fa-chalkboard-user', 14),
            ('school_tasks', '학교업무처리', 'fa-list-check', 14),
            ('school_calendar', '학교일정표', 'fa-calendar-week', 14),
            ('school_survey', '설문조사', 'fa-square-poll-vertical', 14),
            ('school_billing', '청구업무', 'fa-file-invoice-dollar', 5),
            ('school_center_boards', '[센터장] 일반 게시판 (9개 메뉴 일괄)', 'fa-table-list', 14),
            ('school_center_shared', '[센터장] 본부공지사항·자료실 - 접근', 'fa-door-open', 8),
            ('school_center_shared_read', '[센터장] 본부공지사항·자료실 - 읽기', 'fa-book-open', 8),
            ('school_center_shared_write', '[센터장] 본부공지사항·자료실 - 쓰기', 'fa-pen', 5),
            ('school_center_shared_delete', '[센터장] 본부공지사항·자료실 - 삭제', 'fa-trash', 5),
            ('school_center_shared_comment', '[센터장] 본부공지사항·자료실 - 댓글', 'fa-comments', 8),
            ('school_center_event', '[센터장] 이벤트 탭', 'fa-gift', 14),
        ),
    },
    {
        'key': 'support_group',
        'label': '업무지원',
        'icon': 'fa-briefcase',
        'default_max_level': 14,
        'children': (
            ('payroll_main', '스마트 명세서 발송', 'fa-envelope-open-text', 14),
            ('smart_document_main', '스마트 공문발송', 'fa-file-circle-check', 14),
            ('ai_mail_main', '스마트 메일 발송', 'fa-wand-magic-sparkles', 14),
            ('excel_generator', '입금용 엑셀 생성기', 'fa-file-excel', 14),
            ('ebook_library', 'e리플렛', 'fa-book-open-reader', 14),
            ('parent_notifications', '학부모알림전송', 'fa-bell', 7),
            ('ai_agent_main', 'AI에이전트', 'fa-robot', 14),
        ),
    },
    {
        'key': 'workspace_group',
        'label': '업무공간',
        'icon': 'fa-layer-group',
        'default_max_level': 14,
        'children': (
            ('meeting_main', '회의센터', 'fa-users-rectangle', 14),
            ('board_noti', '사내 게시판', 'fa-clipboard-list', 14),
            ('board_archive', '사내 자료실', 'fa-folder-open', 14),
            ('gallery_main', '사내 갤러리', 'fa-images', 14),
            ('board_manual', '업무메뉴얼', 'fa-book', 14),
            ('memo_main', '개인화이트보드', 'fa-chalkboard', 14),
        ),
    },
    {
        'key': 'organization_group',
        'label': '조직관리',
        'icon': 'fa-users-gear',
        'default_max_level': 14,
        'children': (
            ('contacts_main', '본사연락망', 'fa-address-book', 14),
            ('attendance_main', '근태관리', 'fa-clock-rotate-left', 14),
            ('instructor_attendance', '강사출결시스템', 'fa-qrcode', 5),
            ('interview_main', '면접관리', 'fa-user-check', 4),
            ('event_admin', '이벤트관리', 'fa-gift', 2),
            ('organization_invite', '가입초대메일발송', 'fa-paper-plane', 2),
        ),
    },
    {
        'key': 'admin_group',
        'label': '통합관리',
        'icon': 'fa-screwdriver-wrench',
        'default_max_level': 2,
        'children': (
            ('mydesk_main', '마이데스크', 'fa-mug-hot', 2),
            ('admin_people', '인사관리', 'fa-user-gear', 2),
            ('admin_menu_permissions', '메뉴 권한관리', 'fa-key', 2),
            ('admin_boards', '게시판관리', 'fa-clipboard-list', 2),
            ('admin_disk', '디스크관리', 'fa-hard-drive', 2),
            ('admin_themes', '테마관리', 'fa-palette', 2),
            ('admin_stats', '이용통계', 'fa-chart-line', 2),
            ('admin_ai_settings', 'AI api설정', 'fa-robot', 2),
            ('admin_solapi_settings', '솔라피설정', 'fa-comment-dots', 2),
            ('admin_settings', 'Admin설정', 'fa-user-shield', 2),
            ('exhibition_main', '3D전시장', 'fa-cube', 2),
            ('webtoon_main', 'Webtoon', 'fa-book-open', 2),
            ('photobook_main', '웹전자책', 'fa-images', 2),
        ),
    },
)


def _catalog():
    result = {}
    for group in MENU_GROUPS:
        result[group['key']] = {
            'key': group['key'],
            'label': group['label'],
            'parent_key': None,
            'default_max_level': group['default_max_level'],
        }
        for key, label, _icon, default_max_level in group['children']:
            result[key] = {
                'key': key,
                'label': label,
                'parent_key': group['key'],
                'default_max_level': default_max_level,
            }
    return result


MENU_CATALOG = _catalog()


def ensure_menu_access_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS menu_access_permissions (
            menu_key TEXT PRIMARY KEY,
            max_level INTEGER NOT NULL CHECK(max_level BETWEEN -1 AND 99),
            block_north_branch INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 기존 설치본에는 소속부서(북부지점) 차단 열이 없으므로 1회만 추가한다.
    columns = {
        row[1] for row in
        conn.execute('PRAGMA table_info(menu_access_permissions)').fetchall()
    }
    if 'block_north_branch' not in columns:
        conn.execute(
            'ALTER TABLE menu_access_permissions '
            'ADD COLUMN block_north_branch INTEGER NOT NULL DEFAULT 0'
        )
        conn.commit()


def school_director_scope_enabled(conn=None):
    """레벨 7·8 센터장을 담당 센터 공간으로 제한하는 정책(기본 사용)."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        row = conn.execute(
            'SELECT value FROM admin_settings WHERE key=?',
            (SCHOOL_DIRECTOR_SCOPE_SETTING,),
        ).fetchone()
        if not row:
            return True
        return str(row['value'] or '').strip().lower() not in {'0', 'false', 'no', 'off'}
    finally:
        if owns_connection:
            conn.close()


def center_director_mode_active(user_level=None, conn=None):
    """레벨 7 센터장(팀장)·레벨 8 센터장 전용모드 적용 여부를 반환한다."""
    if is_master_admin():
        return False
    try:
        level = int(session.get('user_level', 99) if user_level is None else user_level)
    except (TypeError, ValueError):
        level = 99
    return level in {7, 8} and school_director_scope_enabled(conn)


def has_active_school_assignment(user_level=None, conn=None):
    """현재 회원이 활성 학교의 센터장으로 실제 지정됐는지 반환한다."""
    if is_master_admin() or not session.get('emp_no'):
        return False

    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        row = conn.execute(
            '''
            SELECT 1
            FROM schools
            WHERE ? IN (center_director_id, center_director_id_2)
              AND COALESCE(is_active, 1) = 1
            LIMIT 1
            ''',
            (session.get('emp_no'),),
        ).fetchone()
        return row is not None
    except Exception:
        # 초기 설치처럼 schools 테이블이 아직 준비되지 않은 경우에는
        # 일반 메뉴 권한만 적용한다.
        return False
    finally:
        if owns_connection:
            conn.close()


def load_menu_max_levels(conn=None):
    """저장된 값을 기본 권한과 합쳐 모든 메뉴 키를 반환한다."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        ensure_menu_access_schema(conn)
        values = {
            key: item['default_max_level']
            for key, item in MENU_CATALOG.items()
        }
        rows = conn.execute(
            'SELECT menu_key, max_level FROM menu_access_permissions'
        ).fetchall()
        for row in rows:
            if row['menu_key'] in values:
                values[row['menu_key']] = int(row['max_level'])
        return values
    finally:
        if owns_connection:
            conn.close()


def load_menu_department_blocks(conn=None):
    """메뉴별 '본부전용' 설정을 모든 메뉴 키에 대해 반환한다."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        ensure_menu_access_schema(conn)
        values = {key: False for key in MENU_CATALOG}
        rows = conn.execute(
            'SELECT menu_key, block_north_branch FROM menu_access_permissions'
        ).fetchall()
        for row in rows:
            if row['menu_key'] in values:
                values[row['menu_key']] = int(row['block_north_branch'] or 0) == 1
        return values
    finally:
        if owns_connection:
            conn.close()


def is_master_admin():
    return (
        str(session.get('emp_no') or '').lower() == 'admin'
        or str(session.get('user_name') or '').lower() == 'admin'
    )


def is_department_blocked_member(department=None):
    """현재 회원의 소속부서가 북부지점인지 확인한다(최고관리자는 제외)."""
    if is_master_admin():
        return False
    value = session.get('department', '') if department is None else department
    return normalize_department(value) == DEPARTMENT_BLOCK_TARGET_LABEL


def menu_blocked_for_department(menu_key, department_blocks=None):
    """본부전용으로 지정돼 북부지점 회원에게 숨기는 메뉴인지 반환한다.

    주메뉴를 숨기면 하위 메뉴도 함께 숨긴다. 이 차단은 레벨 설정이나
    센터장 전용 예외보다 우선한다.
    """
    item = MENU_CATALOG.get(menu_key)
    if not item or not is_department_blocked_member():
        return False
    blocks = department_blocks if department_blocks is not None \
        else load_menu_department_blocks()
    if blocks.get(menu_key):
        return True
    parent_key = item['parent_key']
    return bool(parent_key and blocks.get(parent_key))


def menu_is_allowed(menu_key, user_level=None, max_levels=None, department_blocks=None):
    """하위 메뉴는 자신의 권한과 주메뉴 권한을 모두 만족해야 한다."""
    item = MENU_CATALOG.get(menu_key)
    if not item:
        return True
    if is_master_admin():
        return True
    # 본부전용 설정은 레벨 설정과 센터장 예외보다 먼저 적용한다.
    if menu_blocked_for_department(menu_key, department_blocks):
        return False
    try:
        level = int(session.get('user_level', 99) if user_level is None else user_level)
    except (TypeError, ValueError):
        level = 99
    levels = max_levels or load_menu_max_levels()
    if level > int(levels.get(menu_key, item['default_max_level'])):
        return False
    # 담당 센터장은 학교관리 주메뉴의 본사 레벨 제한과 관계없이 센터장용
    # 메뉴 자체의 권한값을 우선 적용한다. 각 센터장 메뉴가 차단되면 위에서
    # 이미 False가 되므로 전용 권한 설정은 그대로 유지된다.
    if (menu_key in SCHOOL_WORKSPACE_CATEGORY_MENUS
            or menu_key == SCHOOL_CENTER_EVENT_MENU) \
            and has_active_school_assignment(level):
        return True
    parent_key = item['parent_key']
    if parent_key:
        parent = MENU_CATALOG[parent_key]
        if level > int(levels.get(parent_key, parent['default_max_level'])):
            return False
    return True


def shared_board_action_is_allowed(action, user_level=None, max_levels=None,
                                   department_blocks=None):
    """본부공지사항·자료실의 동작별 레벨 권한을 주메뉴/전용모드보다 우선한다."""
    menu_key = SCHOOL_CENTER_SHARED_ACTION_MENUS.get(str(action or '').strip())
    if not menu_key:
        return False
    if is_master_admin():
        return True
    if menu_blocked_for_department(menu_key, department_blocks):
        return False
    try:
        level = int(session.get('user_level', 99) if user_level is None else user_level)
    except (TypeError, ValueError):
        level = 99
    levels = max_levels or load_menu_max_levels()
    item = MENU_CATALOG[menu_key]
    return level <= int(levels.get(menu_key, item['default_max_level']))


def build_menu_access(user_level=None):
    levels = load_menu_max_levels()
    blocks = load_menu_department_blocks() if is_department_blocked_member() else {}
    access = {
        key: menu_is_allowed(
            key, user_level=user_level, max_levels=levels, department_blocks=blocks
        )
        for key in MENU_CATALOG
    }
    # 센터장 지정은 직급 레벨이나 로그인 당시 세션 값보다 우선한다.
    # 실제 담당 학교가 있으면 일반 메뉴 상한과 무관하게 업무공간과
    # 학교일정표를 표시한다. 다만 본부전용 설정은 그대로 지킨다.
    if has_active_school_assignment(user_level=user_level):
        if not menu_blocked_for_department('school_group', blocks):
            access['school_group'] = True
        for menu_key in SCHOOL_DIRECTOR_ALLOWED_MENUS:
            if not menu_blocked_for_department(menu_key, blocks):
                access[menu_key] = True
    return access


def _admin_menu_key(path):
    if path in {'/admin', '/admin/'} or path.startswith('/admin/system'):
        return 'admin_group'
    rules = (
        ('/admin/menu-permissions', 'admin_menu_permissions'),
        ('/admin/ai-settings', 'admin_ai_settings'),
        ('/admin/solapi-settings', 'admin_solapi_settings'),
        ('/admin/boards', 'admin_boards'),
        ('/admin/disk', 'admin_disk'),
        ('/admin/themes', 'admin_themes'),
        ('/admin/theme', 'admin_themes'),
        ('/admin/stats', 'admin_stats'),
        ('/admin/settings', 'admin_settings'),
    )
    for prefix, key in rules:
        if path.startswith(prefix):
            return key
    return 'admin_boards'


def resolve_request_menu(path, endpoint='', view_args=None):
    """현재 요청을 상단 메뉴의 하위 항목 하나에 연결한다."""
    endpoint = endpoint or ''
    view_args = view_args or {}
    if path == '/':
        return 'main_home'
    # 학부모 등록은 비회원 서비스이고, 강사 전용페이지는 자체 로그인·사번
    # 검사를 수행하므로 관리자 메뉴 권한과 분리한다.
    if path.startswith('/parent/register/') or path.startswith('/parent/api/') \
            or path == '/parent/push-sw.js' \
            or path.startswith('/parent-notifications/instructor/'):
        return None
    if path.startswith('/api/unified-search'):
        return UNIFIED_SEARCH_MENU
    if path.startswith('/parent-notifications'):
        return 'parent_notifications'
    if path.startswith('/mydesk'):
        return 'mydesk_main'
    if path.startswith('/admin'):
        return _admin_menu_key(path)
    if endpoint.startswith('user_mgmt.'):
        if endpoint in {
            'user_mgmt.invite_page', 'user_mgmt.register',
            'user_mgmt.serve_profile_image',
        }:
            return None
        if endpoint in {
            'user_mgmt.invite_sender_page',
            'user_mgmt.invite_senders',
            'user_mgmt.send_invite',
            'user_mgmt.invite_mail_template',
        }:
            return 'organization_invite'
        return 'admin_people'
    if path.startswith('/verified-contract/admin'):
        return 'verified_contract_admin'
    if path.startswith('/document/admin/settings') or path.startswith('/document/api/') \
            or path.startswith('/document/company-seal') or path.startswith('/document/company-logo'):
        return 'document_admin'
    if path.startswith('/document/admin') or path.startswith('/document/generate') \
            or path.startswith('/document/delete') or path.startswith('/document/send_simple_email') \
            or path.startswith('/document/edit') or path.startswith('/document/pdf'):
        return 'document_admin'
    if path.startswith('/approval'):
        return 'approval_main'
    # 강사용 전송페이지는 인트라넷 메뉴 레벨 대신 전용 비밀번호로 보호한다.
    if path.startswith('/expense/submit/instructor'):
        return None
    if path.startswith('/expense/submit/center'):
        return SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS['expense']
    if path.startswith('/expense'):
        return 'expense_main'
    # 설문 응답 링크는 인트라넷 계정 없이 여는 공개 주소라 메뉴 권한을 적용하지 않는다.
    if path.startswith('/survey/r/'):
        return None
    if path.startswith('/survey'):
        return 'school_survey'
    # 학교회원 포털은 인트라넷 계정이 아닌 별도 로그인으로 보호한다.
    if path == '/portal' or path.startswith('/portal/'):
        return None
    if path == '/billing' or path.startswith('/billing/'):
        return 'school_billing'
    if path.startswith('/school/tasks'):
        return 'school_tasks'
    if path.startswith('/school/calendar'):
        return 'school_calendar'
    if path.startswith('/school'):
        return 'school_workspace'
    # 발송 명세서 열람본은 라우트 내부에서 "발송자 본인 또는 스마트
    # 명세서 메뉴 접근권한"을 확인한다. 메뉴가 나중에 제한되더라도
    # 본인이 발송한 기록까지 일괄 차단하지 않도록 전역 검사와 분리한다.
    if endpoint == 'payroll.history_recipient_statement':
        return None
    if path.startswith('/payroll'):
        return 'payroll_main'
    if path.startswith('/smart-document'):
        return 'smart_document_main'
    if path.startswith('/ai-mail'):
        return 'ai_mail_main'
    if path.startswith('/ai-agent'):
        return 'ai_agent_main'
    if path.startswith('/excel-generator'):
        return 'excel_generator'
    if path.startswith('/event-admin'):
        return 'event_admin'
    if path.startswith('/attendance') or path.startswith('/api/attendance'):
        return 'attendance_main'
    # 강사 휴대폰 스캔 화면과 학교 태블릿 QR 표시 화면은 계정 없이 여는 공개 주소다.
    if path.startswith('/instructor-attendance/scan/') \
            or path.startswith('/instructor-attendance/display/'):
        return None
    if path.startswith('/instructor-attendance'):
        return 'instructor_attendance'
    # 면접자 사전질문지는 로그인 없는 공개 링크이므로 메뉴 권한 검사에서 제외한다.
    if path.startswith('/interview/q/'):
        return None
    if path.startswith('/interview'):
        return 'interview_main'
    if path.startswith('/contacts'):
        return 'contacts_main'
    if path.startswith('/ebook'):
        return 'ebook_library'
    if path.startswith('/webtoon'):
        return 'webtoon_main'
    if path.startswith('/photobook'):
        return 'photobook_main'
    if path.startswith('/board/'):
        board_key = str(view_args.get('board_en') or '').strip()
        if not board_key:
            parts = path.split('/')
            board_key = parts[2] if len(parts) > 2 else ''
        return {
            'noti': 'board_noti',
            'archive': 'board_archive',
            'manual': 'board_manual',
        }.get(board_key)
    if path.startswith('/gall2/school/'):
        return 'school_workspace'
    if path.startswith('/gall2'):
        return 'gallery_main'
    if path.startswith('/memo'):
        return 'memo_main'
    if path.startswith('/meeting'):
        return 'meeting_main'
    return None


def enforce_request_menu_access():
    """로그인 회원의 직접 URL/API 접근도 메뉴 설정과 같게 차단한다."""
    if not session.get('emp_no'):
        return None
    if session.get(INSTRUCTOR_EXPENSE_ACCESS_SESSION) and (request.path or '') in {
        '/expense/submit',
        '/expense/api/preview',
        '/expense/template',
    }:
        return None
    # 센터장 전송화면은 공용 전송/미리보기 API를 사용하므로 요청에 포함된
    # 채널값을 확인해 센터장용 지출결의 권한으로 판정한다. 일반 본부용
    # 전송 요청은 계속 expense_main 권한을 적용한다.
    expense_path = request.path or ''
    if expense_path in {'/expense/submit', '/expense/api/preview', '/expense/template'}:
        expense_channel = (
            request.args.get('channel', '')
            if expense_path == '/expense/template'
            else request.form.get('expense_submit_channel', '')
        )
        if expense_channel == 'center' \
                and has_active_school_assignment() \
                and menu_is_allowed(SCHOOL_WORKSPACE_CATEGORY_MENU_KEYS['expense']):
            return None
    menu_key = resolve_request_menu(request.path or '', request.endpoint or '', request.view_args)
    # 본부전용 메뉴는 센터장 전용모드 등 다른 예외보다 먼저 차단한다.
    if menu_key and menu_blocked_for_department(menu_key):
        message = '본부전용 메뉴로 지정되어 이용할 수 없습니다.'
        if request.is_json or '/api/' in (request.path or '') \
                or request.accept_mimetypes.best == 'application/json':
            return jsonify({'status': 'error', 'message': message}), 403
        return message, 403
    # 상단 통합검색창은 센터장 전용모드와 무관하게 자체 메뉴 권한만 따른다.
    if menu_key == UNIFIED_SEARCH_MENU:
        if menu_is_allowed(menu_key):
            return None
        return jsonify({
            'status': 'error',
            'message': '통합검색을 이용할 권한이 없습니다.',
        }), 403
    if menu_key in SCHOOL_DIRECTOR_ALLOWED_MENUS and has_active_school_assignment():
        return None
    if center_director_mode_active():
        if request.path == '/':
            return redirect('/school')
        # 전용모드는 일반 메뉴 레벨 설정보다 우선한다. 학교 화면에서 사용하는
        # 출퇴근 처리 API와 프로필 카드의 개인화이트보드는 화면 내부 기능이므로
        # 상단 메뉴를 표시하지 않고 직접 접근만 허용한다.
        if menu_key in SCHOOL_DIRECTOR_MODE_EXTRA_MENUS \
                and has_active_school_assignment():
            return None
        if menu_key in SCHOOL_WORKSPACE_CATEGORY_MENUS:
            if menu_is_allowed(menu_key):
                return None
            message = '이 센터장 업무 메뉴에 접근할 권한이 없습니다.'
            if request.is_json or '/api/' in (request.path or '') \
                    or request.accept_mimetypes.best == 'application/json':
                return jsonify({'status': 'error', 'message': message}), 403
            return message, 403
        if menu_key in {'school_workspace', 'school_calendar'} \
                or (request.path or '').startswith('/api/attendance'):
            return None
        if menu_key:
            message = '센터장은 담당 센터 업무공간, 학교일정표와 개인화이트보드만 이용할 수 있습니다.'
            if request.is_json or '/api/' in (request.path or '') \
                    or request.accept_mimetypes.best == 'application/json':
                return jsonify({'status': 'error', 'message': message}), 403
            return message, 403
    if not menu_key or menu_is_allowed(menu_key):
        return None
    message = '이 메뉴에 접근할 권한이 없습니다.'
    if request.is_json or '/api/' in (request.path or '') or request.accept_mimetypes.best == 'application/json':
        return jsonify({'status': 'error', 'message': message}), 403
    return message, 403
