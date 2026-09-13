"""새담 인트라넷의 영구 저장 위치를 한 곳에서 관리한다.

Render에서는 Persistent Disk의 표준 마운트인 ``/mnt/data``를 사용하고,
그 밖의 환경에서는 프로젝트의 ``data`` 폴더를 사용한다. Render에서는
영구 디스크가 빠진 채 임시 파일시스템으로 실행되는 것을 의도적으로 막는다.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
RENDER_DATA_ROOT = Path("/mnt/data")
WINDOWS_LEGACY_RENDER_ROOT = Path(APP_ROOT.anchor) / "mnt" / "data"


def _truthy_environment(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_render_runtime() -> bool:
    return _truthy_environment("RENDER") or bool(
        os.environ.get("RENDER_SERVICE_ID", "").strip()
    )


def _has_render_persistent_mount(path: Path) -> bool:
    """DATA_DIR가 실제 /mnt/data Persistent Disk 아래인지 확인한다."""
    try:
        path.resolve().relative_to(RENDER_DATA_ROOT)
    except ValueError:
        return False
    return RENDER_DATA_ROOT.is_dir() and os.path.ismount(str(RENDER_DATA_ROOT))


def _detect_data_root() -> Path:
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    elif os.name != "nt" and RENDER_DATA_ROOT.is_dir():
        root = RENDER_DATA_ROOT
    else:
        root = APP_ROOT / "data"

    if (
        os.name != "nt"
        and _is_render_runtime()
        and not _truthy_environment("ALLOW_EPHEMERAL_DATA_ON_RENDER")
        and not _has_render_persistent_mount(root)
    ):
        raise RuntimeError(
            "Render Persistent Disk가 연결되지 않았습니다. /mnt/data에 디스크를 "
            "마운트하고 DATA_DIR=/mnt/data로 설정해야 합니다. 임시 저장소로는 "
            "데이터 보호를 위해 실행하지 않습니다."
        )
    return root


DATA_ROOT = _detect_data_root()
MAIN_DB_FILE = DATA_ROOT / "saedam.db"
LEGACY_CONTRACT_DB_FILE = DATA_ROOT / "contracts.db"

UPLOADS_ROOT = DATA_ROOT / "uploads"
EXPENSE_UPLOADS = DATA_ROOT / "expense_uploads"
BOARD_UPLOADS = DATA_ROOT / "board_uploads"
CHAT_UPLOADS = DATA_ROOT / "chat_uploads"
MEMO_UPLOADS = DATA_ROOT / "memo_uploads"
MYDESK_UPLOADS = DATA_ROOT / "mydesk_uploads"
AI_MAIL_UPLOADS = DATA_ROOT / "ai_mail_uploads"
EBOOK_UPLOADS = DATA_ROOT / "ebook_uploads"
WEBTOON_UPLOADS = DATA_ROOT / "webtoon_uploads"
PHOTOBOOK_UPLOADS = DATA_ROOT / "photobook_uploads"
MEETING_UPLOADS = DATA_ROOT / "meeting_uploads"
MANUAL_UPLOADS = DATA_ROOT / "manuals"
PROFILE_ROOT = DATA_ROOT / "id"
SCHOOL_UPLOADS = DATA_ROOT / "school_uploads"
INTERVIEW_UPLOADS = DATA_ROOT / "interview_uploads"
DEPOSIT_UPLOADS = DATA_ROOT / "uploads_deposit"
GALLERY_ROOT = DATA_ROOT / "gallery"
GALLERY_UPLOADS = GALLERY_ROOT / "uploads"
GALLERY_THUMBS = GALLERY_ROOT / "thumbnails"
GALL2_ROOT = DATA_ROOT / "gall2"
VERIFIED_CONTRACT_ROOT = DATA_ROOT / "verified_contract"
VERIFIED_CONTRACTS_ROOT = VERIFIED_CONTRACT_ROOT / "completed"
VERIFIED_TERMS_ROOT = VERIFIED_CONTRACT_ROOT / "terms"
VERIFIED_STAMP_ROOT = VERIFIED_CONTRACT_ROOT / "stamps"
VERIFIED_LOGO_ROOT = VERIFIED_CONTRACT_ROOT / "logos"
VERIFIED_SIGNATURE_ROOT = VERIFIED_CONTRACT_ROOT / "signatures"
VERIFIED_PDF_FONT_ROOT = VERIFIED_CONTRACT_ROOT / "pdf_fonts"
TERMS_ROOT = DATA_ROOT / "terms"
COMPANY_STAMP_ROOT = DATA_ROOT / "company_stamps"
PDF_FONT_ROOT = DATA_ROOT / "pdf_fonts"
SECURITY_ROOT = DATA_ROOT / "security"
LEGACY_ARCHIVE_ROOT = DATA_ROOT / "legacy_archive"
LEGACY_BOOTSTRAP_MARKER = SECURITY_ROOT / ".legacy_files_bootstrapped"


PERSISTENT_DIRECTORIES = (
    DATA_ROOT,
    UPLOADS_ROOT,
    EXPENSE_UPLOADS,
    BOARD_UPLOADS,
    CHAT_UPLOADS,
    MEMO_UPLOADS,
    MYDESK_UPLOADS,
    AI_MAIL_UPLOADS,
    EBOOK_UPLOADS,
    WEBTOON_UPLOADS,
    PHOTOBOOK_UPLOADS,
    MEETING_UPLOADS,
    MANUAL_UPLOADS,
    PROFILE_ROOT,
    SCHOOL_UPLOADS,
    INTERVIEW_UPLOADS,
    DEPOSIT_UPLOADS,
    GALLERY_UPLOADS,
    GALLERY_THUMBS,
    GALL2_ROOT,
    VERIFIED_CONTRACT_ROOT,
    VERIFIED_CONTRACTS_ROOT,
    VERIFIED_TERMS_ROOT,
    VERIFIED_STAMP_ROOT,
    VERIFIED_LOGO_ROOT,
    VERIFIED_SIGNATURE_ROOT,
    VERIFIED_PDF_FONT_ROOT,
    TERMS_ROOT,
    COMPANY_STAMP_ROOT,
    PDF_FONT_ROOT,
    SECURITY_ROOT,
    LEGACY_ARCHIVE_ROOT,
)


def ensure_storage_directories() -> None:
    for directory in PERSISTENT_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def verify_storage_ready() -> dict[str, object]:
    """실행 저장소가 쓰기 가능하고 Render에서는 영구 디스크인지 확인한다.

    시작 로그에 경로를 남길 수 있도록 상태도 함께 반환한다. 확인 파일은 같은
    파일시스템에 생성·동기화한 직후 삭제하므로 사용자 데이터에는 영향을 주지 않는다.
    """
    ensure_storage_directories()
    if (
        os.name != "nt"
        and _is_render_runtime()
        and not _has_render_persistent_mount(DATA_ROOT)
    ):
        raise RuntimeError(
            "Render Persistent Disk가 연결되지 않았습니다. /mnt/data 마운트와 "
            "DATA_DIR=/mnt/data 설정을 확인해 주세요."
        )

    probe_path: Path | None = None
    try:
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".storage-write-check-",
            dir=str(DATA_ROOT),
        )
        probe_path = Path(probe_name)
        with os.fdopen(descriptor, "wb") as probe:
            probe.write(b"saedam-storage-ready\n")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as exc:
        raise RuntimeError(f"데이터 저장소에 쓸 수 없습니다: {DATA_ROOT}") from exc
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "data_root": str(DATA_ROOT),
        "database": str(MAIN_DB_FILE),
        "render": _is_render_runtime(),
        "persistent_disk": (
            _has_render_persistent_mount(DATA_ROOT)
            if os.name != "nt" and _is_render_runtime()
            else None
        ),
    }


def _copy_missing_tree(source: Path, target: Path) -> int:
    """기존 로컬 파일을 덮어쓰지 않고 통합 저장소로 복사한다."""
    if not source.is_dir() or source.resolve() == target.resolve():
        return 0
    copied = 0
    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        target_file = target / source_file.relative_to(source)
        if target_file.exists():
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        copied += 1
    return copied


def delete_storage_target(path: str | os.PathLike[str]) -> None:
    """파일/폴더를 삭제하고 운영체제에서 실제로 제거됐는지 확인한다."""
    target = Path(path)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    if os.path.lexists(target):
        raise OSError("삭제 요청 후에도 파일이 남아 있습니다.")


def bootstrap_legacy_files() -> int:
    """예전 프로젝트 폴더의 영구 파일을 최초 실행 때 자동 통합한다."""
    ensure_storage_directories()
    if DATA_ROOT.resolve() == APP_ROOT.resolve():
        return 0
    # 이 작업은 마이그레이션이다. 매번 누락 파일을 다시 복사하면 사용자가
    # 디스크 관리에서 삭제한 파일이 다음 요청/재시작 때 되살아난다.
    if LEGACY_BOOTSTRAP_MARKER.is_file():
        return 0

    mappings = []
    # 과거 Windows에서 '/mnt/data'를 드라이브 루트로 오인해 저장한 자료가
    # 있으면 프로젝트 data 폴더로 한 번만, 덮어쓰기 없이 복사한다.
    if os.name == "nt" and WINDOWS_LEGACY_RENDER_ROOT.is_dir():
        mappings.append((WINDOWS_LEGACY_RENDER_ROOT, DATA_ROOT))
    mappings.extend((
        (APP_ROOT / "chat_uploads", CHAT_UPLOADS),
        (APP_ROOT / "memo_uploads", MEMO_UPLOADS),
        (APP_ROOT / "ai_mail_uploads", AI_MAIL_UPLOADS),
        (APP_ROOT / "id", PROFILE_ROOT),
        (APP_ROOT / "school_uploads", SCHOOL_UPLOADS),
        (APP_ROOT / "static" / "school_uploads", SCHOOL_UPLOADS),
        (APP_ROOT / "uploads_deposit", DEPOSIT_UPLOADS),
        (APP_ROOT / "instance", SECURITY_ROOT),
    ))
    copied = sum(_copy_missing_tree(source, target) for source, target in mappings)
    LEGACY_BOOTSTRAP_MARKER.write_text(
        "legacy file migration completed\n",
        encoding="utf-8",
    )
    return copied


ensure_storage_directories()
