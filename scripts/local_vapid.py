"""이 PC에서 휴대폰 알림(웹푸시)을 테스트하기 위한 VAPID 키를 준비한다.

운영 서버(Render)는 환경변수의 키를 쓰므로 이 스크립트와 무관하다.
키는 data/security/local_vapid.json 에 한 번만 만들어 두고 계속 재사용한다.
(키가 바뀌면 이미 알림을 등록한 브라우저의 구독이 모두 무효가 된다.)

로컬서버실행.bat 가 호출하며, 표준출력으로 `이름=값` 세 줄을 내보낸다.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from routes.storage import SECURITY_ROOT  # noqa: E402

KEY_FILE = SECURITY_ROOT / 'local_vapid.json'
SUBJECT = 'mailto:admin@saedam.org'


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _generate():
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_raw = private_key.private_numbers().private_value.to_bytes(32, 'big')
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    return {
        'VAPID_PUBLIC_KEY': _b64url(public_raw),
        'VAPID_PRIVATE_KEY': _b64url(private_raw),
        'VAPID_SUBJECT': SUBJECT,
    }


def load_or_create():
    try:
        keys = json.loads(KEY_FILE.read_text(encoding='utf-8'))
        if all(keys.get(name) for name in ('VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY', 'VAPID_SUBJECT')):
            return keys
    except FileNotFoundError:
        pass
    keys = _generate()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps(keys, indent=2), encoding='utf-8')
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return keys


if __name__ == '__main__':
    for name, value in load_or_create().items():
        # 이미 환경변수로 지정했으면 그 값을 그대로 쓴다.
        print(f"{name}={os.environ.get(name) or value}")
