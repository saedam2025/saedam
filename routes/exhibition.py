"""[통합관리] > [3D전시장] 기능.

- 전시관은 서로 다른 여섯 가지 모양(전시관1 라운지형 / 전시관2 갤러리형 /
  전시관3 교실형 / 전시관4 공원형 / 전시관5 극장 로비형 / 전시관6 홈씨어터룸)
  중 하나를 골라 만든다. 모양마다 벽·기둥 배치와 전시공간(작품 자리) 수가 다르다.
- [전시관 셋팅]에서 이름·벽색·바닥색·조명·배경음악·이동속도 등을 정한다.
- [전시파일 셋팅]에서 전시공간마다 사진 여러 장이나 동영상을 올리고,
  작품 아래 팻말에 들어갈 작품제목·작가명·설명을 적는다.
- 관람 화면은 three.js로 그린 3D 공간이며 PC 방향키, 모바일 손가락,
  VR 기기 조작스틱으로 걸어 다니면서 볼 수 있다.

전시관의 벽·전시공간 좌표는 아래 ``HALL_LAYOUTS``에 있고, 그대로 JSON으로
관람 화면에 전달되어 화면이 만들어진다. 올라온 파일은 다른 메뉴와 똑같이
암호화해서 저장하고, 동영상은 되감기가 되도록 Range 요청까지 지원한다.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

from flask import (
    Blueprint,
    Response,
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
from .secure_files import (
    encrypted_response,
    encrypted_storage_name,
    encrypt_upload,
    iter_decrypted,
    original_filename,
    plaintext_size,
)
from .security import is_admin_session, menu_permission_required
from .storage import EXHIBITION_UPLOADS

exhibition_bp = Blueprint("exhibition", __name__)

EXHIBITION_ROOT = Path(EXHIBITION_UPLOADS)
MEDIA_ROOT = EXHIBITION_ROOT / "media"
BGM_ROOT = EXHIBITION_ROOT / "bgm"

MENU_NAME = "3D전시장"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
# 브라우저가 별도 프로그램 없이 바로 재생할 수 있는 형식만 받는다.
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogv", ".ogg", ".m4v", ".mov"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".aac"}

VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".ogg": "video/ogg",
}
AUDIO_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".wav": "audio/wav",
}

MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_VIDEO_BYTES = 300 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
# 한 전시공간에 담을 수 있는 최대 장수(사진 슬라이드 쇼 기준)
MAX_MEDIA_PER_SLOT = 30
MAX_HALLS = 60
# Range 응답 한 조각의 최대 크기
RANGE_CHUNK_LIMIT = 4 * 1024 * 1024

UPLOAD_FORMAT_MESSAGE = (
    "사진(JPG·PNG·WEBP·GIF) 또는 동영상(MP4·WEBM·OGV·MOV) 파일만 올릴 수 있습니다."
)
AUDIO_FORMAT_MESSAGE = "배경음악은 MP3·M4A·OGG·WAV 파일만 올릴 수 있습니다."


# ---------------------------------------------------------------------------
# 전시관 모양(벽·기둥·전시공간 좌표). 단위는 미터.
# ---------------------------------------------------------------------------

FACE_FRONT = 0.0                 # +Z 방향을 바라보는 면
FACE_BACK = math.pi              # -Z 방향
FACE_RIGHT = math.pi / 2         # +X 방향
FACE_LEFT = -math.pi / 2         # -X 방향


def _slot(code, name, x, y, z, rotation_y, width=3.2, height=2.1, frameless=False,
          tilt=0.0):
    """전시공간 한 자리. (x, y, z)는 작품 한가운데, rotation_y는 바라보는 방향.

    ``frameless``는 칠판처럼 액자 없이 전시물만 붙이는 자리에 쓴다.
    ``tilt``는 앞뒤로 눕히는 각도(라디안). -pi/2 면 바닥에 눕혀 하늘을 보게 된다.
    """
    return {
        "code": code,
        "name": name,
        "x": round(x, 3),
        "y": round(y, 3),
        "z": round(z, 3),
        "rotationY": round(rotation_y, 5),
        "tilt": round(tilt, 5),
        "width": width,
        "height": height,
        "frameless": frameless,
    }


def _wall(x, z, rotation_y, width, height):
    return {
        "x": round(x, 3),
        "z": round(z, 3),
        "rotationY": round(rotation_y, 5),
        "width": width,
        "height": height,
    }


def _partition(x, z, axis, length, height, depth=0.45):
    """가운데 세워 둔 가림벽(양면에 작품을 건다)."""
    return {
        "x": round(x, 3),
        "z": round(z, 3),
        "axis": axis,          # 'x' = 좌우로 길게, 'z' = 앞뒤로 길게
        "length": length,
        "height": height,
        "depth": depth,
    }


def _hall1_layout():
    """전시관1 · 라운지형: 밝은 나무 바닥과 천창이 있는 넓은 로비."""
    width, depth, height = 24.0, 18.0, 7.0
    half_w, half_d = width / 2, depth / 2
    surface = 0.07            # 벽에서 살짝 띄워 거는 간격
    # 2층 난간 바닥(높이 3.6m, 두께 0.22m → 아랫면 3.49m)에 액자 윗부분과
    # 그림 조명이 닿지 않도록 전체 액자를 낮춰 건다.
    eye = 1.95                # 작품 한가운데 높이
    partition_z = 2.6
    partition_half = 0.45 / 2

    slots = []
    for index, x in enumerate((-8.4, -2.8, 2.8, 8.4)):
        slots.append(_slot(
            f"A{index + 1}", f"정면 {index + 1}번 자리",
            x, eye, -half_d + surface, FACE_FRONT,
        ))
    for index, z in enumerate((-4.2, 1.4)):
        slots.append(_slot(
            f"B{index + 1}", f"왼쪽 벽 {index + 1}번 자리",
            -half_w + surface, eye, z, FACE_RIGHT,
        ))
    for index, z in enumerate((-4.2, 1.4)):
        slots.append(_slot(
            f"C{index + 1}", f"오른쪽 벽 {index + 1}번 자리",
            half_w - surface, eye, z, FACE_LEFT,
        ))
    for index, x in enumerate((-2.6, 2.6)):
        slots.append(_slot(
            f"D{index + 1}", f"가운데 가림벽 앞 {index + 1}번 자리",
            x, 1.9, partition_z + partition_half + surface, FACE_FRONT,
            width=2.8, height=1.9,
        ))
    for index, x in enumerate((-2.6, 2.6)):
        slots.append(_slot(
            f"D{index + 3}", f"가운데 가림벽 뒤 {index + 1}번 자리",
            x, 1.9, partition_z - partition_half - surface, FACE_BACK,
            width=2.8, height=1.9,
        ))

    return {
        "key": "hall1",
        "name": "전시관1 · 라운지형",
        "summary": "밝은 나무 바닥과 천창이 있는 2층 로비형 전시관입니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 6.5, "z": half_d - 3.0, "heading": 0.35},
        "style": "lounge",
        "walls": [
            _wall(0, -half_d, FACE_FRONT, width, height),
            _wall(0, half_d, FACE_BACK, width, height),
            _wall(-half_w, 0, FACE_RIGHT, depth, height),
            _wall(half_w, 0, FACE_LEFT, depth, height),
        ],
        "partitions": [_partition(0, partition_z, "x", 9.0, 4.2)],
        "skylight": {"width": 9.0, "depth": 11.0},
        "balcony": {"height": 3.6, "depth": 2.4},
        "slots": slots,
    }


def _hall2_layout():
    """전시관2 · 갤러리형: 콘크리트 바닥과 격자 천장의 화이트 큐브."""
    width, depth, height = 30.0, 22.0, 6.0
    half_w, half_d = width / 2, depth / 2
    surface = 0.07
    eye = 2.3
    partition_half = 0.45 / 2

    slots = []
    for index, x in enumerate((-11.0, -4.0, 4.0, 11.0)):
        slots.append(_slot(
            f"A{index + 1}", f"정면 {index + 1}번 자리",
            x, eye, -half_d + surface, FACE_FRONT,
        ))
    for index, z in enumerate((-6.5, -1.0, 4.5)):
        slots.append(_slot(
            f"B{index + 1}", f"왼쪽 벽 {index + 1}번 자리",
            -half_w + surface, eye, z, FACE_RIGHT,
        ))
    for index, z in enumerate((-6.5, -1.0, 4.5)):
        slots.append(_slot(
            f"C{index + 1}", f"오른쪽 벽 {index + 1}번 자리",
            half_w - surface, eye, z, FACE_LEFT,
        ))
    for index, x in enumerate((-10.5, 10.5)):
        slots.append(_slot(
            f"E{index + 1}", f"입구 벽 {index + 1}번 자리",
            x, eye, half_d - surface, FACE_BACK,
        ))
    # 앞뒤로 긴 가림벽 두 개(양면 사용)
    for index, (px, code) in enumerate(((-5.5, "F"), (5.5, "G"))):
        for offset, (z, suffix) in enumerate(((-3.6, 1), (2.4, 2))):
            slots.append(_slot(
                f"{code}{suffix}", f"가운데 가림벽 {index + 1} 오른쪽 {suffix}번 자리",
                px + partition_half + surface, 2.1, z, FACE_RIGHT,
                width=2.9, height=1.95,
            ))
            slots.append(_slot(
                f"{code}{suffix + 2}", f"가운데 가림벽 {index + 1} 왼쪽 {suffix}번 자리",
                px - partition_half - surface, 2.1, z, FACE_LEFT,
                width=2.9, height=1.95,
            ))

    return {
        "key": "hall2",
        "name": "전시관2 · 갤러리형",
        "summary": "콘크리트 바닥과 격자 천장을 가진 넓은 화이트 큐브 전시관입니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": half_d - 3.5, "heading": 0.0},
        "style": "gallery",
        "walls": [
            _wall(0, -half_d, FACE_FRONT, width, height),
            _wall(0, half_d, FACE_BACK, width, height),
            _wall(-half_w, 0, FACE_RIGHT, depth, height),
            _wall(half_w, 0, FACE_LEFT, depth, height),
        ],
        "partitions": [
            _partition(-5.5, -0.6, "z", 11.0, 4.4),
            _partition(5.5, -0.6, "z", 11.0, 4.4),
        ],
        "skylight": {"width": 4.0, "depth": 14.0},
        "balcony": None,
        "slots": slots,
    }


def _hall3_layout():
    """전시관3 · 교실형: 창가로 햇살이 드는 교실. 칠판과 게시판에 전시한다."""
    width, depth, height = 12.0, 9.6, 3.3
    half_w, half_d = width / 2, depth / 2
    surface = 0.07
    board_face = -half_d + 0.18      # 칠판 앞면(벽보다 살짝 앞으로 나와 있다)
    eye = 1.72                       # 천장이 낮은 교실이라 눈높이에 맞춰 건다

    slots = []
    # 칠판에는 액자 없이 전시물 하나만 크게 띄운다.
    slots.append(_slot(
        "A1", "칠판 전시 자리",
        0.0, 1.72, board_face, FACE_FRONT, width=6.0, height=1.8, frameless=True,
    ))
    # 게시판은 액자 없이 사진만 붙이는 쪽이 교실답다.
    # 출입문(뒤쪽 z=3.5)과 패널 사이에도 벽 여백을 남긴다.
    for index, z in enumerate((-2.9, -0.6, 1.7)):
        slots.append(_slot(
            f"B{index + 1}", f"오른쪽 게시판 {index + 1}번 자리",
            half_w - surface, eye, z, FACE_LEFT, width=1.7, height=1.15, frameless=True,
        ))
    # 뒤쪽 게시판(양옆은 사물함이라 가운데만 쓴다)
    for index, x in enumerate((-1.8, 1.8)):
        slots.append(_slot(
            f"C{index + 1}", f"뒤쪽 게시판 {index + 1}번 자리",
            x, 1.85, half_d - surface, FACE_BACK, width=1.8, height=1.2, frameless=True,
        ))
    # 창문 사이 벽(창가 자리)
    for index, z in enumerate((-3.7, 3.7)):
        slots.append(_slot(
            f"D{index + 1}", f"창가 벽 {index + 1}번 자리",
            -half_w + surface, eye, z, FACE_RIGHT, width=1.35, height=0.95, frameless=True,
        ))

    return {
        "key": "hall3",
        "name": "전시관3 · 교실형",
        "summary": "창밖 하늘과 햇살이 드는 교실입니다. 칠판과 벽에는 사진을 테두리 없는 얇은 패널로 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": half_d - 1.1, "heading": 0.0},
        "style": "classroom",
        "walls": [],          # 창문이 뚫린 벽이라 교실 모양은 3D 쪽에서 직접 만든다
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall4_layout():
    """전시관4 · 공원형: 하늘이 트인 야외 공원. 캔버스 게시대에 전시한다."""
    width, depth, height = 34.0, 26.0, 9.0
    half_w = width / 2
    eye = 1.8

    slots = []
    # 안쪽(북쪽) 잔디밭 — 들어오는 사람을 마주 본다
    for index, x in enumerate((-10.5, -3.5, 3.5, 10.5)):
        slots.append(_slot(
            f"A{index + 1}", f"안쪽 잔디밭 {index + 1}번 자리",
            x, eye, -9.5, FACE_FRONT, width=2.6, height=1.75,
        ))
    # 서쪽 산책로
    for index, z in enumerate((-2.5, 3.0, 8.5)):
        slots.append(_slot(
            f"B{index + 1}", f"서쪽 산책로 {index + 1}번 자리",
            -13.5, eye, z, FACE_RIGHT, width=2.6, height=1.75,
        ))
    # 동쪽 산책로
    for index, z in enumerate((-2.5, 3.0, 8.5)):
        slots.append(_slot(
            f"C{index + 1}", f"동쪽 산책로 {index + 1}번 자리",
            13.5, eye, z, FACE_LEFT, width=2.6, height=1.75,
        ))
    # 입구 양옆 — 공원 안쪽을 향하게 세운다(들어와서 돌아보는 자리)
    for index, x in enumerate((-7.0, 7.0)):
        slots.append(_slot(
            f"D{index + 1}", f"입구 {index + 1}번 자리",
            x, eye, 9.5, FACE_BACK, width=2.4, height=1.6,
        ))

    return {
        "key": "hall4",
        "name": "전시관4 · 공원형",
        "summary": "하늘과 햇살이 있는 야외 공원입니다. 나무와 분수 사이 캔버스에 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": depth / 2 - 1.6, "heading": 0.0},
        "style": "park",
        "walls": [],          # 야외라 벽이 없다(가장자리는 생울타리로 막는다)
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall5_layout():
    """전시관5 · 극장 로비형: 매표소·매점·대기석이 있는 넓은 극장 로비."""
    width, depth, height = 28.0, 20.0, 6.0
    half_w, half_d = width / 2, depth / 2
    surface = 0.07
    poster_y = 2.25
    pw, ph = 1.6, 2.35          # 영화 포스터라 세로로 길다

    slots = []
    # 상영관 입구 위쪽의 예고편 스크린(액자 없이 화면만)
    slots.append(_slot(
        "A1", "예고편 스크린",
        0.0, 3.8, -half_d + 0.3, FACE_FRONT, width=7.2, height=3.4, frameless=True,
    ))
    # 포스터는 액자 없이 조명 박스 안에 그대로 들어간다.
    for index, z in enumerate((-6.0, -2.0, 2.0, 6.0)):
        slots.append(_slot(
            f"B{index + 1}", f"왼쪽 포스터월 {index + 1}번 자리",
            -half_w + surface, poster_y, z, FACE_RIGHT, width=pw, height=ph, frameless=True,
        ))
    for index, x in enumerate((-10.5, 10.5)):
        slots.append(_slot(
            f"C{index + 1}", f"상영관 입구 옆 {index + 1}번 자리",
            x, poster_y, -half_d + surface, FACE_FRONT, width=pw, height=ph, frameless=True,
        ))
    for index, z in enumerate((-6.0, -2.4)):
        slots.append(_slot(
            f"D{index + 1}", f"오른쪽 포스터월 {index + 1}번 자리",
            half_w - surface, poster_y, z, FACE_LEFT, width=pw, height=ph, frameless=True,
        ))
    for index, x in enumerate((-9.5, 9.5)):
        slots.append(_slot(
            f"E{index + 1}", f"정문 옆 {index + 1}번 자리",
            x, poster_y, half_d - surface, FACE_BACK, width=pw, height=ph, frameless=True,
        ))

    return {
        "key": "hall5",
        "name": "전시관5 · 극장 로비형",
        "summary": "매점과 대기석이 있는 넓은 극장 로비입니다. 포스터월과 예고편 스크린에 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": half_d - 2.5, "heading": 0.0},
        "style": "lobby",
        "walls": [],          # 매점·상영관 입구가 있어 로비 모양은 3D 쪽에서 직접 만든다
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall6_layout():
    """전시관6 · 홈씨어터룸: 대형 스크린 앞에 1인 관람석이 있는 고급 시청실."""
    width, depth, height = 8.6, 11.0, 3.25
    half_w, half_d = width / 2, depth / 2
    surface = 0.24            # 벽의 흡음 패널·세로 배턴보다 앞으로 나와야 한다
    frame_y = 1.62

    slots = []
    # 정면 대형 스크린(액자 없이 화면만)
    slots.append(_slot(
        "A1", "대형 스크린",
        0.0, 1.78, -half_d + 0.26, FACE_FRONT, width=6.0, height=2.5, frameless=True,
    ))
    for index, z in enumerate((-2.4, 0.4, 3.2)):
        slots.append(_slot(
            f"B{index + 1}", f"왼쪽 벽 {index + 1}번 자리",
            -half_w + surface, frame_y, z, FACE_RIGHT, width=1.35, height=1.0,
        ))
    for index, z in enumerate((-2.4, 0.4, 3.2)):
        slots.append(_slot(
            f"C{index + 1}", f"오른쪽 벽 {index + 1}번 자리",
            half_w - surface, frame_y, z, FACE_LEFT, width=1.35, height=1.0,
        ))

    return {
        "key": "hall6",
        "name": "전시관6 · 홈씨어터룸",
        "summary": "대형 스크린과 1인 관람석이 있는 고급 시청실입니다. 좌우 벽 액자에 함께 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": half_d - 1.4, "heading": 0.0},
        "style": "theater",
        "walls": [],          # 스크린 벽·흡음 패널이 있어 방 모양은 3D 쪽에서 직접 만든다
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall7_layout():
    """전시관7 · 버스정류장형: 유리 부스 대기실이 있는 길가 버스정류장."""
    width, depth, height = 30.0, 16.0, 9.0
    half_w = width / 2
    glass = 0.07              # 유리에서 살짝 띄워 붙이는 간격
    back_z = 1.8              # 유리 부스 뒷유리(차도 반대쪽)
    side_x = 5.5              # 유리 부스 양옆 유리

    slots = []
    # 뒷유리에 군데군데 붙은 포스터
    for index, x in enumerate((-3.9, 0.0, 3.9)):
        slots.append(_slot(
            f"A{index + 1}", f"유리부스 뒷유리 {index + 1}번 자리",
            x, 1.5, back_z - glass, FACE_BACK,
            width=1.0, height=1.4, frameless=True,
        ))
    # 부스 양옆 유리는 정류장 광고판 자리(세로로 길다)
    slots.append(_slot(
        "B1", "유리부스 왼쪽 광고판",
        -side_x + glass, 1.45, 0.3, FACE_RIGHT, width=1.25, height=1.85, frameless=True,
    ))
    slots.append(_slot(
        "B2", "유리부스 오른쪽 광고판",
        side_x - glass, 1.45, 0.3, FACE_LEFT, width=1.25, height=1.85, frameless=True,
    ))
    # 인도에 선 전시봉 · 차도 쪽 줄(인도 안쪽을 바라본다)
    for index, x in enumerate((-11.0, -7.6, 7.6, 11.0)):
        slots.append(_slot(
            f"C{index + 1}", f"차도쪽 전시봉 {index + 1}번 자리",
            x, 1.7, -3.2, FACE_FRONT, width=1.3, height=1.75,
        ))
    # 가게 쪽 줄(차도를 바라본다)
    for index, x in enumerate((-9.2, -4.6, 4.6, 9.2)):
        slots.append(_slot(
            f"D{index + 1}", f"가게쪽 전시봉 {index + 1}번 자리",
            x, 1.7, 5.2, FACE_BACK, width=1.3, height=1.75,
        ))

    return {
        "key": "hall7",
        "name": "전시관7 · 버스정류장형",
        "summary": "유리 부스 대기실이 있는 길가 버스정류장입니다. 인도의 전시봉과 유리 포스터에 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": -12.0, "z": 1.2, "heading": -math.pi / 2},
        "style": "busstop",
        "walls": [],          # 길가라 벽이 없다(차도·가게는 3D 쪽에서 만든다)
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall8_layout():
    """전시관8 · 골목 담벼락형: 포스터와 그라피티가 붙은 좁은 골목."""
    width, depth, height = 9.0, 34.0, 14.0
    half_w = width / 2
    surface = 0.08
    facade = 5.6              # 담벼락 뒤로 물러선 왼쪽 건물 외벽

    slots = []
    # 왼쪽 담벼락 — 세로 포스터
    for index, z in enumerate((-11.5, -5.0, 1.5, 8.0)):
        slots.append(_slot(
            f"A{index + 1}", f"왼쪽 담벼락 {index + 1}번 자리",
            -half_w + surface, 1.75, z, FACE_RIGHT,
            width=1.35, height=1.85, frameless=True,
        ))
    # 오른쪽 담벼락 — 가로 사진
    for index, z in enumerate((-13.0, -7.0, 0.0, 6.5, 12.0)):
        slots.append(_slot(
            f"B{index + 1}", f"오른쪽 담벼락 {index + 1}번 자리",
            half_w - surface, 1.7, z, FACE_LEFT,
            width=1.6, height=1.1, frameless=True,
        ))
    # 건물 외벽에 크게 거는 자리
    slots.append(_slot(
        "C1", "오른쪽 건물 외벽 대형 자리",
        half_w - surface, 6.4, -4.0, FACE_LEFT, width=5.0, height=3.4, frameless=True,
    ))
    slots.append(_slot(
        "C2", "왼쪽 건물 외벽 대형 자리",
        -facade + surface, 6.8, 5.0, FACE_RIGHT, width=4.6, height=3.2, frameless=True,
    ))
    # 골목 바닥에 눕혀 두는 자리(tilt 로 바닥에 깔린다)
    floor_spots = ((-1.3, -8.5, FACE_FRONT), (1.3, -1.5, FACE_FRONT), (-1.3, 5.5, FACE_BACK))
    for index, (x, z, face) in enumerate(floor_spots):
        slots.append(_slot(
            f"D{index + 1}", f"골목 바닥 {index + 1}번 자리",
            x, 0.06, z, face, width=2.0, height=1.4,
            frameless=True, tilt=-math.pi / 2,
        ))

    return {
        "key": "hall8",
        "name": "전시관8 · 골목 담벼락형",
        "summary": "그라피티가 그려진 좁은 골목입니다. 담벼락·건물 외벽·골목 바닥에 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": depth / 2 - 2.0, "heading": 0.0},
        "style": "alley",
        "walls": [],          # 담벼락·건물은 3D 쪽에서 직접 만든다
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


def _hall9_layout():
    """전시관9 · 건물 옥상형: 대형 스크린과 야경이 있는 옥상."""
    width, depth, height = 24.0, 18.0, 10.0
    half_w, half_d = width / 2, depth / 2
    across_x = 18.0           # 건너편 건물 외벽
    penthouse_z = half_d - 2.6

    slots = []
    # 옥상 한쪽에 세운 대형 스크린(액자 없이 화면만)
    slots.append(_slot(
        "A1", "옥상 대형 스크린",
        0.0, 3.9, -half_d + 0.45, FACE_FRONT, width=8.4, height=4.7, frameless=True,
    ))
    # 왼쪽 난간 위 배너 걸이
    for index, z in enumerate((-4.6, 0.0, 4.6)):
        slots.append(_slot(
            f"B{index + 1}", f"왼쪽 난간 배너 {index + 1}번 자리",
            -half_w + 0.34, 1.95, z, FACE_RIGHT, width=2.5, height=1.7, frameless=True,
        ))
    # 옥탑방 외벽
    for index, x in enumerate((-2.6, 2.6)):
        slots.append(_slot(
            f"C{index + 1}", f"옥탑방 외벽 {index + 1}번 자리",
            x, 1.85, penthouse_z - 0.12, FACE_BACK, width=2.3, height=1.55,
        ))
    # 건너편 건물 외벽(옥상에서 건너다보며 관람한다)
    for index, (z, y) in enumerate(((-4.2, 5.2), (1.4, 6.2), (6.6, 4.8))):
        slots.append(_slot(
            f"D{index + 1}", f"건너편 건물 외벽 {index + 1}번 자리",
            across_x - 0.12, y, z, FACE_LEFT, width=5.4, height=3.6, frameless=True,
        ))

    return {
        "key": "hall9",
        "name": "전시관9 · 건물 옥상형",
        "summary": "도시 야경이 보이는 건물 옥상입니다. 난간·옥탑방·건너편 건물 외벽과 대형 스크린에 전시합니다.",
        "size": {"width": width, "depth": depth, "height": height},
        "spawn": {"x": 0.0, "z": half_d - 4.2, "heading": 0.0},
        "style": "rooftop",
        "walls": [],          # 옥상이라 벽 대신 난간과 옥탑방을 3D 쪽에서 만든다
        "partitions": [],
        "skylight": None,
        "balcony": None,
        "slots": slots,
    }


HALL_LAYOUTS = {
    "hall1": _hall1_layout(),
    "hall2": _hall2_layout(),
    "hall3": _hall3_layout(),
    "hall4": _hall4_layout(),
    "hall5": _hall5_layout(),
    "hall6": _hall6_layout(),
    "hall7": _hall7_layout(),
    "hall8": _hall8_layout(),
    "hall9": _hall9_layout(),
}
HALL_TYPES = tuple(HALL_LAYOUTS)
DEFAULT_HALL_TYPE = "hall1"

DEFAULTS = {
    "hall1": {"wall_color": "#f5f1ea", "floor_color": "#d8b184", "accent_color": "#2f6df6"},
    "hall2": {"wall_color": "#f2f2f2", "floor_color": "#b8b8b4", "accent_color": "#111827"},
    "hall3": {"wall_color": "#efe7d5", "floor_color": "#c89b62", "accent_color": "#15803d"},
    "hall4": {"wall_color": "#efe3cb", "floor_color": "#7aa356", "accent_color": "#0ea5e9"},
    "hall5": {"wall_color": "#4a3f52", "floor_color": "#6d1f2e", "accent_color": "#e11d48"},
    "hall6": {"wall_color": "#33405a", "floor_color": "#4a3b34", "accent_color": "#c9a227"},
    "hall7": {"wall_color": "#dfe6ee", "floor_color": "#b9bcc0", "accent_color": "#0ea5e9"},
    "hall8": {"wall_color": "#cbc3b6", "floor_color": "#8d8a85", "accent_color": "#a855f7"},
    "hall9": {"wall_color": "#3a4152", "floor_color": "#6f6a63", "accent_color": "#f97316"},
}

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------


def init_exhibition_schema():
    """3D전시장 테이블과 업로드 폴더를 준비한다(기존 데이터는 보존)."""
    for directory in (EXHIBITION_ROOT, MEDIA_ROOT, BGM_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS exhibition_halls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hall_type TEXT NOT NULL DEFAULT 'hall1',
                intro_title TEXT NOT NULL DEFAULT '',
                intro_text TEXT NOT NULL DEFAULT '',
                wall_color TEXT NOT NULL DEFAULT '#f5f1ea',
                floor_color TEXT NOT NULL DEFAULT '#d8b184',
                accent_color TEXT NOT NULL DEFAULT '#2f6df6',
                light_intensity INTEGER NOT NULL DEFAULT 100,
                move_speed INTEGER NOT NULL DEFAULT 100,
                placard_enabled INTEGER NOT NULL DEFAULT 1,
                auto_slide INTEGER NOT NULL DEFAULT 1,
                slide_seconds INTEGER NOT NULL DEFAULT 5,
                bgm_enabled INTEGER NOT NULL DEFAULT 1,
                bgm_volume INTEGER NOT NULL DEFAULT 40,
                bgm_filename TEXT,
                bgm_path TEXT,
                is_published INTEGER NOT NULL DEFAULT 1,
                view_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_by_emp_no TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS exhibition_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hall_id INTEGER NOT NULL,
                slot_code TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                artist TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(hall_id, slot_code)
            );
            CREATE TABLE IF NOT EXISTS exhibition_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hall_id INTEGER NOT NULL,
                slot_id INTEGER NOT NULL,
                media_kind TEXT NOT NULL DEFAULT 'image',
                filename TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_exhibition_slots_hall
                ON exhibition_slots(hall_id, slot_code);
            CREATE INDEX IF NOT EXISTS idx_exhibition_media_slot
                ON exhibition_media(slot_id, sort_order, id);
        """)
        # 기존 전시관도 자료를 유지한 채 새 재질 설정을 사용할 수 있다.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(exhibition_halls)")}
        for column in ("wall_texture", "floor_texture", "frame_style"):
            if column not in columns:
                conn.execute(f"ALTER TABLE exhibition_halls ADD COLUMN {column} TEXT NOT NULL DEFAULT 'auto'")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def _natural_key(name: str):
    """`사진_10.jpg`가 `사진_9.jpg` 뒤에 오도록 숫자를 숫자로 비교한다."""
    base = Path(str(name or "").replace("\\", "/")).name.casefold()
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", base)
    ]


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _current_name() -> str:
    return str(session.get("user_name") or session.get("emp_no") or "").strip()


def _clamp_int(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _color(value, fallback: str) -> str:
    text = str(value or "").strip()
    return text if HEX_COLOR.match(text) else fallback


def _layout(hall_type: str) -> dict:
    return HALL_LAYOUTS.get(hall_type) or HALL_LAYOUTS[DEFAULT_HALL_TYPE]


def _get_hall(conn, hall_id: int):
    hall = conn.execute(
        "SELECT * FROM exhibition_halls WHERE id=?", (hall_id,)
    ).fetchone()
    if not hall:
        abort(404)
    return hall


def _is_owner(row) -> bool:
    owner = str(row["created_by_emp_no"] or "").strip()
    return bool(owner) and owner == str(session.get("emp_no") or "").strip()


def _can_manage(row) -> bool:
    """전시관을 만든 사람 본인이나 관리자만 셋팅을 바꿀 수 있다."""
    return _is_owner(row) or is_admin_session()


def _require_manage(hall):
    if not _can_manage(hall):
        abort(403)


def _ensure_slots(conn, hall) -> None:
    """전시관 모양이 가진 전시공간이 DB에 모두 있는지 확인하고 없으면 만든다."""
    layout = _layout(hall["hall_type"])
    existing = {
        row["slot_code"] for row in conn.execute(
            "SELECT slot_code FROM exhibition_slots WHERE hall_id=?", (hall["id"],)
        ).fetchall()
    }
    missing = [slot for slot in layout["slots"] if slot["code"] not in existing]
    if not missing:
        return
    conn.executemany(
        "INSERT INTO exhibition_slots (hall_id, slot_code) VALUES (?, ?)",
        [(hall["id"], slot["code"]) for slot in missing],
    )
    conn.commit()


def _slot_rows(conn, hall_id: int) -> dict:
    return {
        row["slot_code"]: row for row in conn.execute(
            "SELECT * FROM exhibition_slots WHERE hall_id=?", (hall_id,)
        ).fetchall()
    }


def _media_rows(conn, hall_id: int) -> dict:
    """전시공간 id별 미디어 목록."""
    grouped: dict[int, list] = {}
    rows = conn.execute(
        """SELECT * FROM exhibition_media WHERE hall_id=?
           ORDER BY slot_id, sort_order, id""",
        (hall_id,),
    ).fetchall()
    for row in rows:
        grouped.setdefault(int(row["slot_id"]), []).append(row)
    return grouped


def _hall_dir(hall_id: int) -> Path:
    return MEDIA_ROOT / str(hall_id)


def _remove_file(path) -> None:
    if not path:
        return
    try:
        Path(str(path)).unlink(missing_ok=True)
    except OSError:
        pass


def _touch_hall(conn, hall_id: int) -> None:
    conn.execute(
        "UPDATE exhibition_halls SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (hall_id,),
    )


# ---------------------------------------------------------------------------
# 업로드 저장
# ---------------------------------------------------------------------------


def _upload_size(upload) -> int:
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    return int(size)


def _store_image(upload, folder: Path):
    raw_name = original_filename(upload.filename, "photo")
    size = _upload_size(upload)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"‘{raw_name}’ 사진이 {MAX_IMAGE_BYTES // (1024 * 1024)}MB를 초과합니다."
        )
    try:
        with Image.open(upload.stream) as image:
            image.verify()
        upload.stream.seek(0)
        with Image.open(upload.stream) as image:
            image_format = image.format
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"‘{raw_name}’은(는) 정상적인 사진 파일이 아닙니다.") from exc
    finally:
        upload.stream.seek(0)
    if image_format not in IMAGE_FORMATS:
        raise ValueError("JPG, PNG, WEBP, GIF 사진만 올릴 수 있습니다.")

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / encrypted_storage_name(raw_name)
    encrypt_upload(upload, path)
    return raw_name, str(path), int(width or 0), int(height or 0)


def _store_video(upload, folder: Path):
    raw_name = original_filename(upload.filename, "video")
    size = _upload_size(upload)
    if size > MAX_VIDEO_BYTES:
        raise ValueError(
            f"‘{raw_name}’ 동영상이 {MAX_VIDEO_BYTES // (1024 * 1024)}MB를 초과합니다."
        )
    if size <= 0:
        raise ValueError(f"‘{raw_name}’ 동영상이 비어 있습니다.")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / encrypted_storage_name(raw_name)
    encrypt_upload(upload, path)
    return raw_name, str(path), 0, 0


def _store_media(upload, folder: Path):
    """사진이면 사진으로, 동영상이면 동영상으로 저장하고 종류를 함께 돌려준다."""
    suffix = Path(str(upload.filename or "")).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        raw_name, path, width, height = _store_image(upload, folder)
        return "image", raw_name, path, width, height
    if suffix in VIDEO_EXTENSIONS:
        raw_name, path, width, height = _store_video(upload, folder)
        return "video", raw_name, path, width, height
    raise ValueError(UPLOAD_FORMAT_MESSAGE)


def _store_bgm(upload):
    raw_name = original_filename(upload.filename, "bgm")
    if Path(str(upload.filename or "")).suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(AUDIO_FORMAT_MESSAGE)
    size = _upload_size(upload)
    if size > MAX_AUDIO_BYTES:
        raise ValueError(
            f"‘{raw_name}’ 음악이 {MAX_AUDIO_BYTES // (1024 * 1024)}MB를 초과합니다."
        )
    BGM_ROOT.mkdir(parents=True, exist_ok=True)
    path = BGM_ROOT / encrypted_storage_name(raw_name)
    encrypt_upload(upload, path)
    return raw_name, str(path)


def _ranged_response(path: str, display_name: str, mimetype: str) -> Response:
    """동영상·음악이 되감기(Range 요청)될 수 있도록 부분 전송을 지원한다.

    저장 파일은 앞에서부터 순서대로 복호화되므로, 시작 지점까지는 읽고 버린 뒤
    필요한 구간만 내보낸다. 파일 전체를 메모리에 올리지 않는다.
    """
    total = plaintext_size(path)
    range_header = str(request.headers.get("Range") or "").strip()
    match = re.match(r"^bytes=(\d*)-(\d*)$", range_header) if range_header else None

    if not match or total <= 0:
        response = encrypted_response(
            path, display_name, as_attachment=False, mimetype=mimetype
        )
        response.headers["Accept-Ranges"] = "bytes"
        return response

    start_text, end_text = match.group(1), match.group(2)
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
    else:
        # bytes=-500 : 끝에서 500바이트
        length = int(end_text or 0)
        start = max(total - length, 0)
        end = total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        response = Response(status=416, mimetype=mimetype)
        response.headers["Content-Range"] = f"bytes */{total}"
        response.headers["Accept-Ranges"] = "bytes"
        return response
    end = min(end, start + RANGE_CHUNK_LIMIT - 1)
    length = end - start + 1

    def stream():
        position = 0
        remaining = length
        for chunk in iter_decrypted(path):
            if remaining <= 0:
                break
            chunk_end = position + len(chunk)
            if chunk_end <= start:
                position = chunk_end
                continue
            offset = max(start - position, 0)
            piece = chunk[offset:offset + remaining]
            remaining -= len(piece)
            position = chunk_end
            yield piece

    response = Response(stream(), status=206, mimetype=mimetype)
    response.headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    response.headers["Content-Length"] = str(length)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# 관람용 데이터 만들기
# ---------------------------------------------------------------------------


MATERIAL_CHOICES = {
    "wall_texture": {"auto", "plaster", "concrete", "limestone", "plain"},
    "floor_texture": {"auto", "oak", "walnut", "concrete", "limestone", "plain"},
    "frame_style": {"auto", "white", "oak", "walnut", "black", "brass"},
}


def _material_choice(field, hall):
    value = request.form.get(field, hall[field])
    return value if value in MATERIAL_CHOICES[field] else hall[field]


def _hall_settings(hall) -> dict:
    return {
        "id": int(hall["id"]),
        "name": hall["name"],
        "hallType": hall["hall_type"],
        "introTitle": hall["intro_title"],
        "introText": hall["intro_text"],
        "wallColor": hall["wall_color"],
        "floorColor": hall["floor_color"],
        "wallTexture": hall["wall_texture"],
        "floorTexture": hall["floor_texture"],
        "frameStyle": hall["frame_style"],
        "accentColor": hall["accent_color"],
        "lightIntensity": int(hall["light_intensity"]) / 100.0,
        "moveSpeed": int(hall["move_speed"]) / 100.0,
        "placardEnabled": bool(hall["placard_enabled"]),
        "autoSlide": bool(hall["auto_slide"]),
        "slideSeconds": int(hall["slide_seconds"]),
        "bgmEnabled": bool(hall["bgm_enabled"]) and bool(hall["bgm_path"]),
        "bgmVolume": int(hall["bgm_volume"]) / 100.0,
        "bgmUrl": (
            url_for("exhibition.serve_bgm", hall_id=hall["id"])
            if hall["bgm_path"] else ""
        ),
    }


def _scene_script_json(scene: dict) -> str:
    """<script> 안에 그대로 넣어도 안전한 JSON 문자열.

    작품제목·작가명에 ``</script>`` 같은 글자가 들어가면 태그가 끊겨
    관람 화면이 "전시관을 준비하는 중…"에서 멈추므로 미리 막아 둔다.
    """
    return (
        json.dumps(scene, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _scene_payload(conn, hall) -> dict:
    """관람 화면이 그대로 받아 쓰는 전시관 데이터."""
    layout = _layout(hall["hall_type"])
    slot_rows = _slot_rows(conn, hall["id"])
    media_map = _media_rows(conn, hall["id"])

    slots = []
    for slot in layout["slots"]:
        row = slot_rows.get(slot["code"])
        items = media_map.get(int(row["id"]), []) if row else []
        media = [
            {
                "id": int(item["id"]),
                "kind": item["media_kind"],
                "name": item["filename"],
                "width": int(item["width"] or 0),
                "height": int(item["height"] or 0),
                "url": url_for("exhibition.serve_media", media_id=item["id"]),
            }
            for item in items
        ]
        slots.append({
            **{key: slot[key] for key in
               ("code", "name", "x", "y", "z", "rotationY", "tilt", "width",
                "height", "frameless")},
            "title": row["title"] if row else "",
            "artist": row["artist"] if row else "",
            "description": row["description"] if row else "",
            "media": media,
        })

    return {
        "hall": _hall_settings(hall),
        "layout": {
            "key": layout["key"],
            "name": layout["name"],
            "style": layout["style"],
            "size": layout["size"],
            "spawn": layout["spawn"],
            "walls": layout["walls"],
            "partitions": layout["partitions"],
            "skylight": layout["skylight"],
            "balcony": layout["balcony"],
        },
        "slots": slots,
    }


# ---------------------------------------------------------------------------
# 전시관 목록 · 만들기 · 지우기
# ---------------------------------------------------------------------------


@exhibition_bp.route("/")
@menu_permission_required("exhibition_main")
def index():
    conn = get_db()
    try:
        halls = conn.execute(
            """SELECT h.*,
                      (SELECT COUNT(*) FROM exhibition_media m WHERE m.hall_id = h.id)
                          AS media_count
               FROM exhibition_halls h
               ORDER BY h.updated_at DESC, h.id DESC"""
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "exhibition/index.html",
        halls=halls,
        layouts=HALL_LAYOUTS,
        slot_counts={key: len(value["slots"]) for key, value in HALL_LAYOUTS.items()},
        can_manage_map={hall["id"]: _can_manage(hall) for hall in halls},
    )


@exhibition_bp.route("/new", methods=["POST"])
@menu_permission_required("exhibition_main")
def create_hall():
    hall_type = str(request.form.get("hall_type") or "").strip()
    if hall_type not in HALL_LAYOUTS:
        flash("전시관 종류를 목록에서 골라 주세요.", "error")
        return redirect(url_for("exhibition.index"))

    layout = HALL_LAYOUTS[hall_type]
    name = _clean_text(request.form.get("name"), 80) or layout["name"]
    defaults = DEFAULTS[hall_type]

    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM exhibition_halls").fetchone()[0]
        if int(total) >= MAX_HALLS:
            flash(f"전시관은 최대 {MAX_HALLS}개까지 만들 수 있습니다.", "error")
            return redirect(url_for("exhibition.index"))

        cursor = conn.execute(
            """INSERT INTO exhibition_halls
                   (name, hall_type, intro_title, wall_color, floor_color,
                    accent_color, created_by, created_by_emp_no)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name, hall_type, name,
                defaults["wall_color"], defaults["floor_color"], defaults["accent_color"],
                _current_name(), str(session.get("emp_no") or ""),
            ),
        )
        hall_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO exhibition_slots (hall_id, slot_code) VALUES (?, ?)",
            [(hall_id, slot["code"]) for slot in layout["slots"]],
        )
        conn.commit()
    finally:
        conn.close()

    flash(f"‘{name}’ 전시관을 만들었습니다. 전시파일을 올려 주세요.", "success")
    return redirect(url_for("exhibition.files", hall_id=hall_id))


@exhibition_bp.route("/<int:hall_id>/delete", methods=["POST"])
@menu_permission_required("exhibition_main")
def delete_hall(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        paths = [
            row["file_path"] for row in conn.execute(
                "SELECT file_path FROM exhibition_media WHERE hall_id=?", (hall_id,)
            ).fetchall()
        ]
        bgm_path = hall["bgm_path"]
        name = hall["name"]
        conn.execute("DELETE FROM exhibition_media WHERE hall_id=?", (hall_id,))
        conn.execute("DELETE FROM exhibition_slots WHERE hall_id=?", (hall_id,))
        conn.execute("DELETE FROM exhibition_halls WHERE id=?", (hall_id,))
        conn.commit()
    finally:
        conn.close()

    for path in paths:
        _remove_file(path)
    _remove_file(bgm_path)
    try:
        shutil.rmtree(_hall_dir(hall_id), ignore_errors=True)
    except OSError:
        pass

    flash(f"‘{name}’ 전시관을 삭제했습니다.", "success")
    return redirect(url_for("exhibition.index"))


# ---------------------------------------------------------------------------
# 전시관 셋팅
# ---------------------------------------------------------------------------


@exhibition_bp.route("/<int:hall_id>/settings", methods=["GET", "POST"])
@menu_permission_required("exhibition_main")
def settings(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        _ensure_slots(conn, hall)

        if request.method == "GET":
            return render_template(
                "exhibition/settings.html",
                hall=hall,
                layout=_layout(hall["hall_type"]),
                slot_count=len(_layout(hall["hall_type"])["slots"]),
            )

        defaults = DEFAULTS.get(hall["hall_type"], DEFAULTS[DEFAULT_HALL_TYPE])
        name = _clean_text(request.form.get("name"), 80)
        if not name:
            flash("전시관 이름을 입력해 주세요.", "error")
            return redirect(url_for("exhibition.settings", hall_id=hall_id))

        bgm_filename = hall["bgm_filename"]
        bgm_path = hall["bgm_path"]
        old_bgm = None
        bgm_upload = request.files.get("bgm")
        if request.form.get("remove_bgm") == "1" and bgm_path:
            old_bgm, bgm_filename, bgm_path = bgm_path, None, None
        elif bgm_upload and bgm_upload.filename:
            try:
                new_name, new_path = _store_bgm(bgm_upload)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("exhibition.settings", hall_id=hall_id))
            old_bgm = bgm_path
            bgm_filename, bgm_path = new_name, new_path

        conn.execute(
            """UPDATE exhibition_halls SET
                   name=?, intro_title=?, intro_text=?,
                   wall_color=?, floor_color=?, accent_color=?,
                   wall_texture=?, floor_texture=?, frame_style=?,
                   light_intensity=?, move_speed=?, placard_enabled=?,
                   auto_slide=?, slide_seconds=?,
                   bgm_enabled=?, bgm_volume=?, bgm_filename=?, bgm_path=?,
                   is_published=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                name,
                _clean_text(request.form.get("intro_title"), 80),
                _clean_text(request.form.get("intro_text"), 1000),
                _color(request.form.get("wall_color"), defaults["wall_color"]),
                _color(request.form.get("floor_color"), defaults["floor_color"]),
                _color(request.form.get("accent_color"), defaults["accent_color"]),
                _material_choice("wall_texture", hall),
                _material_choice("floor_texture", hall),
                _material_choice("frame_style", hall),
                _clamp_int(request.form.get("light_intensity"), 30, 200, 100),
                _clamp_int(request.form.get("move_speed"), 50, 200, 100),
                1 if request.form.get("placard_enabled") else 0,
                1 if request.form.get("auto_slide") else 0,
                _clamp_int(request.form.get("slide_seconds"), 2, 30, 5),
                1 if request.form.get("bgm_enabled") else 0,
                _clamp_int(request.form.get("bgm_volume"), 0, 100, 40),
                bgm_filename,
                bgm_path,
                1 if request.form.get("is_published") else 0,
                hall_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if old_bgm:
        _remove_file(old_bgm)
    flash("전시관 설정을 저장했습니다.", "success")
    return redirect(url_for("exhibition.settings", hall_id=hall_id))


# ---------------------------------------------------------------------------
# 전시파일 셋팅
# ---------------------------------------------------------------------------


@exhibition_bp.route("/<int:hall_id>/files")
@menu_permission_required("exhibition_main")
def files(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        _ensure_slots(conn, hall)
        layout = _layout(hall["hall_type"])
        slot_rows = _slot_rows(conn, hall["id"])
        media_map = _media_rows(conn, hall["id"])
    finally:
        conn.close()

    slots = []
    for slot in layout["slots"]:
        row = slot_rows.get(slot["code"])
        slots.append({
            "code": slot["code"],
            "name": slot["name"],
            "row": row,
            "media": media_map.get(int(row["id"]), []) if row else [],
        })

    return render_template(
        "exhibition/files.html",
        hall=hall,
        layout=layout,
        slots=slots,
        max_media=MAX_MEDIA_PER_SLOT,
        max_image_mb=MAX_IMAGE_BYTES // (1024 * 1024),
        max_video_mb=MAX_VIDEO_BYTES // (1024 * 1024),
    )


@exhibition_bp.route("/<int:hall_id>/slots/<slot_code>", methods=["POST"])
@menu_permission_required("exhibition_main")
def save_slot(hall_id, slot_code):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        _ensure_slots(conn, hall)
        row = conn.execute(
            "SELECT * FROM exhibition_slots WHERE hall_id=? AND slot_code=?",
            (hall_id, slot_code),
        ).fetchone()
        if not row:
            abort(404)

        # 여러 장을 한꺼번에 고르면 파일 이름 순서대로 걸리게 한다.
        uploads = sorted(
            (upload for upload in request.files.getlist("media")
             if upload and upload.filename),
            key=lambda upload: _natural_key(upload.filename),
        )
        if len(uploads) > MAX_MEDIA_PER_SLOT:
            flash(
                f"한 전시공간에는 최대 {MAX_MEDIA_PER_SLOT}개까지 담을 수 있습니다.",
                "error",
            )
            return redirect(url_for("exhibition.files", hall_id=hall_id))

        conn.execute(
            """UPDATE exhibition_slots
               SET title=?, artist=?, description=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                _clean_text(request.form.get("title"), 120),
                _clean_text(request.form.get("artist"), 60),
                _clean_text(request.form.get("description"), 500),
                row["id"],
            ),
        )

        if uploads:
            current = int(conn.execute(
                "SELECT COUNT(*) FROM exhibition_media WHERE slot_id=?", (row["id"],)
            ).fetchone()[0])
            if current + len(uploads) > MAX_MEDIA_PER_SLOT:
                conn.commit()
                flash(
                    f"이 전시공간에는 {MAX_MEDIA_PER_SLOT - current}개까지 더 올릴 수 있습니다.",
                    "error",
                )
                return redirect(url_for("exhibition.files", hall_id=hall_id))

            order = int(conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM exhibition_media WHERE slot_id=?",
                (row["id"],),
            ).fetchone()[0])
            stored = []
            try:
                for upload in uploads:
                    kind, name, path, width, height = _store_media(
                        upload, _hall_dir(hall_id)
                    )
                    order += 1
                    stored.append(path)
                    conn.execute(
                        """INSERT INTO exhibition_media
                               (hall_id, slot_id, media_kind, filename, file_path,
                                width, height, sort_order)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (hall_id, row["id"], kind, name, path, width, height, order),
                    )
            except ValueError as exc:
                conn.rollback()
                for path in stored:
                    _remove_file(path)
                flash(str(exc), "error")
                return redirect(url_for("exhibition.files", hall_id=hall_id))

        _touch_hall(conn, hall_id)
        conn.commit()
    finally:
        conn.close()

    flash("전시공간 내용을 저장했습니다.", "success")
    return redirect(url_for("exhibition.files", hall_id=hall_id) + f"#slot-{slot_code}")


@exhibition_bp.route("/<int:hall_id>/media/<int:media_id>/delete", methods=["POST"])
@menu_permission_required("exhibition_main")
def delete_media(hall_id, media_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        row = conn.execute(
            "SELECT * FROM exhibition_media WHERE id=? AND hall_id=?",
            (media_id, hall_id),
        ).fetchone()
        if not row:
            abort(404)
        slot_code = conn.execute(
            "SELECT slot_code FROM exhibition_slots WHERE id=?", (row["slot_id"],)
        ).fetchone()
        path = row["file_path"]
        conn.execute("DELETE FROM exhibition_media WHERE id=?", (media_id,))
        _touch_hall(conn, hall_id)
        conn.commit()
    finally:
        conn.close()

    _remove_file(path)
    flash("전시파일을 삭제했습니다.", "success")
    anchor = f"#slot-{slot_code['slot_code']}" if slot_code else ""
    return redirect(url_for("exhibition.files", hall_id=hall_id) + anchor)


@exhibition_bp.route("/<int:hall_id>/media/<int:media_id>/move", methods=["POST"])
@menu_permission_required("exhibition_main")
def move_media(hall_id, media_id):
    """같은 전시공간 안에서 순서를 한 칸 앞뒤로 옮긴다."""
    direction = "up" if str(request.form.get("direction")) == "up" else "down"
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        _require_manage(hall)
        row = conn.execute(
            "SELECT * FROM exhibition_media WHERE id=? AND hall_id=?",
            (media_id, hall_id),
        ).fetchone()
        if not row:
            abort(404)
        if direction == "up":
            neighbour = conn.execute(
                """SELECT * FROM exhibition_media
                   WHERE slot_id=? AND (sort_order < ? OR (sort_order = ? AND id < ?))
                   ORDER BY sort_order DESC, id DESC LIMIT 1""",
                (row["slot_id"], row["sort_order"], row["sort_order"], row["id"]),
            ).fetchone()
        else:
            neighbour = conn.execute(
                """SELECT * FROM exhibition_media
                   WHERE slot_id=? AND (sort_order > ? OR (sort_order = ? AND id > ?))
                   ORDER BY sort_order ASC, id ASC LIMIT 1""",
                (row["slot_id"], row["sort_order"], row["sort_order"], row["id"]),
            ).fetchone()
        slot_code = conn.execute(
            "SELECT slot_code FROM exhibition_slots WHERE id=?", (row["slot_id"],)
        ).fetchone()
        if neighbour:
            conn.execute(
                "UPDATE exhibition_media SET sort_order=? WHERE id=?",
                (int(neighbour["sort_order"]), int(row["id"])),
            )
            conn.execute(
                "UPDATE exhibition_media SET sort_order=? WHERE id=?",
                (int(row["sort_order"]), int(neighbour["id"])),
            )
            _touch_hall(conn, hall_id)
            conn.commit()
    finally:
        conn.close()

    anchor = f"#slot-{slot_code['slot_code']}" if slot_code else ""
    return redirect(url_for("exhibition.files", hall_id=hall_id) + anchor)


# ---------------------------------------------------------------------------
# 관람
# ---------------------------------------------------------------------------


@exhibition_bp.route("/<int:hall_id>/view")
@menu_permission_required("exhibition_main")
def view(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        if not int(hall["is_published"] or 0) and not _can_manage(hall):
            abort(403)
        _ensure_slots(conn, hall)
        conn.execute(
            "UPDATE exhibition_halls SET view_count = view_count + 1 WHERE id=?",
            (hall_id,),
        )
        conn.commit()
        hall = _get_hall(conn, hall_id)
        scene = _scene_payload(conn, hall)
    finally:
        conn.close()

    return render_template(
        "exhibition/view.html",
        hall=hall,
        scene_json=_scene_script_json(scene),
        can_manage=_can_manage(hall),
    )


@exhibition_bp.route("/<int:hall_id>/scene.json")
@menu_permission_required("exhibition_main")
def scene(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        if not int(hall["is_published"] or 0) and not _can_manage(hall):
            abort(403)
        _ensure_slots(conn, hall)
        payload = _scene_payload(conn, hall)
    finally:
        conn.close()
    return jsonify(payload)


@exhibition_bp.route("/media/<int:media_id>/file")
@menu_permission_required("exhibition_main")
def serve_media(media_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM exhibition_media WHERE id=?", (media_id,)
        ).fetchone()
        if not row:
            abort(404)
        hall = _get_hall(conn, int(row["hall_id"]))
        if not int(hall["is_published"] or 0) and not _can_manage(hall):
            abort(403)
    finally:
        conn.close()

    path = str(row["file_path"] or "")
    if not path or not os.path.isfile(path):
        abort(404)
    name = row["filename"] or "media"
    if row["media_kind"] == "video":
        suffix = Path(name).suffix.lower()
        mimetype = VIDEO_MIME_TYPES.get(suffix, "video/mp4")
        return _ranged_response(path, name, mimetype)
    return encrypted_response(path, name, as_attachment=False)


@exhibition_bp.route("/<int:hall_id>/bgm")
@menu_permission_required("exhibition_main")
def serve_bgm(hall_id):
    conn = get_db()
    try:
        hall = _get_hall(conn, hall_id)
        if not int(hall["is_published"] or 0) and not _can_manage(hall):
            abort(403)
        path = str(hall["bgm_path"] or "")
        name = hall["bgm_filename"] or "bgm.mp3"
    finally:
        conn.close()

    if not path or not os.path.isfile(path):
        abort(404)
    mimetype = AUDIO_MIME_TYPES.get(Path(name).suffix.lower(), "audio/mpeg")
    return _ranged_response(path, name, mimetype)
