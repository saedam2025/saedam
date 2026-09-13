from flask import Blueprint, render_template, session
from .socketio_ext import socketio
from .database import get_db

noti_bp = Blueprint('noti', __name__)


@socketio.on('connect', namespace='/notifications')
def notification_socket_connect():
    if not session.get('emp_no'):
        return False
    return True


def emit_notification_refresh(reason='changed'):
    """연결된 메인 화면에 업무 알림 재조회 신호를 보낸다."""
    socketio.emit(
        'notifications_changed',
        {'reason': str(reason or 'changed')},
        namespace='/notifications',
    )


@noti_bp.route('/widget/notifications')
def widget_notifications():
    current_user = session.get('user_name', '배호영') 
    conn = get_db()
    
    # 기본값 설정
    approval_pending_count = 0
    approval_draft_count = 0
    expense_wait_count = 0
    school_task_wait_count = 0
    cert_wait_count = 0      # 증명서 대기
    contract_miss_count = 0  # 인증전자계약 미완료
    
    # 1. 결재 및 쪽지, 학교업무 (SQLite DB 조회)
    try:
        # 수신대기
        pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE (approver_1 = ? AND status = '대기') OR (approver_2 = ? AND status = '1차승인')", (current_user, current_user)).fetchone()
        approval_pending_count = pending[0] if pending else 0

        # 기안함 전체
        draft = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE drafter = ? AND status <> '임시저장'",
            (current_user,)
        ).fetchone()
        approval_draft_count = draft[0] if draft else 0
        
        # 학교업무 접수 (본부 공지/자료실은 처리 대상이 아닌 공유 게시판)
        task = conn.execute("""
            SELECT COUNT(*)
            FROM school_posts
            WHERE (status = '접수' OR status IS NULL OR status = '')
              AND TRIM(COALESCE(category, '')) NOT IN (
                  'community', '본부공지사항', 'reference', '자료실'
              )
        """).fetchone()
        school_task_wait_count = task[0] if task else 0
        
        # 지출결의 대기
        expense_wait = conn.execute("""
            SELECT COUNT(*)
            FROM expense_reports
            WHERE COALESCE(doc_status, '대기') NOT IN ('완료', '반려')
        """).fetchone()
        expense_wait_count = expense_wait[0] if expense_wait else 0

        # 증명서 발급 대기
        certificate_wait = conn.execute("""
            SELECT COUNT(*)
            FROM certificate_requests
            WHERE status = '대기'
        """).fetchone()
        cert_wait_count = certificate_wait[0] if certificate_wait else 0
    except Exception as e:
        print("메인 DB 조회 오류:", e)
    finally:
        conn.close()

    # 2. 인증전자계약 미완료 건수 (saedam.db 통합 테이블)
    try:
        c_conn = get_db()
        c_row = c_conn.execute(
            "SELECT COUNT(*) FROM verified_contracts "
            "WHERE LOWER(COALESCE(status,'')) NOT IN ('completed','signed')"
        ).fetchone()
        contract_miss_count = c_row[0] if c_row else 0
        c_conn.close()
    except Exception as e:
        print("인증전자계약 DB 조회 오류:", e)

    notification_data = {
        'approval_vacation': approval_pending_count,
        'approval_draft': approval_draft_count,       
        'board_new': school_task_wait_count,          
        'cert_wait': cert_wait_count,                 # 🚀 이제 정확하게 연동됩니다
        'expense_wait': expense_wait_count,
        'contract_miss': contract_miss_count
    }

    return render_template('notifications.html', data=notification_data)
