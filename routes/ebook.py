"""PNG/JPG 페이지를 공개 e리플렛으로 제작·공유하는 기능."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import uuid
from html import escape
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError

from .database import get_db
from .security import is_admin_session
from .storage import DATA_ROOT
from .secure_files import (
    delete_file,
    encrypted_response,
    encrypted_storage_name,
    encrypt_upload,
    original_filename,
    read_decrypted,
)


ebook_bp = Blueprint("ebook", __name__)
EBOOK_ROOT = Path(DATA_ROOT) / "ebook_uploads"
COVER_ROOT = EBOOK_ROOT / "covers"  # 기존 e-book 표지 호환용
MEDIA_ROOT = EBOOK_ROOT / "media"
LEAFLET_ROOT = EBOOK_ROOT / "leaflets"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
IMAGE_FORMATS = {"JPEG", "PNG"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
TEXT_EXTENSIONS = {".txt", ".md"}
COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
COVER_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}
DEFAULT_PAGE_LENGTH = 1800
MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_COVER_BYTES = 12 * 1024 * 1024


def init_ebook_schema():
    """기존 텍스트 e-book DB를 보존하면서 이미지형 리플렛 필드를 추가한다."""
    for directory in (EBOOK_ROOT, COVER_ROOT, MEDIA_ROOT, LEAFLET_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                cover_filename TEXT,
                cover_path TEXT,
                source_filename TEXT,
                content_text TEXT NOT NULL DEFAULT '',
                page_char_limit INTEGER NOT NULL DEFAULT 1800,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ebook_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebook_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                content_html TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ebook_id, page_no)
            );
            CREATE TABLE IF NOT EXISTS ebook_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebook_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                filepath TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ebook_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebook_id INTEGER NOT NULL,
                user_key TEXT NOT NULL,
                slot INTEGER NOT NULL DEFAULT 1,
                page_no INTEGER NOT NULL DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ebook_id, user_key, slot)
            );
            CREATE TABLE IF NOT EXISTS ebook_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebook_id INTEGER NOT NULL,
                author_emp_no TEXT NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        ebook_columns = {row[1] for row in conn.execute("PRAGMA table_info(ebooks)")}
        if "kind" not in ebook_columns:
            conn.execute("ALTER TABLE ebooks ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
        if "share_token" not in ebook_columns:
            conn.execute("ALTER TABLE ebooks ADD COLUMN share_token TEXT")

        page_columns = {row[1] for row in conn.execute("PRAGMA table_info(ebook_pages)")}
        if "image_filename" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN image_filename TEXT")
        if "image_path" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN image_path TEXT")
        if "width" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN width INTEGER NOT NULL DEFAULT 0")
        if "height" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN height INTEGER NOT NULL DEFAULT 0")

        bookmark_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ebook_bookmarks)")
        }
        if "slot" not in bookmark_columns:
            conn.execute(
                "ALTER TABLE ebook_bookmarks ADD COLUMN slot INTEGER NOT NULL DEFAULT 1"
            )

        rows = conn.execute(
            "SELECT id FROM ebooks WHERE share_token IS NULL OR TRIM(share_token)=''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE ebooks SET share_token=? WHERE id=?",
                (secrets.token_urlsafe(24), row["id"]),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ebooks_share_token ON ebooks(share_token)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ebook_pages_book ON ebook_pages(ebook_id,page_no)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ebook_reviews_book ON ebook_reviews(ebook_id,created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ebook_bookmarks_user ON ebook_bookmarks(ebook_id,user_key,slot)"
        )
        conn.commit()
    finally:
        conn.close()


def _is_staff() -> bool:
    return bool(session.get("emp_no"))


def _require_staff() -> None:
    if not _is_staff():
        abort(401)


def _require_admin() -> None:
    if not is_admin_session():
        abort(403)


def _user_key() -> str:
    return str(session.get("emp_no") or session.get("user_name") or "").strip()


def _get_text_book(conn, ebook_id: int):
    book = conn.execute(
        "SELECT * FROM ebooks WHERE id=? AND kind='text'", (ebook_id,)
    ).fetchone()
    if not book:
        abort(404)
    return book


def _read_text(upload) -> str:
    raw = upload.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("텍스트 파일은 20MB 이하만 등록할 수 있습니다.")
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("TXT 파일의 문자 인코딩을 확인해 주세요. UTF-8 또는 CP949를 지원합니다.")


def _split_long_text(text: str, limit: int):
    pieces = []
    remaining = text.strip()
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut < max(100, limit // 3):
            cut = limit
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def paginate_text(text: str, limit: int):
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ["<p><br></p>"]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    pages = []
    current = []
    size = 0
    for paragraph in paragraphs:
        for chunk in _split_long_text(paragraph, limit):
            addition = len(chunk) + (2 if current else 0)
            if current and size + addition > limit:
                pages.append(current)
                current = []
                size = 0
            current.append(chunk)
            size += len(chunk) + (2 if len(current) > 1 else 0)
    if current:
        pages.append(current)
    return [
        "".join(f"<p>{escape(part).replace(chr(10), '<br>')}</p>" for part in page)
        for page in pages
    ]


def _save_validated_image(upload, folder: Path, max_bytes: int, formats, extensions):
    raw_name = original_filename(upload.filename, "image")
    extension = Path(raw_name).suffix.lower()
    if extension not in extensions:
        raise ValueError("표지는 JPG, PNG, GIF, WEBP 이미지만 사용할 수 있습니다.")
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > max_bytes:
        raise ValueError("이미지 파일 용량이 허용 범위를 초과했습니다.")
    try:
        with Image.open(upload.stream) as image:
            image.verify()
            if image.format not in formats:
                raise ValueError("지원하지 않는 이미지 형식입니다.")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("정상적인 이미지 파일이 아닙니다.") from exc
    finally:
        upload.stream.seek(0)
    stored_name = encrypted_storage_name(raw_name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored_name
    encrypt_upload(upload, path)
    return raw_name, stored_name, str(path)


def _sanitize_page_html(raw_html: str, allowed_media_ids):
    soup = BeautifulSoup(raw_html or "", "html.parser")
    allowed_tags = {
        "p", "br", "div", "span", "figure", "figcaption", "strong", "b",
        "em", "i", "u", "blockquote", "img",
    }
    allowed_image_classes = {
        "ebook-inline-image", "image-left", "image-right", "image-center",
        "image-size-small", "image-size-medium", "image-size-large", "image-size-full",
    }
    for tag in list(soup.find_all(["script", "style", "iframe", "object", "embed"])):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.name not in allowed_tags:
            tag.unwrap()
            continue
        if tag.name == "img":
            match = re.fullmatch(r"/ebook/media/(\d+)", str(tag.get("src") or ""))
            if not match or int(match.group(1)) not in allowed_media_ids:
                tag.decompose()
                continue
            classes = [item for item in tag.get("class", []) if item in allowed_image_classes]
            tag.attrs = {
                "src": f"/ebook/media/{int(match.group(1))}",
                "alt": str(tag.get("alt") or "본문 이미지")[:200],
                "class": classes or ["ebook-inline-image", "image-center"],
            }
        else:
            tag.attrs = {}
    cleaned = str(soup).strip()
    return cleaned or "<p><br></p>"


def _prune_unreferenced_text_media(conn, ebook_id: int):
    """편집기에서 제거된 이미지의 DB 레코드를 정리하고 삭제할 파일 경로를 반환한다."""
    used_ids = set()
    for row in conn.execute(
        "SELECT content_html FROM ebook_pages WHERE ebook_id=?", (ebook_id,)
    ).fetchall():
        used_ids.update(
            int(value)
            for value in re.findall(r'/ebook/media/(\d+)', row["content_html"] or "")
        )
    media_rows = conn.execute(
        "SELECT id,filepath FROM ebook_media WHERE ebook_id=?", (ebook_id,)
    ).fetchall()
    orphaned = [row for row in media_rows if int(row["id"]) not in used_ids]
    if orphaned:
        placeholders = ",".join("?" for _ in orphaned)
        conn.execute(
            f"DELETE FROM ebook_media WHERE ebook_id=? AND id IN ({placeholders})",
            (ebook_id, *(int(row["id"]) for row in orphaned)),
        )
    return [row["filepath"] for row in orphaned if row["filepath"]]


def _get_leaflet(conn, ebook_id: int):
    book = conn.execute(
        "SELECT * FROM ebooks WHERE id=? AND kind='leaflet'", (ebook_id,)
    ).fetchone()
    if not book:
        abort(404)
    return book


def _publicly_visible(book) -> bool:
    """강사 홍보 리플렛처럼 본사 승인이 필요한 리플렛은 승인 전에는 외부에 보이지 않는다.

    승인 열(approval_status)이 없거나 'approved'면 예전처럼 공개하고,
    인트라넷에 로그인한 직원은 승인 전에도 미리 볼 수 있다.
    """
    try:
        status = str(book["approval_status"] or "approved")
    except (IndexError, KeyError):
        status = "approved"
    return status == "approved" or _is_staff()


def _get_shared_leaflet(conn, token: str):
    book = conn.execute(
        "SELECT * FROM ebooks WHERE share_token=? AND kind='leaflet'", (token,)
    ).fetchone()
    if not book or not _publicly_visible(book):
        abort(404)
    return book


def _natural_image_key(upload):
    name = Path((upload.filename or "").replace("\\", "/")).name.casefold()
    return [
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", name)
    ]


def _selected_images(files):
    images = [upload for upload in files if upload and upload.filename]
    if not images:
        raise ValueError("PNG 또는 JPG 페이지를 한 장 이상 선택해 주세요.")
    if len(images) > MAX_PAGES:
        raise ValueError(f"한 리플렛에는 최대 {MAX_PAGES}장까지 올릴 수 있습니다.")
    for upload in images:
        extension = Path(upload.filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise ValueError("PNG, JPG, JPEG 이미지만 올릴 수 있습니다.")
    return sorted(images, key=_natural_image_key)


def _save_page_image(upload, folder: Path):
    raw_name = original_filename(upload.filename, "page")
    extension = Path(raw_name).suffix.lower()
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"‘{raw_name}’ 파일이 20MB를 초과합니다.")
    width = 0
    height = 0
    try:
        with Image.open(upload.stream) as image:
            width, height = image.size
            image.verify()
            if image.format not in IMAGE_FORMATS:
                raise ValueError("PNG, JPG, JPEG 이미지만 올릴 수 있습니다.")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"‘{raw_name}’은(는) 정상적인 이미지 파일이 아닙니다.") from exc
    finally:
        upload.stream.seek(0)

    stored_name = encrypted_storage_name(raw_name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored_name
    encrypt_upload(upload, path)
    return raw_name, str(path), int(width), int(height)


def _leaflet_pages(conn, ebook_id: int):
    pages = conn.execute(
        """SELECT id,page_no,image_filename,image_path,width,height
           FROM ebook_pages WHERE ebook_id=? ORDER BY page_no""",
        (ebook_id,),
    ).fetchall()
    updated = False
    for page in pages:
        if int(page["width"] or 0) > 0 and int(page["height"] or 0) > 0:
            continue
        path = page["image_path"]
        if not path or not os.path.isfile(path):
            continue
        try:
            with Image.open(BytesIO(read_decrypted(path, MAX_IMAGE_BYTES + 1))) as image:
                width, height = image.size
            if width > 0 and height > 0:
                conn.execute(
                    "UPDATE ebook_pages SET width=?,height=? WHERE id=?",
                    (int(width), int(height), page["id"]),
                )
                updated = True
        except (OSError, ValueError, UnidentifiedImageError):
            continue
    if updated:
        conn.commit()
        pages = conn.execute(
            """SELECT id,page_no,image_filename,image_path,width,height
               FROM ebook_pages WHERE ebook_id=? ORDER BY page_no""",
            (ebook_id,),
        ).fetchall()
    return pages


def _reader_context(book, pages):
    public_url = url_for("ebook.public_reader", token=book["share_token"], _external=True)
    return {
        "book": book,
        "pages": pages,
        "public_url": public_url,
        "can_manage": _is_staff(),
    }


@ebook_bp.route("/")
def library():
    query = (request.args.get("q") or "").strip()
    where = "WHERE e.kind='leaflet'"
    params = []
    if query:
        where += " AND (e.title LIKE ? OR e.description LIKE ?)"
        params.extend((f"%{query}%", f"%{query}%"))
    conn = get_db()
    try:
        books = conn.execute(
            f"""
            SELECT e.*, COUNT(p.id) AS page_count
            FROM ebooks e LEFT JOIN ebook_pages p ON p.ebook_id=e.id
            {where}
            GROUP BY e.id ORDER BY e.updated_at DESC,e.id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "ebook/library.html", books=books, query=query, can_manage=_is_staff()
    )


@ebook_bp.route("/new", methods=["GET", "POST"])
def create_book():
    _require_staff()
    if request.method == "GET":
        return render_template("ebook/form.html")

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not title:
        flash("제목을 입력해 주세요.", "error")
        return render_template("ebook/form.html"), 400
    if len(title) > 200 or len(description) > 2000:
        flash("제목 또는 소개 글이 너무 깁니다.", "error")
        return render_template("ebook/form.html"), 400
    try:
        images = _selected_images(request.files.getlist("page_images"))
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("ebook/form.html"), 400

    conn = get_db()
    leaflet_dir = None
    try:
        token = secrets.token_urlsafe(24)
        creator = session.get("user_name") or str(session.get("emp_no"))
        cursor = conn.execute(
            """INSERT INTO ebooks
               (title,author,description,cover_filename,cover_path,source_filename,
                content_text,page_char_limit,created_by,kind,share_token)
               VALUES (?,?,?,'','','','',1800,?,'leaflet',?)""",
            (title, creator, description, creator, token),
        )
        ebook_id = cursor.lastrowid
        leaflet_dir = LEAFLET_ROOT / str(ebook_id)
        saved_pages = []
        for page_no, upload in enumerate(images, 1):
            filename, path, width, height = _save_page_image(upload, leaflet_dir)
            saved_pages.append((ebook_id, page_no, "", filename, path, width, height))
        conn.executemany(
            """INSERT INTO ebook_pages
               (ebook_id,page_no,content_html,image_filename,image_path,width,height)
               VALUES (?,?,?,?,?,?,?)""",
            saved_pages,
        )
        first_filename, first_path = saved_pages[0][3], saved_pages[0][4]
        conn.execute(
            """UPDATE ebooks SET cover_filename=?,cover_path=?,source_filename=?
               WHERE id=?""",
            (first_filename, first_path, first_filename, ebook_id),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        if leaflet_dir and leaflet_dir.is_dir():
            shutil.rmtree(leaflet_dir, ignore_errors=True)
        flash(str(exc), "error")
        return render_template("ebook/form.html"), 400
    except Exception:
        conn.rollback()
        if leaflet_dir and leaflet_dir.is_dir():
            shutil.rmtree(leaflet_dir, ignore_errors=True)
        raise
    finally:
        conn.close()

    flash(f"‘{title}’ e리플렛을 {len(images)}페이지로 만들었습니다.", "success")
    return redirect(url_for("ebook.library"))


@ebook_bp.route("/<int:ebook_id>")
def read_book(ebook_id):
    conn = get_db()
    try:
        book = conn.execute("SELECT * FROM ebooks WHERE id=?", (ebook_id,)).fetchone()
        if not book:
            abort(404)
    finally:
        conn.close()
    if book["kind"] == "text":
        return redirect(url_for("ebook.read_text_book", ebook_id=ebook_id))
    return redirect(url_for("ebook.public_reader", token=book["share_token"]))


@ebook_bp.route("/view/<token>")
def public_reader(token):
    conn = get_db()
    try:
        book = _get_shared_leaflet(conn, token)
        pages = _leaflet_pages(conn, book["id"])
    finally:
        conn.close()
    return render_template("ebook/reader.html", **_reader_context(book, pages))


@ebook_bp.route("/<int:ebook_id>/edit", methods=["GET", "POST"])
def edit_book(ebook_id):
    _require_staff()
    conn = get_db()
    replacement_dir = None
    backup_dir = None
    final_dir = LEAFLET_ROOT / str(ebook_id)
    filesystem_swapped = False
    try:
        kind_row = conn.execute("SELECT kind FROM ebooks WHERE id=?", (ebook_id,)).fetchone()
        if kind_row and kind_row["kind"] == "text":
            conn.close()
            conn = None
            return redirect(url_for("ebook.edit_text_book", ebook_id=ebook_id))
        book = _get_leaflet(conn, ebook_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            if not title:
                flash("제목을 입력해 주세요.", "error")
                return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
            if len(title) > 200 or len(description) > 2000:
                flash("제목 또는 소개 글이 너무 깁니다.", "error")
                return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))

            uploads = [item for item in request.files.getlist("page_images") if item.filename]
            new_pages = []
            if uploads:
                images = _selected_images(uploads)
                replacement_dir = LEAFLET_ROOT / f"{ebook_id}_replacement_{uuid.uuid4().hex}"
                for page_no, upload in enumerate(images, 1):
                    filename, path, width, height = _save_page_image(upload, replacement_dir)
                    new_pages.append((ebook_id, page_no, "", filename, path, width, height))

                # 새 파일을 먼저 완성한 뒤 폴더를 교체한다. DB 저장이
                # 실패하면 백업 폴더로 즉시 되돌릴 수 있다.
                backup_dir = LEAFLET_ROOT / f"{ebook_id}_backup_{uuid.uuid4().hex}"
                if final_dir.is_dir():
                    final_dir.rename(backup_dir)
                replacement_dir.rename(final_dir)
                replacement_dir = None
                filesystem_swapped = True
                new_pages = [
                    (
                        row[0], row[1], row[2], row[3],
                        str(final_dir / Path(row[4]).name), row[5], row[6],
                    )
                    for row in new_pages
                ]
                conn.execute("DELETE FROM ebook_pages WHERE ebook_id=?", (ebook_id,))
                conn.executemany(
                    """INSERT INTO ebook_pages
                       (ebook_id,page_no,content_html,image_filename,image_path,width,height)
                       VALUES (?,?,?,?,?,?,?)""",
                    new_pages,
                )
                cover_filename, cover_path = new_pages[0][3], new_pages[0][4]
            else:
                cover_filename, cover_path = book["cover_filename"], book["cover_path"]

            conn.execute(
                """UPDATE ebooks SET title=?,description=?,cover_filename=?,cover_path=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title, description, cover_filename, cover_path, ebook_id),
            )
            conn.commit()
            if backup_dir and backup_dir.is_dir():
                shutil.rmtree(backup_dir, ignore_errors=True)
            flash("리플렛 정보를 수정했습니다.", "success")
            return redirect(url_for("ebook.library"))

        pages = _leaflet_pages(conn, ebook_id)
    except ValueError as exc:
        conn.rollback()
        if replacement_dir and replacement_dir.is_dir():
            shutil.rmtree(replacement_dir, ignore_errors=True)
        if filesystem_swapped:
            if final_dir.is_dir():
                shutil.rmtree(final_dir, ignore_errors=True)
            if backup_dir and backup_dir.is_dir():
                backup_dir.rename(final_dir)
        flash(str(exc), "error")
        return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
    except Exception:
        conn.rollback()
        if replacement_dir and replacement_dir.is_dir():
            shutil.rmtree(replacement_dir, ignore_errors=True)
        if filesystem_swapped:
            if final_dir.is_dir():
                shutil.rmtree(final_dir, ignore_errors=True)
            if backup_dir and backup_dir.is_dir():
                backup_dir.rename(final_dir)
        raise
    finally:
        if conn is not None:
            conn.close()
    return render_template(
        "ebook/edit.html",
        book=book,
        pages=pages,
        public_url=url_for("ebook.public_reader", token=book["share_token"], _external=True),
    )


@ebook_bp.route("/<int:ebook_id>/cover")
def serve_cover(ebook_id):
    conn = get_db()
    try:
        book = conn.execute("SELECT * FROM ebooks WHERE id=?", (ebook_id,)).fetchone()
        if not book or not _publicly_visible(book):
            abort(404)
        cover_path = book["cover_path"]
    finally:
        conn.close()
    if not cover_path or not os.path.isfile(cover_path):
        abort(404)
    return encrypted_response(
        cover_path,
        book["cover_filename"] or "cover.jpg",
        as_attachment=False,
    )


@ebook_bp.route("/<int:ebook_id>/pages/<int:page_id>/image")
def serve_page_image(ebook_id, page_id):
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT p.image_path,p.image_filename,e.* FROM ebook_pages p
               JOIN ebooks e ON e.id=p.ebook_id
               WHERE p.id=? AND p.ebook_id=? AND e.kind='leaflet'""",
            (page_id, ebook_id),
        ).fetchone()
    finally:
        conn.close()
    if row and not _publicly_visible(row):
        abort(404)
    if not row or not row["image_path"] or not os.path.isfile(row["image_path"]):
        abort(404)
    return encrypted_response(
        row["image_path"], row["image_filename"] or "page.jpg", as_attachment=False
    )


@ebook_bp.route("/<int:ebook_id>/delete", methods=["POST"])
def delete_book(ebook_id):
    _require_staff()
    conn = get_db()
    directory = LEAFLET_ROOT / str(ebook_id)
    try:
        kind_row = conn.execute("SELECT kind FROM ebooks WHERE id=?", (ebook_id,)).fetchone()
        if kind_row and kind_row["kind"] == "text":
            conn.close()
            conn = None
            return delete_text_book(ebook_id)
        _get_leaflet(conn, ebook_id)
        conn.execute("DELETE FROM ebook_pages WHERE ebook_id=?", (ebook_id,))
        for table in ("ebook_reviews", "ebook_bookmarks", "ebook_media"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                conn.execute(f"DELETE FROM {table} WHERE ebook_id=?", (ebook_id,))
        conn.execute("DELETE FROM ebooks WHERE id=?", (ebook_id,))
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    flash("e리플렛을 삭제했습니다.", "success")
    return redirect(url_for("ebook.library"))


# ---------------------------------------------------------------------------
# TXT eBook library
# 이미지형 e리플렛과 별도 URL을 사용해 기존 텍스트 도서와 독서 기록을 보존한다.
# ---------------------------------------------------------------------------


@ebook_bp.route("/books")
def text_library():
    _require_staff()
    query = (request.args.get("q") or "").strip()
    where = "WHERE e.kind='text'"
    params = []
    if query:
        where += " AND (e.title LIKE ? OR e.author LIKE ?)"
        params.extend((f"%{query}%", f"%{query}%"))
    conn = get_db()
    try:
        books = conn.execute(
            f"""
            SELECT e.*, COUNT(DISTINCT p.id) AS page_count,
                   COUNT(DISTINCT r.id) AS review_count
            FROM ebooks e
            LEFT JOIN ebook_pages p ON p.ebook_id=e.id
            LEFT JOIN ebook_reviews r ON r.ebook_id=e.id
            {where}
            GROUP BY e.id
            ORDER BY e.updated_at DESC,e.id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "ebook/text_library.html",
        books=books,
        query=query,
        can_manage=is_admin_session(),
    )


@ebook_bp.route("/books/new", methods=["GET", "POST"])
def create_text_book():
    _require_admin()
    if request.method == "GET":
        return render_template("ebook/text_form.html")

    title = (request.form.get("title") or "").strip()
    author = (request.form.get("author") or "").strip()
    description = (request.form.get("description") or "").strip()
    text_file = request.files.get("text_file")
    cover = request.files.get("cover")
    try:
        page_length = int(request.form.get("page_length") or DEFAULT_PAGE_LENGTH)
    except (TypeError, ValueError):
        page_length = DEFAULT_PAGE_LENGTH

    if not title or not author:
        flash("제목과 저자를 입력해 주세요.", "error")
        return render_template("ebook/text_form.html"), 400
    if not text_file or not text_file.filename:
        flash("TXT 또는 MD 파일을 선택해 주세요.", "error")
        return render_template("ebook/text_form.html"), 400
    if Path(text_file.filename).suffix.lower() not in TEXT_EXTENSIONS:
        flash("TXT 또는 MD 파일만 등록할 수 있습니다.", "error")
        return render_template("ebook/text_form.html"), 400
    page_length = min(5000, max(500, page_length))

    conn = get_db()
    ebook_id = None
    saved_cover = None
    try:
        content_text = _read_text(text_file)
        page_html = paginate_text(content_text, page_length)
        creator = _user_key()
        cursor = conn.execute(
            """INSERT INTO ebooks
               (title,author,description,cover_filename,cover_path,source_filename,
                content_text,page_char_limit,created_by,kind,share_token)
               VALUES (?,?,?,'','',?,?,?,?, 'text',?)""",
            (
                title,
                author,
                description,
                Path(text_file.filename).name,
                content_text,
                page_length,
                creator,
                secrets.token_urlsafe(24),
            ),
        )
        ebook_id = cursor.lastrowid
        if cover and cover.filename:
            cover_name, _stored_name, cover_path = _save_validated_image(
                cover, COVER_ROOT / str(ebook_id), MAX_COVER_BYTES,
                COVER_FORMATS, COVER_EXTENSIONS,
            )
            saved_cover = cover_path
            conn.execute(
                "UPDATE ebooks SET cover_filename=?,cover_path=? WHERE id=?",
                (cover_name, cover_path, ebook_id),
            )
        conn.executemany(
            "INSERT INTO ebook_pages (ebook_id,page_no,content_html) VALUES (?,?,?)",
            ((ebook_id, page_no, html) for page_no, html in enumerate(page_html, 1)),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        if saved_cover and os.path.isfile(saved_cover):
            delete_file(saved_cover)
        flash(str(exc), "error")
        return render_template("ebook/text_form.html"), 400
    except Exception:
        conn.rollback()
        if saved_cover and os.path.isfile(saved_cover):
            delete_file(saved_cover)
        raise
    finally:
        conn.close()

    flash(f"‘{title}’ eBook을 {len(page_html):,}페이지로 등록했습니다.", "success")
    return redirect(url_for("ebook.text_library"))


@ebook_bp.route("/books/<int:ebook_id>")
def read_text_book(ebook_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_text_book(conn, ebook_id)
        pages = [dict(row) for row in conn.execute(
            "SELECT id,page_no,content_html FROM ebook_pages WHERE ebook_id=? ORDER BY page_no",
            (ebook_id,),
        ).fetchall()]
        bookmarks = [dict(row) for row in conn.execute(
            """SELECT slot,page_no,updated_at FROM ebook_bookmarks
               WHERE ebook_id=? AND user_key=? ORDER BY slot""",
            (ebook_id, _user_key()),
        ).fetchall()]
        reviews = conn.execute(
            "SELECT * FROM ebook_reviews WHERE ebook_id=? ORDER BY created_at DESC,id DESC",
            (ebook_id,),
        ).fetchall()
    finally:
        conn.close()
    if not pages:
        abort(404)
    requested_page = request.args.get("page", type=int)
    initial_page = requested_page or (bookmarks[0]["page_no"] if bookmarks else 1)
    initial_page = max(1, min(len(pages), int(initial_page)))
    return render_template(
        "ebook/text_reader.html",
        book=book,
        pages=pages,
        bookmarks=bookmarks,
        reviews=reviews,
        initial_page=initial_page,
        can_manage=is_admin_session(),
        current_emp_no=_user_key(),
    )


@ebook_bp.route("/<int:ebook_id>/bookmark", methods=["POST"])
@ebook_bp.route("/books/<int:ebook_id>/bookmark", methods=["POST"])
def save_text_bookmark(ebook_id):
    _require_staff()
    data = request.get_json(silent=True) or {}
    try:
        slot = max(1, min(5, int(data.get("slot") or 1)))
        page_no = int(data.get("page_no") or 0)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "잘못된 책갈피 값입니다."}), 400
    conn = get_db()
    try:
        _get_text_book(conn, ebook_id)
        maximum = conn.execute(
            "SELECT COALESCE(MAX(page_no),1) FROM ebook_pages WHERE ebook_id=?",
            (ebook_id,),
        ).fetchone()[0]
        if page_no <= 0:
            conn.execute(
                "DELETE FROM ebook_bookmarks WHERE ebook_id=? AND user_key=? AND slot=?",
                (ebook_id, _user_key(), slot),
            )
        else:
            page_no = max(1, min(int(maximum), page_no))
            conn.execute(
                """INSERT INTO ebook_bookmarks
                   (ebook_id,user_key,slot,page_no,updated_at)
                   VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT DO UPDATE SET slot=excluded.slot,
                   page_no=excluded.page_no,updated_at=CURRENT_TIMESTAMP""",
                (ebook_id, _user_key(), slot, page_no),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "slot": slot, "page_no": max(0, page_no)})


@ebook_bp.route("/<int:ebook_id>/reviews", methods=["POST"])
@ebook_bp.route("/books/<int:ebook_id>/reviews", methods=["POST"])
def create_text_review(ebook_id):
    _require_staff()
    content = (request.form.get("content") or "").strip()
    if not content or len(content) > 10000:
        flash("독후감은 1자 이상 10,000자 이하로 입력해 주세요.", "error")
        return redirect(url_for("ebook.read_text_book", ebook_id=ebook_id) + "#reviews")
    conn = get_db()
    try:
        _get_text_book(conn, ebook_id)
        conn.execute(
            """INSERT INTO ebook_reviews
               (ebook_id,author_emp_no,author_name,content) VALUES (?,?,?,?)""",
            (
                ebook_id,
                _user_key(),
                str(session.get("user_name") or _user_key()),
                content,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    flash("독후감을 등록했습니다.", "success")
    return redirect(url_for("ebook.read_text_book", ebook_id=ebook_id) + "#reviews")


@ebook_bp.route(
    "/books/<int:ebook_id>/reviews/<int:review_id>/delete", methods=["POST"]
)
@ebook_bp.route(
    "/<int:ebook_id>/reviews/<int:review_id>/delete", methods=["POST"]
)
def delete_text_review(ebook_id, review_id):
    _require_staff()
    conn = get_db()
    try:
        review = conn.execute(
            "SELECT * FROM ebook_reviews WHERE id=? AND ebook_id=?",
            (review_id, ebook_id),
        ).fetchone()
        if not review:
            abort(404)
        if not is_admin_session() and str(review["author_emp_no"]) != _user_key():
            abort(403)
        conn.execute("DELETE FROM ebook_reviews WHERE id=?", (review_id,))
        conn.commit()
    finally:
        conn.close()
    flash("독후감을 삭제했습니다.", "success")
    return redirect(url_for("ebook.read_text_book", ebook_id=ebook_id) + "#reviews")


@ebook_bp.route("/books/<int:ebook_id>/edit", methods=["GET", "POST"])
def edit_text_book(ebook_id):
    _require_admin()
    conn = get_db()
    try:
        book = _get_text_book(conn, ebook_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            author = (request.form.get("author") or "").strip()
            description = (request.form.get("description") or "").strip()
            if not title or not author:
                flash("제목과 저자를 입력해 주세요.", "error")
                return redirect(url_for("ebook.edit_text_book", ebook_id=ebook_id))
            cover_name, cover_path = book["cover_filename"], book["cover_path"]
            cover = request.files.get("cover")
            old_cover = None
            if cover and cover.filename:
                new_name, _stored_name, new_path = _save_validated_image(
                    cover, COVER_ROOT / str(ebook_id), MAX_COVER_BYTES,
                    COVER_FORMATS, COVER_EXTENSIONS,
                )
                old_cover = cover_path
                cover_name, cover_path = new_name, new_path
            conn.execute(
                """UPDATE ebooks SET title=?,author=?,description=?,cover_filename=?,
                   cover_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title, author, description, cover_name, cover_path, ebook_id),
            )
            conn.commit()
            if old_cover and old_cover != cover_path and os.path.isfile(old_cover):
                delete_file(old_cover)
            flash("eBook 정보를 수정했습니다.", "success")
            return redirect(url_for("ebook.edit_text_book", ebook_id=ebook_id))
        pages = [dict(row) for row in conn.execute(
            "SELECT id,page_no,content_html FROM ebook_pages WHERE ebook_id=? ORDER BY page_no",
            (ebook_id,),
        ).fetchall()]
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "error")
        return redirect(url_for("ebook.edit_text_book", ebook_id=ebook_id))
    finally:
        conn.close()
    return render_template("ebook/text_edit.html", book=book, pages=pages)


@ebook_bp.route(
    "/books/<int:ebook_id>/pages/<int:page_id>", methods=["POST"]
)
@ebook_bp.route("/<int:ebook_id>/pages/<int:page_id>", methods=["POST"])
def update_text_page(ebook_id, page_id):
    _require_admin()
    data = request.get_json(silent=True) or {}
    conn = get_db()
    orphaned_paths = []
    try:
        _get_text_book(conn, ebook_id)
        allowed_ids = {
            row["id"] for row in conn.execute(
                "SELECT id FROM ebook_media WHERE ebook_id=?", (ebook_id,)
            ).fetchall()
        }
        cleaned = _sanitize_page_html(str(data.get("content_html") or ""), allowed_ids)
        cursor = conn.execute(
            """UPDATE ebook_pages SET content_html=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND ebook_id=?""",
            (cleaned, page_id, ebook_id),
        )
        if not cursor.rowcount:
            abort(404)
        conn.execute(
            "UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,)
        )
        orphaned_paths = _prune_unreferenced_text_media(conn, ebook_id)
        conn.commit()
    finally:
        conn.close()
    for path in orphaned_paths:
        delete_file(path)
    return jsonify({"status": "success", "content_html": cleaned})


@ebook_bp.route("/<int:ebook_id>/pages", methods=["POST"])
@ebook_bp.route("/books/<int:ebook_id>/pages", methods=["POST"])
def add_text_page(ebook_id):
    _require_admin()
    conn = get_db()
    try:
        _get_text_book(conn, ebook_id)
        page_no = conn.execute(
            "SELECT COALESCE(MAX(page_no),0)+1 FROM ebook_pages WHERE ebook_id=?",
            (ebook_id,),
        ).fetchone()[0]
        cursor = conn.execute(
            """INSERT INTO ebook_pages (ebook_id,page_no,content_html)
               VALUES (?,?,'<p><br></p>')""",
            (ebook_id, page_no),
        )
        conn.execute(
            "UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "success", "id": cursor.lastrowid, "page_no": page_no})


@ebook_bp.route(
    "/books/<int:ebook_id>/pages/<int:page_id>/delete", methods=["POST"]
)
@ebook_bp.route(
    "/<int:ebook_id>/pages/<int:page_id>/delete", methods=["POST"]
)
def delete_text_page(ebook_id, page_id):
    _require_admin()
    conn = get_db()
    orphaned_paths = []
    try:
        _get_text_book(conn, ebook_id)
        count = conn.execute(
            "SELECT COUNT(*) FROM ebook_pages WHERE ebook_id=?", (ebook_id,)
        ).fetchone()[0]
        if count <= 1:
            return jsonify({"status": "error", "message": "마지막 페이지는 삭제할 수 없습니다."}), 400
        row = conn.execute(
            "SELECT page_no FROM ebook_pages WHERE id=? AND ebook_id=?",
            (page_id, ebook_id),
        ).fetchone()
        if not row:
            abort(404)
        conn.execute("DELETE FROM ebook_pages WHERE id=?", (page_id,))
        conn.execute(
            "UPDATE ebook_pages SET page_no=page_no-1 WHERE ebook_id=? AND page_no>?",
            (ebook_id, row["page_no"]),
        )
        conn.execute(
            "UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,)
        )
        orphaned_paths = _prune_unreferenced_text_media(conn, ebook_id)
        conn.commit()
    finally:
        conn.close()
    for path in orphaned_paths:
        delete_file(path)
    return jsonify({"status": "success"})


@ebook_bp.route("/<int:ebook_id>/media", methods=["POST"])
@ebook_bp.route("/books/<int:ebook_id>/media", methods=["POST"])
def upload_text_media(ebook_id):
    _require_admin()
    upload = request.files.get("image")
    if not upload or not upload.filename:
        return jsonify({"status": "error", "message": "이미지를 선택해 주세요."}), 400
    conn = get_db()
    saved_path = None
    try:
        _get_text_book(conn, ebook_id)
        original, stored, saved_path = _save_validated_image(
            upload, MEDIA_ROOT / str(ebook_id), MAX_COVER_BYTES,
            COVER_FORMATS, COVER_EXTENSIONS,
        )
        cursor = conn.execute(
            """INSERT INTO ebook_media
               (ebook_id,original_name,stored_name,filepath,created_by)
               VALUES (?,?,?,?,?)""",
            (ebook_id, original, stored, saved_path, _user_key()),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        if saved_path and os.path.isfile(saved_path):
            delete_file(saved_path)
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception:
        conn.rollback()
        delete_file(saved_path)
        raise
    finally:
        conn.close()
    media_id = cursor.lastrowid
    return jsonify({
        "status": "success",
        "id": media_id,
        "url": url_for("ebook.serve_text_media", media_id=media_id),
    })


@ebook_bp.route("/media/<int:media_id>")
def serve_text_media(media_id):
    _require_staff()
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT m.filepath,m.original_name FROM ebook_media m
               JOIN ebooks e ON e.id=m.ebook_id
               WHERE m.id=? AND e.kind='text'""",
            (media_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not os.path.isfile(row["filepath"]):
        abort(404)
    return encrypted_response(
        row["filepath"], row["original_name"] or "image", as_attachment=False
    )


@ebook_bp.route("/books/<int:ebook_id>/delete", methods=["POST"])
def delete_text_book(ebook_id):
    _require_admin()
    conn = get_db()
    paths = []
    try:
        book = _get_text_book(conn, ebook_id)
        if book["cover_path"]:
            paths.append(book["cover_path"])
        paths.extend(
            row["filepath"] for row in conn.execute(
                "SELECT filepath FROM ebook_media WHERE ebook_id=?", (ebook_id,)
            ).fetchall()
        )
        for table in ("ebook_reviews", "ebook_bookmarks", "ebook_media", "ebook_pages"):
            conn.execute(f"DELETE FROM {table} WHERE ebook_id=?", (ebook_id,))
        conn.execute("DELETE FROM ebooks WHERE id=?", (ebook_id,))
        conn.commit()
    finally:
        conn.close()
    for path in paths:
        delete_file(path)
    flash("eBook을 삭제했습니다.", "success")
    return redirect(url_for("ebook.text_library"))
