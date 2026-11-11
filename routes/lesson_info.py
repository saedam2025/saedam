"""학교지원 > 수업안내.

학교 시간표·안내장·소식지 같은 수업 안내 자료를 학교마다 올리고, 그 학교 학부모가
[학부모 홈 > 수업안내] 탭에서 보게 한다.

- 글 하나 = 종류(시간표·안내장·소식지·기타) + 제목 + 글 내용 + 그림·PDF 파일 여러 개.
- 그림은 긴 변 2000px JPEG로 줄여 암호화 저장한다. PDF는 원본을 암호화 저장하고, 휴대폰에서 바로 보이도록
  쪽마다 미리보기 그림(최대 20쪽)도 만든다(변환할 수 없는 서버면 미리보기 없이 [PDF 열기]만 보인다).
- 학부모 화면은 로그인 없는 보호자 개인 주소(/parent/home/<토큰>?tab=lessons)이고, 파일도 그 주소 아래
  (/parent/home/<토큰>/lessons/<파일id>)로만 열린다. 보호자의 자녀(또는 수강 과목)가 그 학교일 때만 보인다.
- 숨김(비공개) 글은 학부모에게 보이지 않는다. 고정한 글은 맨 위에 온다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)

from . import instructor_hub as hub
from . import parent_notifications as pn
from . import student_attendance as sa
from .database import get_db
from .secure_files import delete_file, encrypt_bytes, encrypted_response, encrypted_storage_name
from .security import menu_permission_required
from .storage import DATA_ROOT

lesson_info_bp = Blueprint('lesson_info', __name__)
logger = logging.getLogger(__name__)

MENU_KEY = 'school_lesson_info'
LESSON_ROOT = Path(DATA_ROOT) / 'lesson_info'
# 종류: 키 → (이름, 아이콘). 화면의 선택 상자·학부모 화면의 분류 단추가 이 순서를 따른다.
CATEGORIES = {
    'timetable': ('시간표', 'fa-table-cells'),
    'letter': ('안내장', 'fa-envelope-open-text'),
    'newsletter': ('소식지', 'fa-newspaper'),
    'etc': ('기타', 'fa-thumbtack'),
}
DEFAULT_CATEGORY = 'letter'
IMAGE_EXTENSIONS = hub.IMAGE_EXTENSIONS
MAX_TITLE = 100
MAX_BODY = 5000
MAX_FILES_PER_POST = 20            # 글 하나에 붙일 수 있는 그림·PDF 수
MAX_UPLOAD_FILES = 10              # 한 번에 올리는 파일 수
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 20                 # PDF 미리보기 그림을 만드는 쪽 수(원본은 전부 보관)
IMAGE_LONG_SIDE = 2000
PDF_PAGE_LONG_SIDE = 1600
NEW_DAYS = 7                       # 올리거나 고친 지 이만큼 안 된 글에 'NEW' 표시
PARENT_LIMIT = 60                  # 학부모 화면에 보여 줄 최대 글 수

_text = hub._text
_multiline = hub._multiline
_int = hub._int
_norm = hub.norm


# ---------------------------------------------------------------- 스키마

def ensure_lesson_info_schema(conn):
    conn.executescript('''
        -- 학교별 수업안내 글
        CREATE TABLE IF NOT EXISTS li_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER,
            school_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'letter',
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            is_published INTEGER NOT NULL DEFAULT 1,
            pinned INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_li_posts_school ON li_posts(school_name);

        -- 글에 붙인 그림·PDF. PDF는 쪽 미리보기 그림 경로를 pages(JSON [[경로, 가로, 세로], …])에 둔다.
        CREATE TABLE IF NOT EXISTS li_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            width INTEGER NOT NULL DEFAULT 0,
            height INTEGER NOT NULL DEFAULT 0,
            pages TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_li_files_post ON li_files(post_id, position);
    ''')
    conn.commit()


def init_lesson_info_schema():
    LESSON_ROOT.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        ensure_lesson_info_schema(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 공통 도우미

def _now():
    """한국시간 문자열(서버는 UTC일 수 있다)."""
    return sa._now_text()


def _admin_name():
    return str(session.get('user_name') or session.get('emp_no') or '')


def _pages_of(row):
    try:
        pages = json.loads(row['pages'] or '[]')
    except (TypeError, ValueError):
        return []
    return [page for page in pages if isinstance(page, list) and len(page) == 3]


def _file_rows(conn, post_ids):
    result = {post_id: [] for post_id in post_ids}
    if not post_ids:
        return result
    marks = ','.join('?' * len(post_ids))
    for row in conn.execute(f'SELECT * FROM li_files WHERE post_id IN ({marks}) ORDER BY position, id',
                            list(post_ids)).fetchall():
        result[row['post_id']].append(row)
    return result


def _file_view(row):
    pages = _pages_of(row)
    return {'id': row['id'], 'kind': row['kind'], 'name': row['name'], 'size': row['size'],
            'width': row['width'], 'height': row['height'],
            'pages': [{'no': index, 'width': w, 'height': h} for index, (_path, w, h) in enumerate(pages, 1)]}


def _post_view(row, files):
    label, icon = CATEGORIES.get(row['category'], CATEGORIES['etc'])
    stamp = row['updated_at'] or row['created_at'] or ''
    try:
        fresh = datetime.strptime(stamp[:19], '%Y-%m-%d %H:%M:%S') >= \
            sa._now().replace(tzinfo=None) - timedelta(days=NEW_DAYS)
    except ValueError:
        fresh = False
    views = [_file_view(item) for item in files]
    return {
        'id': row['id'], 'school_name': row['school_name'], 'category': row['category'] if row['category'] in CATEGORIES
        else 'etc', 'category_label': label, 'icon': icon, 'title': row['title'], 'body': row['body'] or '',
        'published': bool(row['is_published']), 'pinned': bool(row['pinned']),
        'date': stamp[:10], 'updated_at': stamp[:16], 'created_by': row['created_by'], 'updated_by': row['updated_by'],
        'is_new': fresh, 'files': views,
        'images': sum(1 for item in views if item['kind'] == 'image'),
        'pdfs': sum(1 for item in views if item['kind'] == 'pdf'),
    }


def _order_sql():
    return 'ORDER BY pinned DESC, COALESCE(NULLIF(updated_at, \'\'), created_at) DESC, id DESC'


def school_posts(conn, school_name, *, published_only=False):
    """한 학교의 글(고정 → 최근 순)."""
    key = _norm(school_name)
    rows = [row for row in conn.execute(f'SELECT * FROM li_posts {_order_sql()}').fetchall()
            if _norm(row['school_name']) == key and (row['is_published'] or not published_only)]
    files = _file_rows(conn, [row['id'] for row in rows])
    return [_post_view(row, files[row['id']]) for row in rows]


def posts_for_schools(conn, school_names, *, category=None, limit=PARENT_LIMIT):
    """학부모 화면: 여러 학교(자녀가 다니는 학교)의 공개 글을 모아 고정 → 최근 순으로 돌려준다."""
    keys = {_norm(name) for name in school_names if _norm(name)}
    if not keys:
        return []
    rows = [row for row in conn.execute(f'SELECT * FROM li_posts WHERE is_published=1 {_order_sql()}').fetchall()
            if _norm(row['school_name']) in keys and (not category or row['category'] == category)][:limit]
    files = _file_rows(conn, [row['id'] for row in rows])
    return [_post_view(row, files[row['id']]) for row in rows]


def categories_for_schools(conn, school_names):
    """학부모 화면의 분류 단추에 쓸, 공개 글이 있는 종류(정해진 순서)."""
    keys = {_norm(name) for name in school_names if _norm(name)}
    used = {row['category'] for row in conn.execute('SELECT school_name, category FROM li_posts WHERE is_published=1')
            if _norm(row['school_name']) in keys}
    return [(key, label, icon) for key, (label, icon) in CATEGORIES.items() if key in used]


def guardian_school_keys(conn, guardian_id):
    """보호자의 자녀가 다니는 학교(학생 정보의 학교 + 수강 과목의 학교), 공백 무시 이름."""
    keys = set()
    for row in conn.execute('''
        SELECT s.school_name, sc.school_name AS catalog_name FROM parent_guardian_students gs
        JOIN parent_students s ON s.id=gs.student_id AND s.is_active=1
        LEFT JOIN schools sc ON sc.id=s.school_id
        WHERE gs.guardian_id=?
    ''', (guardian_id,)).fetchall():
        keys.update(_norm(name) for name in (row['school_name'], row['catalog_name']) if name)
    for row in conn.execute('''
        SELECT c.school_name FROM parent_guardian_students gs
        JOIN parent_students s ON s.id=gs.student_id AND s.is_active=1
        JOIN parent_class_students cs ON cs.student_id=s.id
        JOIN parent_classes c ON c.id=cs.class_id AND c.is_active=1
        WHERE gs.guardian_id=?
    ''', (guardian_id,)).fetchall():
        if row['school_name']:
            keys.add(_norm(row['school_name']))
    keys.discard('')
    return keys


# ---------------------------------------------------------------- 파일 올리기

def _prepare_uploads(uploads):
    """올린 파일을 저장 전에 모두 검사·변환한다. 하나라도 틀리면 ValueError(아무것도 저장하지 않는다)."""
    uploads = [upload for upload in uploads if upload and upload.filename]
    if len(uploads) > MAX_UPLOAD_FILES:
        raise ValueError(f'한 번에 파일 {MAX_UPLOAD_FILES}개까지 올릴 수 있습니다.')
    prepared = []
    for upload in uploads:
        name = Path(str(upload.filename).replace('\\', '/')).name[:120] or 'file'
        extension = Path(name).suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            try:
                data = hub._read_upload(upload, MAX_IMAGE_BYTES)
                jpeg, width, height = hub._jpeg(hub._open_image(data), IMAGE_LONG_SIDE, 88)
            except ValueError as exc:
                raise ValueError(f'‘{name}’: {exc}') from exc
            prepared.append({'kind': 'image', 'name': name, 'data': jpeg, 'width': width, 'height': height,
                             'pages': []})
        elif extension == '.pdf':
            try:
                data = hub._read_upload(upload, MAX_PDF_BYTES)
            except ValueError as exc:
                raise ValueError(f'‘{name}’: {exc}') from exc
            if not data.lstrip()[:5].startswith(b'%PDF'):
                raise ValueError(f'‘{name}’은(는) 정상적인 PDF가 아닙니다.')
            try:
                images = hub._pdf_images(data, MAX_PDF_PAGES)
            except RuntimeError:                       # 이 서버에 PDF 변환 도구가 없다 → 미리보기 없이 원본만
                logger.warning('수업안내 PDF 미리보기를 만들 수 없습니다(변환 도구 없음): %s', name)
                images = []
            except Exception as exc:
                raise ValueError(f'‘{name}’은(는) 정상적인 PDF가 아닙니다.') from exc
            pages = [hub._jpeg(image, PDF_PAGE_LONG_SIDE, 84) for image in images]
            first = pages[0] if pages else (b'', 0, 0)
            prepared.append({'kind': 'pdf', 'name': name, 'data': data, 'width': first[1], 'height': first[2],
                             'pages': pages})
        else:
            raise ValueError(f'‘{name}’은(는) 올릴 수 없는 형식입니다. 그림(JPG·PNG 등)이나 PDF만 올려 주세요.')
    return prepared


def _store_files(conn, post_id, prepared):
    folder = LESSON_ROOT / f'post{post_id}'
    folder.mkdir(parents=True, exist_ok=True)
    position = conn.execute('SELECT COALESCE(MAX(position), 0) FROM li_files WHERE post_id=?',
                            (post_id,)).fetchone()[0]
    for item in prepared:
        position += 1
        if item['kind'] == 'image':
            path = hub._store_jpeg(folder, 'image', item['data'])
            pages = []
        else:
            path = str(folder / encrypted_storage_name('document.pdf'))
            encrypt_bytes(item['data'], path)
            pages = [[hub._store_jpeg(folder, f'pdfpage{number}', jpeg), width, height]
                     for number, (jpeg, width, height) in enumerate(item['pages'], 1)]
        conn.execute('''
            INSERT INTO li_files (post_id, kind, position, path, name, size, width, height, pages, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (post_id, item['kind'], position, path, item['name'], len(item['data']), item['width'],
              item['height'], json.dumps(pages, ensure_ascii=False), _now()))


def _delete_file_row(conn, row):
    delete_file(row['path'])
    for path, _w, _h in _pages_of(row):
        delete_file(path)
    conn.execute('DELETE FROM li_files WHERE id=?', (row['id'],))


def delete_post(conn, post_id):
    for row in conn.execute('SELECT * FROM li_files WHERE post_id=?', (post_id,)).fetchall():
        _delete_file_row(conn, row)
    conn.execute('DELETE FROM li_posts WHERE id=?', (post_id,))
    folder = LESSON_ROOT / f'post{post_id}'
    try:
        folder.rmdir()
    except OSError:
        pass


def _serve(row, page):
    """파일 하나(그림·PDF 원본·PDF 쪽 미리보기)를 돌려준다."""
    if page:
        pages = _pages_of(row)
        if not 1 <= page <= len(pages):
            abort(404)
        path, name, mimetype = pages[page - 1][0], f'{Path(row["name"]).stem}_{page}.jpg', 'image/jpeg'
    elif row['kind'] == 'image':
        path, name, mimetype = row['path'], f'{Path(row["name"]).stem}.jpg', 'image/jpeg'
    else:
        path, name, mimetype = row['path'], row['name'], 'application/pdf'
    if not path or not Path(path).is_file():
        abort(404)
    response = encrypted_response(path, name, as_attachment=bool(request.args.get('download')), mimetype=mimetype)
    response.headers['Cache-Control'] = 'private, max-age=600'
    return response


# ---------------------------------------------------------------- 학부모 공개 화면(파일)

@lesson_info_bp.route('/parent/home/<token>/lessons/<int:file_id>')
def parent_file(token, file_id):
    """보호자 개인 주소로만 여는 수업안내 파일. 자녀의 학교에 공개된 글의 파일만 보인다."""
    conn = get_db()
    try:
        invite = pn._invite_from_token(conn, token)
        row = conn.execute('''
            SELECT f.*, p.school_name FROM li_files f JOIN li_posts p ON p.id=f.post_id AND p.is_published=1
            WHERE f.id=?
        ''', (file_id,)).fetchone() if invite else None
        if not row or _norm(row['school_name']) not in guardian_school_keys(conn, invite['guardian_id']):
            abort(404)
    finally:
        conn.close()
    response = _serve(row, _int(request.args.get('page')))
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


# ---------------------------------------------------------------- 본사 담당자 화면(인트라넷 메뉴)

def _school_choices(conn):
    """선택 상자용 학교: [학교업무공간]의 학교 + 예전에 글을 올렸지만 지금 목록에 없는 학교."""
    counts = {}
    for row in conn.execute('SELECT school_name, is_published, COALESCE(NULLIF(updated_at, \'\'), created_at) AS at '
                            'FROM li_posts').fetchall():
        item = counts.setdefault(_norm(row['school_name']), {'name': row['school_name'], 'published': 0,
                                                             'hidden': 0, 'last': ''})
        item['published' if row['is_published'] else 'hidden'] += 1
        item['last'] = max(item['last'], row['at'] or '')
    zero = {'published': 0, 'hidden': 0, 'last': ''}
    schools, known = [], set()
    for school in hub._catalog_schools(conn):
        key = _norm(school['name'])
        known.add(key)
        stats = counts.get(key, zero)
        schools.append({'name': school['name'], 'school_id': school['id'], 'matched': True,
                        'published': stats['published'], 'hidden': stats['hidden'], 'last': stats['last'][:16]})
    schools.sort(key=lambda item: item['name'])
    legacy = sorted(({'name': item['name'], 'school_id': None, 'matched': False, 'published': item['published'],
                      'hidden': item['hidden'], 'last': item['last'][:16]}
                     for key, item in counts.items() if key not in known), key=lambda item: item['name'])
    return schools, legacy


def _selected(schools, legacy):
    """고른 학교. [학부모·강사 통합지원]과 같은 '마지막으로 본 학교'를 함께 쓴다."""
    if 'school' in request.args:
        wanted = _text(request.args.get('school'), 100)
        session[hub.ADMIN_SCHOOL_SESSION] = wanted
    else:
        wanted = session.get(hub.ADMIN_SCHOOL_SESSION, '')
    key = _norm(wanted)
    return next((item for item in schools + legacy if _norm(item['name']) == key), None) if key else None


def _back(school='', **extra):
    return redirect(url_for('lesson_info.admin_page', school=school or None, **extra))


@lesson_info_bp.route('/lesson-info/')
@lesson_info_bp.route('/lesson-info')
@menu_permission_required(MENU_KEY)
def admin_page():
    from . import parent_hub as ph                     # parent_hub가 이 모듈을 불러 쓰므로 여기서만 부른다

    conn = get_db()
    try:
        schools, legacy = _school_choices(conn)
        selected = _selected(schools, legacy)
        context = {'schools': schools, 'legacy': legacy, 'selected': selected, 'categories': CATEGORIES,
                   'posts': [], 'edit': None, 'portal': None, 'limits': {
                       'files': MAX_FILES_PER_POST, 'upload': MAX_UPLOAD_FILES, 'pdf_pages': MAX_PDF_PAGES,
                       'image_mb': MAX_IMAGE_BYTES // (1024 * 1024), 'pdf_mb': MAX_PDF_BYTES // (1024 * 1024)}}
        if selected:
            posts = school_posts(conn, selected['name'])
            edit_id = _int(request.args.get('edit'))
            context['posts'] = posts
            context['edit'] = next((post for post in posts if post['id'] == edit_id), None) if edit_id else None
            portal = ph.portal_for_school(conn, selected['name'], active_only=False)
            context['portal'] = portal
            context['guardians'] = ph._school_stats(conn)[0].get(_norm(selected['name']), {}).get('guardians', 0)
        return render_template('lesson_info/admin.html', **context)
    finally:
        conn.close()


@lesson_info_bp.route('/lesson-info/posts/save', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_save_post():
    """글을 새로 쓰거나(post_id 없음) 고친다. 파일 검사에 실패하면 아무것도 바꾸지 않는다."""
    post_id = _int(request.form.get('post_id'))
    category = request.form.get('category') if request.form.get('category') in CATEGORIES else DEFAULT_CATEGORY
    title = _text(request.form.get('title'), MAX_TITLE)
    body = _multiline(request.form.get('body'), MAX_BODY)
    published = 1 if request.form.get('is_published') else 0
    pinned = 1 if request.form.get('pinned') else 0
    conn = get_db()
    try:
        if post_id:
            post = conn.execute('SELECT * FROM li_posts WHERE id=?', (post_id,)).fetchone()
            if not post:
                abort(404)
            school_id, school_name = post['school_id'], post['school_name']
        else:
            school_id, school_name = hub.canonical_school(conn, request.form.get('school_name'))
            if not school_id:
                flash('[학교업무공간]에 등록된 학교에만 수업안내를 올릴 수 있습니다.', 'error')
                return _back(school_name)
        retry = {'edit': post_id} if post_id else {'write': 1}
        if not title:
            flash('제목을 입력해 주세요.', 'error')
            return _back(school_name, **retry)
        remove_ids = {_int(value) for value in request.form.getlist('remove_file')} - {None}
        existing = conn.execute('SELECT * FROM li_files WHERE post_id=?', (post_id,)).fetchall() if post_id else []
        removing = [row for row in existing if row['id'] in remove_ids]
        try:
            prepared = _prepare_uploads(request.files.getlist('files'))
        except ValueError as exc:
            flash(f'{exc} (글은 저장하지 않았습니다.)', 'error')
            return _back(school_name, **retry)
        kept = len(existing) - len(removing)
        if kept + len(prepared) > MAX_FILES_PER_POST:
            flash(f'글 하나에는 그림·PDF를 {MAX_FILES_PER_POST}개까지 붙일 수 있습니다. '
                  f'지금 {kept}개가 있어 {len(prepared)}개를 더할 수 없습니다.', 'error')
            return _back(school_name, **retry)
        if not body and not kept and not prepared:
            flash('글 내용을 적거나 그림·PDF를 하나 이상 올려 주세요.', 'error')
            return _back(school_name, **retry)
        now, who = _now(), _admin_name()
        if post_id:
            conn.execute('''
                UPDATE li_posts SET category=?, title=?, body=?, is_published=?, pinned=?, updated_by=?, updated_at=?
                WHERE id=?
            ''', (category, title, body, published, pinned, who, now, post_id))
            for row in removing:
                _delete_file_row(conn, row)
        else:
            post_id = conn.execute('''
                INSERT INTO li_posts (school_id, school_name, category, title, body, is_published, pinned,
                                      created_by, created_at, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (school_id, school_name, category, title, body, published, pinned, who, now, who, now)).lastrowid
        _store_files(conn, post_id, prepared)
        conn.commit()
        state = '학부모에게 공개했습니다' if published else '숨김 상태로 저장했습니다(학부모에게 보이지 않음)'
        flash(f'[{CATEGORIES[category][0]}] {title} — {state}.', 'success')
        return _back(school_name)
    finally:
        conn.close()


@lesson_info_bp.route('/lesson-info/posts/<int:post_id>/<action>', methods=['POST'])
@menu_permission_required(MENU_KEY)
def admin_post_action(post_id, action):
    conn = get_db()
    try:
        post = conn.execute('SELECT * FROM li_posts WHERE id=?', (post_id,)).fetchone()
        if not post:
            abort(404)
        if action == 'toggle':
            conn.execute('UPDATE li_posts SET is_published=1-is_published WHERE id=?', (post_id,))
            flash('숨겼습니다. 학부모에게 보이지 않습니다.' if post['is_published'] else '학부모에게 공개했습니다.',
                  'success')
        elif action == 'pin':
            conn.execute('UPDATE li_posts SET pinned=1-pinned WHERE id=?', (post_id,))
            flash('고정을 풀었습니다.' if post['pinned'] else '맨 위에 고정했습니다.', 'success')
        elif action == 'delete':
            delete_post(conn, post_id)
            flash(f"‘{post['title']}’ 글과 파일을 삭제했습니다.", 'success')
        else:
            abort(404)
        conn.commit()
        return _back(post['school_name'])
    finally:
        conn.close()


@lesson_info_bp.route('/lesson-info/files/<int:file_id>')
@menu_permission_required(MENU_KEY)
def admin_file(file_id):
    """본사 화면·디자인 미리보기에서 여는 파일(숨김 글도 볼 수 있다)."""
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM li_files WHERE id=?', (file_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        abort(404)
    return _serve(row, _int(request.args.get('page')))
