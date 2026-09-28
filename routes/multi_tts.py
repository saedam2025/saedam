"""[통합관리] > 멀티TTS: 글을 여러 캐릭터 목소리의 음성파일로 만든다.

- 텍스트를 붙여넣거나 문서(TXT·DOCX·HWP·HWPX·PDF)를 올리면 음성(WAV) 파일로 만든다.
- 남자·여자·아이·할아버지·조폭·뮤지컬배우·아나운서 등 캐릭터 목소리를 고른다.
  OpenAI gpt-4o-mini-tts의 기본 목소리에 '연기 지시(instructions)'를 붙여 캐릭터를 만든다.
- 대화 연출: A·B 캐릭터의 대사를 줄마다 따로 만든 뒤 쉼을 두고 한 파일로 이어 붙인다.
  줄(조각)마다 먼저 만들어 두므로 화면이 진행률을 보여 주고, 고친 줄만 다시 만든다.
- API 키는 [AI api설정]에 등록된 OpenAI 프리셋을 함께 쓴다.

음성 조각은 24kHz·16bit·모노 PCM으로 받아 앞뒤 무음을 다듬고 크기를 맞춘 뒤
암호화해 하루 동안 보관한다. 완성본은 조각을 순서대로 이어 WAV로 저장한다.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import threading
import time
import wave
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import numpy as np
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    session,
    url_for,
)

from .database import get_db
from .openai_settings import find_openai_api_key
from .secure_files import (
    delete_file,
    encrypt_bytes,
    encrypt_stream,
    encrypted_response,
    encrypted_storage_name,
    original_filename,
    read_decrypted,
)
from .security import is_admin_session
from .storage import MULTI_TTS_UPLOADS
from services import voice_effects

multi_tts_bp = Blueprint("multi_tts", __name__)

TTS_ROOT = Path(MULTI_TTS_UPLOADS)
SEGMENT_ROOT = TTS_ROOT / "segments"
CLIP_ROOT = TTS_ROOT / "clips"
PREVIEW_ROOT = TTS_ROOT / "previews"

TTS_MODEL = os.environ.get("MULTI_TTS_MODEL", "").strip() or "gpt-4o-mini-tts"
# OpenAI의 pcm 응답 형식: 24kHz, 16bit, 모노, little-endian
SAMPLE_RATE = 24000

MAX_SEGMENT_CHARS = 1000        # 한 번에 합성하는 조각 길이(모델 입력 한도보다 넉넉히 작게)
MAX_TEXT_CHARS = 10000          # 한 파일에 담는 전체 글자수(텍스트·대화 공통, 화면과 같은 값)
MAX_DIALOGUE_LINES = 200
MAX_PARTS = 400
MAX_CLIP_SECONDS = 30 * 60
MAX_GAP_MS = 3000
MAX_DIRECTION_CHARS = 200
MAX_CUSTOM_INSTRUCTIONS = 600
MAX_NAME_CHARS = 20
MAX_TITLE_CHARS = 120
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SCRIPT_JSON_BYTES = 400_000
SEGMENT_TTL = timedelta(hours=24)
SPEED_RANGE = (0.5, 2.0)
PAN_LIMIT = 0.8
LEAD_IN_MS = 120
TAIL_MS = 300
# 목소리마다 다른 크기를 맞출 목표 음량(약 -20 dBFS)과 증폭 한도
TARGET_RMS = 0.1 * 32768
GAIN_RANGE = (0.6, 2.5)
SILENCE_THRESHOLD = 0.012 * 32768
TRIM_MARGIN_MS = 60
FADE_MS = 8

TEXT_EXTENSIONS = {".txt", ".md", ".srt", ".csv"}
DOCUMENT_EXTENSIONS = {".docx", ".hwp", ".hwpx", ".pdf"}
UPLOAD_EXTENSIONS = TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
CSRF_SESSION_KEY = "multi_tts_csrf_token"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MODE_LABELS = {"single": "텍스트 낭독", "dialogue": "대화 연출"}

COMMON_INSTRUCTION = (
    "Read the text exactly as written, without adding, skipping or translating words. "
    "Pronounce Korean naturally, like a native speaker."
)

BASE_VOICES = (
    ("alloy", "Alloy · 중성적이고 담백한 목소리"),
    ("ash", "Ash · 또렷한 남성"),
    ("ballad", "Ballad · 부드럽고 감성적인 남성"),
    ("cedar", "Cedar · 자연스러운 남성"),
    ("coral", "Coral · 따뜻한 여성"),
    ("echo", "Echo · 경쾌한 남성"),
    ("fable", "Fable · 이야기꾼 같은 목소리"),
    ("marin", "Marin · 자연스러운 여성"),
    ("nova", "Nova · 밝고 젊은 여성"),
    ("onyx", "Onyx · 낮고 굵은 남성"),
    ("sage", "Sage · 차분한 여성"),
    ("shimmer", "Shimmer · 맑고 부드러운 여성"),
    ("verse", "Verse · 표현력이 풍부한 남성"),
)
BASE_VOICE_KEYS = {key for key, _label in BASE_VOICES}

VOICE_GROUPS = (
    ("basic", "기본"),
    ("age", "나이대"),
    ("character", "캐릭터"),
    ("pro", "전문 낭독"),
)
CUSTOM_PRESET_KEY = "custom"


def _preset(key, label, group, icon, voice, description, instructions, sample, effect=""):
    return {
        "key": key, "label": label, "group": group, "icon": icon, "voice": voice,
        "description": description, "instructions": instructions, "sample": sample,
        "effect": effect,
    }


# 캐릭터 목소리 = OpenAI 기본 목소리 + 연기 지시(+ 필요하면 음성 효과). 지시문은 영어가 가장 안정적으로 먹힌다.
VOICE_PRESETS = (
    _preset(
        "man", "남자", "basic", "fa-person", "cedar", "30대 남성의 자연스러운 말투",
        "Voice: a Korean man in his thirties with a warm, natural voice. "
        "Tone: friendly and sincere. Pacing: steady and conversational.",
        "안녕하세요. 오늘 하루도 편안하게 보내고 계신가요?",
    ),
    _preset(
        "woman", "여자", "basic", "fa-person-dress", "marin", "30대 여성의 부드러운 말투",
        "Voice: a Korean woman in her thirties with a clear, gentle voice. "
        "Tone: warm, kind and natural. Pacing: steady and conversational.",
        "안녕하세요. 이렇게 만나게 되어 정말 반갑습니다.",
    ),
    # OpenAI 목소리는 모두 어른이라 연기 지시만으로는 아이처럼 들리지 않는다. 음높이와 음색(포먼트)은
    # 아이 효과(services/voice_effects.py)가 올리므로, 지시문은 아이다운 말투만 맡기고 가성은 막는다.
    _preset(
        "child", "아이", "basic", "fa-child", "nova", "7살 어린이의 해맑은 목소리",
        "Voice: a young Korean child about seven years old. Delivery: innocent, curious and playful, "
        "with bouncy, sing-song rises and falls, simple phrasing and a slightly slower pace, like a kid "
        "excitedly telling mom about their day. Keep the voice natural and relaxed; "
        "do not use falsetto or a squeaky cartoon voice.",
        "엄마! 나 오늘 유치원에서 칭찬 스티커 받았어요!",
        effect="child",
    ),
    _preset(
        "teen", "청소년", "basic", "fa-user-graduate", "shimmer", "발랄한 10대 학생",
        "Voice: a lively Korean middle-school student. Tone: casual, upbeat and a little cheeky. "
        "Delivery: quick and energetic, like chatting with friends after class.",
        "야, 오늘 급식 진짜 맛있지 않았어? 완전 대박이었어!",
    ),
    _preset(
        "grandpa", "할아버지", "age", "fa-person-cane", "onyx", "인자한 80대 할아버지",
        "Voice: a kind Korean grandfather in his eighties. Timbre: low, slightly husky and a little "
        "trembling. Pacing: slow and unhurried, with gentle pauses. Tone: warm, wise and affectionate.",
        "허허, 우리 강아지 왔구나. 어디 보자, 그새 많이 컸네.",
    ),
    _preset(
        "grandma", "할머니", "age", "fa-hand-holding-heart", "sage", "다정한 80대 할머니",
        "Voice: a loving Korean grandmother in her eighties. Timbre: soft, slightly thin and aged. "
        "Pacing: slow, with tender pauses. Tone: caring and doting, like talking to a beloved grandchild.",
        "아이고, 우리 새끼 밥은 먹었어? 할머니가 맛있는 거 해 줄게.",
    ),
    _preset(
        "gangster", "조폭", "character", "fa-user-secret", "ash", "영화 속 험악한 조직 보스",
        "Voice: a menacing Korean gangster boss from a crime movie. Timbre: low, gravelly and rough. "
        "Delivery: slow, heavy and intimidating, with a threatening calm and blunt, clipped endings. "
        "Attitude: tough and street-smart; he never needs to raise his voice.",
        "어이, 거기. 내가 두 번 말하는 거 싫어하는 거 알지?",
    ),
    _preset(
        "musical", "뮤지컬배우", "character", "fa-masks-theater", "verse", "무대 위의 드라마틱한 배우",
        "Voice: a charismatic Korean musical theater actor on stage. Delivery: grand, theatrical and "
        "projecting to the back row, with sweeping melodic intonation that almost sings. "
        "Emotion: passionate and dramatic, with big crescendos and expressive pauses.",
        "오, 이 밤이 끝나기 전에! 나의 꿈을 노래하리라!",
    ),
    _preset(
        "sageuk", "사극 대감", "character", "fa-crown", "ballad", "조선시대 사극 말투",
        "Voice: a dignified nobleman in a Korean historical drama set in the Joseon dynasty. "
        "Tone: solemn, authoritative and old-fashioned. Pacing: measured, with weighty pauses "
        "and drawn-out sentence endings.",
        "게 아무도 없느냐. 어서 이 일을 전하께 고하도록 하여라.",
    ),
    _preset(
        "mc", "예능 MC", "character", "fa-microphone", "echo", "에너지 넘치는 예능 진행자",
        "Voice: an energetic Korean TV variety show host. Delivery: loud, fast, bright and punchy, "
        "full of excitement and playful exaggeration, hyping up the audience.",
        "자, 여러분! 오늘의 주인공을 소개합니다! 큰 박수 부탁드려요!",
    ),
    _preset(
        "robot", "로봇", "character", "fa-robot", "alloy", "감정 없는 인공지능 로봇",
        "Voice: a robotic artificial intelligence. Delivery: flat, monotone and mechanical, with evenly "
        "spaced syllables, precise articulation and no emotion at all.",
        "안녕하십니까. 저는 새담 인공지능 로봇입니다. 명령을 입력하십시오.",
    ),
    _preset(
        "announcer_f", "아나운서(여)", "pro", "fa-tv", "coral", "9시 뉴스 여성 앵커",
        "Voice: a professional female Korean news anchor. Diction: crisp, precise standard Seoul "
        "pronunciation. Tone: calm, confident, neutral and trustworthy. "
        "Pacing: measured, with clean pauses at commas.",
        "안녕하십니까, 새담 뉴스입니다. 오늘의 주요 소식을 전해 드리겠습니다.",
    ),
    _preset(
        "announcer_m", "아나운서(남)", "pro", "fa-tv", "cedar", "9시 뉴스 남성 앵커",
        "Voice: a professional male Korean news anchor. Diction: crisp, precise standard Seoul "
        "pronunciation. Tone: calm, authoritative, neutral and trustworthy. "
        "Pacing: measured, with clean pauses at commas.",
        "안녕하십니까. 이어서 오늘의 날씨를 전해 드리겠습니다.",
    ),
    _preset(
        "narrator", "다큐 내레이터", "pro", "fa-film", "onyx", "묵직한 다큐멘터리 성우",
        "Voice: a seasoned Korean documentary narrator. Timbre: deep, rich and resonant. "
        "Tone: calm, thoughtful and immersive. Pacing: slow and deliberate, letting important words land.",
        "수천 년의 시간이 흐르는 동안, 이 강은 단 한 번도 멈춘 적이 없었다.",
    ),
    _preset(
        "storyteller", "동화구연", "pro", "fa-book-open", "fable", "아이들에게 읽어 주는 동화 선생님",
        "Voice: a warm storyteller reading a fairy tale aloud to young children. Delivery: gentle, "
        "animated and expressive, with a sense of wonder, clear articulation and playful emphasis "
        "on sound words.",
        "옛날 옛날 깊은 산속에, 마음씨 착한 토끼 한 마리가 살고 있었어요.",
    ),
    _preset(
        "counselor", "상담원", "pro", "fa-headset", "sage", "친절한 고객센터 상담원",
        "Voice: a polite and friendly Korean customer service representative. Tone: bright, courteous "
        "and reassuring, using a respectful service tone. Pacing: clear and moderate.",
        "고객님, 기다려 주셔서 감사합니다. 무엇을 도와드릴까요?",
    ),
    _preset(
        "whisper", "ASMR 속삭임", "pro", "fa-feather", "shimmer", "귓가에 속삭이는 잔잔한 목소리",
        "Voice: a soft, intimate whisper, as if speaking right next to the listener's ear. "
        "Delivery: very quiet, breathy, slow and soothing, with relaxed pauses.",
        "괜찮아요. 천천히 숨을 들이쉬고, 편안하게 내쉬어 보세요.",
    ),
)
PRESETS_BY_KEY = {preset["key"]: preset for preset in VOICE_PRESETS}
CUSTOM_SAMPLE = "안녕하세요. 제 목소리는 이렇게 들립니다. 잘 부탁드립니다."


class TTSError(RuntimeError):
    """화면에 그대로 보여 줄 수 있는 음성 만들기 오류."""

    def __init__(self, message: str, status: int = 400, code: str = "", **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------


def init_multi_tts_schema():
    """멀티TTS 테이블과 저장 폴더를 준비한다(기존 데이터는 보존)."""
    for directory in (TTS_ROOT, SEGMENT_ROOT, CLIP_ROOT, PREVIEW_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS multi_tts_segments (
                id TEXT PRIMARY KEY,
                emp_no TEXT NOT NULL,
                file_path TEXT NOT NULL,
                voice_key TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_multi_tts_segments_created
                ON multi_tts_segments(created_at);
            CREATE TABLE IF NOT EXISTS multi_tts_clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'single',
                script_json TEXT NOT NULL DEFAULT '{}',
                summary TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                channels INTEGER NOT NULL DEFAULT 1,
                file_path TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_by_emp_no TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_multi_tts_clips_owner
                ON multi_tts_clips(created_by_emp_no, id);
            CREATE TABLE IF NOT EXISTS multi_tts_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_no TEXT NOT NULL,
                user_name TEXT NOT NULL DEFAULT '',
                voice_key TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_multi_tts_usage_created
                ON multi_tts_usage(created_at);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _emp_no() -> str:
    return str(session.get("emp_no") or "").strip()


def _current_name() -> str:
    return str(session.get("user_name") or session.get("emp_no") or "").strip()


def _clean_line(value, limit: int) -> str:
    """한 줄짜리 입력(이름·제목·연기 지시)을 공백 하나로 정리한다."""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def normalize_speech_text(value) -> str:
    """낭독할 글의 공백을 정리한다. 줄바꿈은 자연스러운 쉼이 되므로 남긴다."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n[\s]*", "\n", text)
    return text.strip()


def _csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return str(token)


def _json_error(message: str, status: int = 400, **extra):
    return jsonify({"status": "error", "message": message, **extra}), status


def _api(view):
    """로그인 확인 + CSRF 헤더 확인 + 오류를 JSON으로 바꿔 주는 공통 포장."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _emp_no():
            return _json_error("로그인이 필요합니다.", 401)
        if request.method == "POST":
            supplied = str(request.headers.get("X-CSRF-Token") or "")
            expected = str(session.get(CSRF_SESSION_KEY) or "")
            if not expected or not secrets.compare_digest(supplied, expected):
                return _json_error("요청 보안 토큰이 올바르지 않습니다. 화면을 새로고침해 주세요.", 403)
        try:
            return view(*args, **kwargs)
        except TTSError as exc:
            extra = {"code": exc.code, **exc.extra} if exc.code else dict(exc.extra)
            return _json_error(exc.message, exc.status, **extra)
        except ValueError as exc:
            return _json_error(str(exc), 400)

    return wrapped


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("요청 내용을 읽지 못했습니다.")
    return data


def _parse_speed(value) -> float:
    try:
        speed = float(value if value not in (None, "") else 1.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("말하기 속도 값이 올바르지 않습니다.") from exc
    if not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        raise ValueError(f"말하기 속도는 {SPEED_RANGE[0]}배에서 {SPEED_RANGE[1]}배 사이로 정해 주세요.")
    return round(speed, 2)


def _duration_label(duration_ms: int) -> str:
    seconds = max(0, int(round(int(duration_ms or 0) / 1000)))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}분 {seconds:02d}초" if minutes else f"{seconds}초"


def _size_label(size: int) -> str:
    size = int(size or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    return f"{max(1, round(size / 1024))}KB"


# ---------------------------------------------------------------------------
# 목소리
# ---------------------------------------------------------------------------


def resolve_voice(spec) -> dict:
    """화면에서 넘어온 목소리 선택을 (기본 목소리, 연기 지시, 이름표)로 바꾼다."""
    if not isinstance(spec, dict):
        raise ValueError("목소리를 선택해 주세요.")
    key = str(spec.get("preset") or "").strip()
    if key == CUSTOM_PRESET_KEY:
        voice = str(spec.get("voice") or "").strip()
        if voice not in BASE_VOICE_KEYS:
            raise ValueError("직접 연출에 쓸 기본 목소리를 골라 주세요.")
        instructions = _clean_line(spec.get("instructions"), MAX_CUSTOM_INSTRUCTIONS)
        if not instructions:
            raise ValueError("직접 연출할 목소리의 특징을 적어 주세요. (예: 60대 호탕한 시장 상인)")
        return {"key": CUSTOM_PRESET_KEY, "label": "직접 연출", "voice": voice,
                "instructions": instructions, "effect": ""}
    preset = PRESETS_BY_KEY.get(key)
    if not preset:
        raise ValueError("목소리 목록에서 골라 주세요.")
    return {"key": key, "label": preset["label"], "voice": preset["voice"],
            "instructions": preset["instructions"], "effect": preset["effect"]}


def preset_revision(preset: dict) -> str:
    """목소리 설정(모델·기본 목소리·지시문·효과)이 바뀌면 달라지는 값.

    화면은 이 값까지 같을 때만 만들어 둔 조각을 다시 쓰므로, 목소리를 고치면
    예전 목소리로 만든 조각이 섞여 들어가지 않는다.
    """
    effect = preset.get("effect") or ""
    version = voice_effects.EFFECT_VERSION if effect else ""
    source = f"{TTS_MODEL}\0{preset['voice']}\0{preset['instructions']}\0{effect}:{version}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def build_instructions(voice_info: dict, direction: str = "") -> str:
    parts = [voice_info["instructions"]]
    if direction:
        parts.append(f"Acting direction for this line (written in Korean): {direction}")
    parts.append(COMMON_INSTRUCTION)
    return "\n".join(parts)


def _pace_hint(speed: float) -> str:
    if speed >= 1.35:
        return "Pacing override: speak much faster than usual."
    if speed > 1.0:
        return "Pacing override: speak a little faster than usual."
    if speed <= 0.75:
        return "Pacing override: speak much slower than usual."
    return "Pacing override: speak a little slower than usual."


def _require_api_key() -> str:
    try:
        info = find_openai_api_key()
    except RuntimeError as exc:
        raise TTSError("저장된 OpenAI API 키를 읽지 못했습니다. [AI api설정]에서 키를 다시 등록해 주세요.",
                       500, "no_api_key") from exc
    if not info["api_key"]:
        raise TTSError(
            "음성 만들기에 쓸 OpenAI API 키가 없습니다. [통합관리 > 시스템관리 > AI api설정]에 "
            "OpenAI 프리셋 키를 등록해 주세요.",
            400, "no_api_key",
        )
    return info["api_key"]


def _friendly_error(exc: Exception) -> str:
    name = exc.__class__.__name__
    if name == "AuthenticationError":
        return "OpenAI API 키가 올바르지 않습니다. [AI api설정]의 OpenAI 키를 확인해 주세요."
    if name == "PermissionDeniedError":
        return "등록된 OpenAI 키로는 음성합성(TTS)을 사용할 수 없습니다. 키 권한을 확인해 주세요."
    if name == "RateLimitError":
        return "OpenAI 사용 한도에 걸렸거나 요청이 몰렸습니다. 잠시 후 다시 시도해 주세요."
    if name in {"APIConnectionError", "APITimeoutError"}:
        return "OpenAI 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
    if name == "NotFoundError":
        return f"음성 모델({TTS_MODEL})을 사용할 수 없습니다. 관리자에게 문의해 주세요."
    if name == "BadRequestError":
        return "OpenAI가 이 문장의 음성 만들기를 거절했습니다. 문장을 조금 바꿔 다시 시도해 주세요."
    return "음성을 만드는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."


def _openai_client(api_key: str):
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise TTSError("서버에 openai 라이브러리가 설치되어 있지 않습니다.", 500) from exc
    return OpenAI(api_key=api_key, timeout=120.0, max_retries=2)


def synthesize_pcm(api_key: str, voice: str, instructions: str, text: str, speed: float = 1.0) -> bytes:
    """OpenAI 음성합성으로 24kHz·16bit·모노 PCM을 받는다."""
    client = _openai_client(api_key)
    params = {
        "model": TTS_MODEL, "voice": voice, "input": text,
        "instructions": instructions, "response_format": "pcm",
    }
    if abs(speed - 1.0) >= 0.01:
        params["speed"] = speed
    try:
        try:
            response = client.audio.speech.create(**params)
        except Exception as exc:
            # 모델이 speed 인자를 받지 않으면 연기 지시로 빠르기를 대신 전한다.
            if "speed" in params and exc.__class__.__name__ == "BadRequestError" \
                    and "speed" in str(exc).lower():
                params.pop("speed")
                params["instructions"] = f"{instructions}\n{_pace_hint(speed)}"
                response = client.audio.speech.create(**params)
            else:
                raise
        return bytes(response.content)
    except TTSError:
        raise
    except Exception as exc:
        current_app.logger.warning("멀티TTS 음성합성 실패: %s: %s", exc.__class__.__name__, exc)
        raise TTSError(_friendly_error(exc), 502, "tts_failed") from exc


# ---------------------------------------------------------------------------
# 오디오 가공
# ---------------------------------------------------------------------------


def polish_pcm(raw: bytes, effect: str = "") -> np.ndarray:
    """앞뒤 무음을 다듬고, 목소리 효과를 입힌 뒤, 목소리마다 다른 크기를 비슷하게 맞춘다."""
    if len(raw) % 2:
        raw = raw[:-1]
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    loud = np.flatnonzero(np.abs(samples) > SILENCE_THRESHOLD)
    if loud.size == 0:
        return np.zeros(0, dtype="<i2")
    margin = int(SAMPLE_RATE * TRIM_MARGIN_MS / 1000)
    samples = samples[max(0, loud[0] - margin):min(samples.size, loud[-1] + margin + 1)]
    if effect:
        samples = voice_effects.apply_effect(effect, samples)

    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms > 0:
        samples *= min(max(TARGET_RMS / rms, GAIN_RANGE[0]), GAIN_RANGE[1])
    peak = float(np.max(np.abs(samples)))
    ceiling = 0.97 * 32767
    if peak > ceiling:
        samples *= ceiling / peak

    # 조각을 이을 때 '틱' 소리가 나지 않도록 양 끝을 아주 짧게 페이드한다.
    fade = min(int(SAMPLE_RATE * FADE_MS / 1000), samples.size // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        samples[:fade] *= ramp
        samples[-fade:] *= ramp[::-1]
    return np.clip(np.round(samples), -32768, 32767).astype("<i2")


def pcm_to_wav(pcm: bytes, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm)
    return buffer.getvalue()


def pan_gains(pan: float) -> tuple[float, float]:
    """-1(왼쪽)~1(오른쪽). 가운데는 원래 크기 그대로, 한쪽으로 갈수록 반대쪽만 줄인다."""
    pan = min(max(float(pan), -PAN_LIMIT), PAN_LIMIT)
    return min(1.0, 1.0 - pan), min(1.0, 1.0 + pan)


def compose_wav(parts: list[dict], stereo: bool, destination: Path) -> dict:
    """조각을 순서대로 잇고 사이사이 쉼을 넣어 WAV 한 파일로 암호화 저장한다.

    parts: [{'path': 조각 파일, 'gap_ms': 뒤에 둘 쉼, 'pan': 좌우 위치}]
    한 번에 조각 하나만 복호화하므로 긴 파일도 메모리를 많이 쓰지 않는다.
    """
    channels = 2 if stereo else 1
    frame_bytes = 2 * channels
    total_frames = 0
    with tempfile.TemporaryFile() as scratch:
        with wave.open(scratch, "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(2)
            writer.setframerate(SAMPLE_RATE)

            def silence(ms: int) -> int:
                frames = int(SAMPLE_RATE * ms / 1000)
                if frames:
                    writer.writeframes(b"\x00" * frames * frame_bytes)
                return frames

            total_frames += silence(LEAD_IN_MS)
            for index, part in enumerate(parts):
                samples = np.frombuffer(read_decrypted(part["path"]), dtype="<i2")
                if stereo:
                    left, right = pan_gains(part.get("pan") or 0.0)
                    frames = np.empty((samples.size, 2), dtype="<i2")
                    frames[:, 0] = np.round(samples * left).astype("<i2")
                    frames[:, 1] = np.round(samples * right).astype("<i2")
                    writer.writeframes(frames.tobytes())
                else:
                    writer.writeframes(samples.tobytes())
                total_frames += samples.size
                if index < len(parts) - 1:
                    total_frames += silence(int(part.get("gap_ms") or 0))
            total_frames += silence(TAIL_MS)
        scratch.seek(0, os.SEEK_END)
        size = scratch.tell()
        scratch.seek(0)
        encrypt_stream(scratch, destination)
    return {
        "frames": total_frames,
        "duration_ms": int(round(total_frames * 1000 / SAMPLE_RATE)),
        "size": size,
        "channels": channels,
    }


# ---------------------------------------------------------------------------
# 조각·사용량 저장
# ---------------------------------------------------------------------------

_cleanup_lock = threading.Lock()
_last_cleanup = 0.0


def cleanup_expired_segments(force: bool = False) -> int:
    """하루가 지난 음성 조각을 지운다(10분에 한 번만 실제로 확인한다)."""
    global _last_cleanup
    now = time.monotonic()
    with _cleanup_lock:
        if not force and now - _last_cleanup < 600:
            return 0
        _last_cleanup = now
    cutoff = (datetime.now() - SEGMENT_TTL).strftime(TIME_FORMAT)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, file_path FROM multi_tts_segments WHERE created_at < ?", (cutoff,)
        ).fetchall()
        removed = [row["id"] for row in rows if delete_file(row["file_path"])]
        conn.executemany("DELETE FROM multi_tts_segments WHERE id=?", [(sid,) for sid in removed])
        conn.commit()
        return len(removed)
    finally:
        conn.close()


def _log_usage(conn, voice_key: str, char_count: int, sample_count: int) -> None:
    conn.execute(
        """INSERT INTO multi_tts_usage (emp_no, user_name, voice_key, char_count, sample_count, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_emp_no(), _current_name(), voice_key, int(char_count), int(sample_count), _now()),
    )


def usage_summary(conn) -> dict:
    """이번 달 내가 만든 글자수·분량(관리자는 전체 합계도)."""
    month_start = datetime.now().strftime("%Y-%m-01 00:00:00")

    def totals(where: str, params: tuple) -> dict:
        row = conn.execute(
            f"""SELECT COUNT(*) AS requests, COALESCE(SUM(char_count), 0) AS chars,
                       COALESCE(SUM(sample_count), 0) AS samples
                FROM multi_tts_usage WHERE created_at >= ? {where}""",
            (month_start, *params),
        ).fetchone()
        return {
            "requests": int(row["requests"] or 0),
            "chars": int(row["chars"] or 0),
            "minutes": round(int(row["samples"] or 0) / SAMPLE_RATE / 60, 1),
        }

    summary = {"mine": totals("AND emp_no=?", (_emp_no(),))}
    if is_admin_session():
        summary["all"] = totals("", ())
    return summary


def _make_segment(voice_spec, text, direction="", speed=1.0) -> dict:
    """글 한 조각을 음성으로 만들어 임시 조각으로 저장하고 조각 정보를 돌려준다."""
    voice_info = resolve_voice(voice_spec)
    text = normalize_speech_text(text)
    if not text:
        raise ValueError("음성으로 만들 글을 입력해 주세요.")
    if len(text) > MAX_SEGMENT_CHARS:
        raise ValueError(f"한 조각은 {MAX_SEGMENT_CHARS}자까지 만들 수 있습니다. 문장을 나눠 주세요.")
    direction = _clean_line(direction, MAX_DIRECTION_CHARS)
    speed = _parse_speed(speed)

    api_key = _require_api_key()
    cleanup_expired_segments()
    raw = synthesize_pcm(api_key, voice_info["voice"], build_instructions(voice_info, direction), text, speed)
    pcm = polish_pcm(raw, voice_info["effect"])
    if pcm.size == 0:
        raise TTSError("만들어진 음성이 비어 있습니다. 문장을 조금 바꿔 다시 시도해 주세요.", 502, "tts_empty")

    segment_id = secrets.token_urlsafe(18)
    path = SEGMENT_ROOT / encrypted_storage_name("segment.pcm")
    encrypt_bytes(pcm.tobytes(), path)
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO multi_tts_segments
               (id, emp_no, file_path, voice_key, char_count, sample_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (segment_id, _emp_no(), str(path), voice_info["key"], len(text), int(pcm.size), _now()),
        )
        _log_usage(conn, voice_info["key"], len(text), int(pcm.size))
        conn.commit()
    except Exception:
        conn.rollback()
        delete_file(path)
        raise
    finally:
        conn.close()
    return {
        "segment_id": segment_id,
        "duration_ms": int(round(pcm.size * 1000 / SAMPLE_RATE)),
        "url": url_for("multi_tts.segment_audio", segment_id=segment_id),
    }


def _preview_fingerprint(preset: dict) -> str:
    return hashlib.sha256(f"{preset_revision(preset)}\0{preset['sample']}".encode("utf-8")).hexdigest()[:12]


def _preview_path(preset: dict) -> Path:
    return PREVIEW_ROOT / f"{preset['key']}-{_preview_fingerprint(preset)}.pcm.sdf"


# ---------------------------------------------------------------------------
# 저장한 음성
# ---------------------------------------------------------------------------


def _can_manage(row) -> bool:
    return str(row["created_by_emp_no"] or "") == _emp_no() or is_admin_session()


def _get_clip(conn, clip_id: int):
    row = conn.execute("SELECT * FROM multi_tts_clips WHERE id=?", (clip_id,)).fetchone()
    if not row or not _can_manage(row):
        abort(404)
    return row


def _clip_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "mode": row["mode"],
        "mode_label": MODE_LABELS.get(row["mode"], row["mode"]),
        "summary": row["summary"],
        "char_count": int(row["char_count"] or 0),
        "duration_ms": int(row["duration_ms"] or 0),
        "duration_label": _duration_label(row["duration_ms"]),
        "size_label": _size_label(row["file_size"]),
        "stereo": int(row["channels"] or 1) == 2,
        "created_by": row["created_by"],
        "created_at": str(row["created_at"] or "")[:16],
        "is_mine": str(row["created_by_emp_no"] or "") == _emp_no(),
        "audio_url": url_for("multi_tts.clip_audio", clip_id=row["id"]),
        "download_url": url_for("multi_tts.clip_audio", clip_id=row["id"], download=1),
    }


def list_clips(conn, scope: str = "mine") -> list[dict]:
    if scope == "all" and is_admin_session():
        rows = conn.execute("SELECT * FROM multi_tts_clips ORDER BY id DESC LIMIT 300").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM multi_tts_clips WHERE created_by_emp_no=? ORDER BY id DESC LIMIT 300",
            (_emp_no(),),
        ).fetchall()
    return [_clip_payload(row) for row in rows]


def summarize_script(mode: str, script: dict) -> str:
    """서재 카드에 보여 줄 목소리 요약(예: '철수(조폭) · 영희(아이)')."""
    def label(spec) -> str:
        try:
            return resolve_voice(spec)["label"]
        except ValueError:
            return "목소리"

    if mode == "dialogue":
        cast = script.get("cast") if isinstance(script.get("cast"), dict) else {}
        names = []
        for slot in ("A", "B"):
            member = cast.get(slot) if isinstance(cast.get(slot), dict) else {}
            name = _clean_line(member.get("name"), MAX_NAME_CHARS) or slot
            names.append(f"{name}({label(member.get('voice'))})")
        return " · ".join(names)
    return label(script.get("voice"))


def _parse_parts(raw_parts) -> list[dict]:
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("합칠 음성 조각이 없습니다. 먼저 음성을 만들어 주세요.")
    if len(raw_parts) > MAX_PARTS:
        raise ValueError(f"한 파일에는 최대 {MAX_PARTS}개 조각까지 담을 수 있습니다.")
    parts = []
    for item in raw_parts:
        if not isinstance(item, dict):
            raise ValueError("음성 조각 정보가 올바르지 않습니다.")
        segment_id = str(item.get("segment_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", segment_id):
            raise ValueError("음성 조각 정보가 올바르지 않습니다.")
        try:
            gap_ms = int(item.get("gap_ms") or 0)
            pan = float(item.get("pan") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError("쉼·좌우 위치 값이 올바르지 않습니다.") from exc
        parts.append({
            "segment_id": segment_id,
            "gap_ms": min(max(gap_ms, 0), MAX_GAP_MS),
            "pan": min(max(pan, -PAN_LIMIT), PAN_LIMIT),
        })
    return parts


# ---------------------------------------------------------------------------
# 문서에서 글 꺼내기
# ---------------------------------------------------------------------------


def decode_text_file(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def strip_subtitles(text: str) -> str:
    """SRT 자막의 번호·시간 줄을 빼고 대사만 남긴다."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\d+", stripped) or "-->" in stripped:
            continue
        kept.append(line)
    return "\n".join(kept)


def extract_upload_text(filename: str, data: bytes) -> str:
    extension = Path(filename).suffix.lower()
    if extension in TEXT_EXTENSIONS:
        text = decode_text_file(data)
        if extension == ".srt":
            text = strip_subtitles(text)
    elif extension in DOCUMENT_EXTENSIONS:
        from services.interview_resume import extract_text
        try:
            text = extract_text(data, extension)
        except Exception as exc:
            raise ValueError(f"‘{filename}’에서 글자를 읽지 못했습니다. 손상되었거나 암호가 걸린 문서인지 확인해 주세요.") from exc
    else:
        raise ValueError("TXT·MD·SRT·CSV·DOCX·HWP·HWPX·PDF 파일만 불러올 수 있습니다.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------


@multi_tts_bp.route("/")
def index():
    if not _emp_no():
        abort(401)
    conn = get_db()
    try:
        clips = list_clips(conn)
        usage = usage_summary(conn)
    finally:
        conn.close()
    try:
        key_info = find_openai_api_key()
    except RuntimeError:
        key_info = {"api_key": "", "preset_label": ""}
    boot = {
        "csrf": _csrf_token(),
        "groups": [{"key": key, "label": label} for key, label in VOICE_GROUPS],
        "presets": [
            {**{k: preset[k] for k in ("key", "label", "group", "icon", "voice", "description")},
             "rev": preset_revision(preset)}
            for preset in VOICE_PRESETS
        ],
        "base_voices": [{"key": key, "label": label} for key, label in BASE_VOICES],
        "custom_sample": CUSTOM_SAMPLE,
        "clips": clips,
        "usage": usage,
        "can_view_all": is_admin_session(),
        "api_ready": bool(key_info["api_key"]),
        "api_label": key_info.get("preset_label") or "",
        "limits": {
            "text_chars": MAX_TEXT_CHARS,
            "segment_chars": MAX_SEGMENT_CHARS,
            "dialogue_lines": MAX_DIALOGUE_LINES,
            "direction_chars": MAX_DIRECTION_CHARS,
            "custom_chars": MAX_CUSTOM_INSTRUCTIONS,
            "name_chars": MAX_NAME_CHARS,
            "title_chars": MAX_TITLE_CHARS,
            "upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            "speed_min": SPEED_RANGE[0],
            "speed_max": SPEED_RANGE[1],
            "gap_max": MAX_GAP_MS,
        },
        "urls": {
            "extract": url_for("multi_tts.extract_text_api"),
            "preview": url_for("multi_tts.preview_api"),
            "segments": url_for("multi_tts.create_segment"),
            "clips": url_for("multi_tts.clips_api"),
            "ai_settings": url_for("admin.ai_settings_page") if "admin" in current_app.blueprints else "",
        },
    }
    return render_template("multi_tts/index.html", boot=boot, model_name=TTS_MODEL)


@multi_tts_bp.route("/api/extract", methods=["POST"])
@_api
def extract_text_api():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        raise ValueError("불러올 파일을 선택해 주세요.")
    name = original_filename(upload.filename, "document")
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"파일은 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB까지 불러올 수 있습니다.")
    text = extract_upload_text(name, data)
    if not text:
        raise ValueError(f"‘{name}’에서 읽을 수 있는 글자를 찾지 못했습니다. (스캔 이미지 PDF는 글자를 읽을 수 없습니다.)")
    return jsonify({
        "status": "ok", "filename": name, "chars": len(text),
        "truncated": len(text) > MAX_TEXT_CHARS, "text": text[:MAX_TEXT_CHARS],
    })


@multi_tts_bp.route("/api/preview", methods=["POST"])
@_api
def preview_api():
    """캐릭터 목소리 미리듣기. 목소리마다 한 번만 만들고 모두가 같이 쓴다."""
    preset = PRESETS_BY_KEY.get(str(_json_body().get("preset") or "").strip())
    if not preset:
        raise ValueError("미리들을 목소리를 골라 주세요.")
    path = _preview_path(preset)
    if not path.is_file():
        api_key = _require_api_key()
        raw = synthesize_pcm(api_key, preset["voice"], build_instructions(preset), preset["sample"])
        pcm = polish_pcm(raw, preset["effect"])
        if pcm.size == 0:
            raise TTSError("미리듣기 음성이 비어 있습니다. 잠시 후 다시 시도해 주세요.", 502)
        encrypt_bytes(pcm.tobytes(), path)
        # 목소리 설정이 바뀌기 전에 만든 미리듣기 파일은 더 쓰지 않으므로 지운다.
        for stale in PREVIEW_ROOT.glob(f"{preset['key']}-*.pcm.sdf"):
            if stale != path:
                delete_file(stale)
        conn = get_db()
        try:
            _log_usage(conn, preset["key"], len(preset["sample"]), int(pcm.size))
            conn.commit()
        finally:
            conn.close()
    return jsonify({
        "status": "ok",
        "url": url_for("multi_tts.preview_audio", preset_key=preset["key"], v=_preview_fingerprint(preset)),
        "sample": preset["sample"],
    })


@multi_tts_bp.route("/preview/<preset_key>.wav")
def preview_audio(preset_key):
    if not _emp_no():
        abort(401)
    preset = PRESETS_BY_KEY.get(preset_key)
    if not preset:
        abort(404)
    path = _preview_path(preset)
    if not path.is_file():
        abort(404)
    response = Response(pcm_to_wav(read_decrypted(path)), mimetype="audio/wav")
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


@multi_tts_bp.route("/api/segments", methods=["POST"])
@_api
def create_segment():
    """글 한 조각(대화 한 줄 또는 긴 글의 한 부분)을 음성으로 만든다."""
    data = _json_body()
    segment = _make_segment(data.get("voice"), data.get("text"), data.get("direction"), data.get("speed"))
    return jsonify({"status": "ok", **segment})


@multi_tts_bp.route("/segments/<segment_id>.wav")
def segment_audio(segment_id):
    if not _emp_no():
        abort(401)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT file_path FROM multi_tts_segments WHERE id=? AND emp_no=?",
            (segment_id, _emp_no()),
        ).fetchone()
    finally:
        conn.close()
    if not row or not Path(row["file_path"]).is_file():
        abort(404)
    response = Response(pcm_to_wav(read_decrypted(row["file_path"])), mimetype="audio/wav")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@multi_tts_bp.route("/api/clips", methods=["GET", "POST"])
@_api
def clips_api():
    if request.method == "GET":
        scope = "all" if request.args.get("scope") == "all" else "mine"
        conn = get_db()
        try:
            return jsonify({"status": "ok", "clips": list_clips(conn, scope)})
        finally:
            conn.close()
    return _compose_clip(_json_body())


def _compose_clip(data: dict):
    """만들어 둔 조각들을 이어 붙여 음성파일 한 개로 저장한다."""
    mode = "dialogue" if data.get("mode") == "dialogue" else "single"
    stereo = bool(data.get("stereo")) and mode == "dialogue"
    parts = _parse_parts(data.get("parts"))
    script = data.get("script") if isinstance(data.get("script"), dict) else {}
    script_json = json.dumps(script, ensure_ascii=False)
    if len(script_json.encode("utf-8")) > MAX_SCRIPT_JSON_BYTES:
        raise ValueError("대본이 너무 깁니다. 나눠서 만들어 주세요.")
    title = _clean_line(data.get("title"), MAX_TITLE_CHARS) or (
        "대화 연출" if mode == "dialogue" else "텍스트 낭독")

    conn = get_db()
    try:
        segment_ids = sorted({part["segment_id"] for part in parts})
        placeholders = ",".join("?" for _ in segment_ids)
        rows = conn.execute(
            f"SELECT * FROM multi_tts_segments WHERE emp_no=? AND id IN ({placeholders})",
            (_emp_no(), *segment_ids),
        ).fetchall()
        found = {row["id"]: row for row in rows if Path(row["file_path"]).is_file()}
        missing = [sid for sid in segment_ids if sid not in found]
        if missing:
            # 화면은 이 목록의 조각만 다시 만든 뒤 합치기를 한 번 더 요청한다.
            raise TTSError("시간이 지나 지워진 음성 조각이 있어 다시 만들어야 합니다.",
                           410, "segment_expired", missing=missing)

        total_frames = sum(int(found[p["segment_id"]]["sample_count"]) for p in parts)
        total_frames += sum(int(SAMPLE_RATE * p["gap_ms"] / 1000) for p in parts[:-1])
        if total_frames > MAX_CLIP_SECONDS * SAMPLE_RATE:
            raise ValueError(f"한 파일은 {MAX_CLIP_SECONDS // 60}분까지 만들 수 있습니다. 나눠서 만들어 주세요.")

        for part in parts:
            part["path"] = found[part["segment_id"]]["file_path"]
        destination = CLIP_ROOT / encrypted_storage_name("clip.wav")
        result = compose_wav(parts, stereo, destination)
        char_count = sum(int(found[p["segment_id"]]["char_count"]) for p in parts)
        try:
            cursor = conn.execute(
                """INSERT INTO multi_tts_clips
                   (title, mode, script_json, summary, char_count, duration_ms, channels,
                    file_path, file_size, created_by, created_by_emp_no, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (title, mode, script_json, summarize_script(mode, script), char_count,
                 result["duration_ms"], result["channels"], str(destination), result["size"],
                 _current_name(), _emp_no(), _now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            delete_file(destination)
            raise
        row = conn.execute("SELECT * FROM multi_tts_clips WHERE id=?", (cursor.lastrowid,)).fetchone()
        return jsonify({"status": "ok", "clip": _clip_payload(row)})
    finally:
        conn.close()


@multi_tts_bp.route("/api/clips/<int:clip_id>")
@_api
def clip_detail(clip_id):
    """저장한 음성의 대본(다시 불러와 고쳐 만들기용)."""
    conn = get_db()
    try:
        row = _get_clip(conn, clip_id)
    finally:
        conn.close()
    try:
        script = json.loads(row["script_json"] or "{}")
    except (TypeError, ValueError):
        script = {}
    return jsonify({"status": "ok", "clip": _clip_payload(row), "script": script})


@multi_tts_bp.route("/clips/<int:clip_id>/audio")
def clip_audio(clip_id):
    if not _emp_no():
        abort(401)
    conn = get_db()
    try:
        row = _get_clip(conn, clip_id)
    finally:
        conn.close()
    try:
        return encrypted_response(
            row["file_path"], f"{row['title']}.wav",
            as_attachment=request.args.get("download") == "1", mimetype="audio/wav",
        )
    except FileNotFoundError:
        abort(404)


@multi_tts_bp.route("/api/clips/<int:clip_id>/delete", methods=["POST"])
@_api
def delete_clip(clip_id):
    conn = get_db()
    try:
        row = _get_clip(conn, clip_id)
        if not delete_file(row["file_path"]):
            raise TTSError("음성파일을 지우지 못했습니다. 잠시 후 다시 시도해 주세요.", 500)
        conn.execute("DELETE FROM multi_tts_clips WHERE id=?", (clip_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok", "deleted": clip_id})
