"""통합관리에서 관리하는 전사 공용 AI api설정(OpenAI·Claude 프리셋, 최대 9개).

AI에이전트와 스마트공문발송이 공통으로 "현재 적용 프리셋" 하나를 읽어서 실행된다.
프리셋은 기본 3개로 시작해 필요할 때마다 최대 9개까지 늘릴 수 있고, 통합관리에서 그중 하나를 활성 프리셋으로 전환하는 방식이다.
과거에는 직원별로 각자 키를 저장했지만(smart_document_ai_settings), 지금은 전 직원이 이 설정을 공유한다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .database import get_db
from .security import load_credential_secret

PROVIDERS = ('openai', 'claude')
PROVIDER_LABELS = {'openai': 'OpenAI', 'claude': 'Claude'}
DEFAULT_PROVIDER = 'openai'

PROVIDER_MODELS = {
    'openai': ('gpt-6-luna', 'gpt-6-sol', 'gpt-6-astra'),
    'claude': ('claude-opus-5', 'claude-sonnet-5', 'claude-fable-5', 'claude-haiku-4-5-20251001'),
}
MODEL_LABELS = {
    'gpt-6-luna': '빠른 AI · GPT-6 Luna',
    'gpt-6-sol': '고급 AI · GPT-6 Sol',
    'gpt-6-astra': '최고급 AI · GPT-6 Astra',
    'claude-opus-5': 'Claude Opus 5 · 최고 성능',
    'claude-sonnet-5': 'Claude Sonnet 5 · 균형형',
    'claude-fable-5': 'Claude Fable 5 · 경량형',
    'claude-haiku-4-5-20251001': 'Claude Haiku 4.5 · 빠르고 경제적',
}
MODEL_SHORT_NAMES = {
    'gpt-6-luna': 'Luna',
    'gpt-6-sol': 'Sol',
    'gpt-6-astra': 'Astra',
    'claude-opus-5': 'Opus 5',
    'claude-sonnet-5': 'Sonnet 5',
    'claude-fable-5': 'Fable 5',
    'claude-haiku-4-5-20251001': 'Haiku 4.5',
}
DEFAULT_MODEL = {'openai': 'gpt-6-luna', 'claude': 'claude-sonnet-5'}

# 기존 프리셋의 등급을 유지한 채 새 모델로 승계한다.
LEGACY_OPENAI_MODELS = {
    'gpt-5.6-luna': 'gpt-6-luna',
    'gpt-5.6-terra': 'gpt-6-sol',
    'gpt-5.6-sol': 'gpt-6-astra',
}

SOURCE_LABELS = {
    'menu': '메뉴 등록 API 사용 중',
    'none': 'API 미설정',
}

MAX_PRESETS = 9
DEFAULT_VISIBLE_PRESETS = 3
PRESET_IDS = tuple(str(i) for i in range(1, MAX_PRESETS + 1))
DEFAULT_PRESET_LABELS = {str(i): f'프리셋 {i}' for i in range(1, MAX_PRESETS + 1)}

_SETTINGS_KEY = 'ai_api_settings'
_LEGACY_SINGLE_KEY = 'openai_api_settings'  # 프리셋 도입 전 단일 설정 스키마


def _fernet() -> Fernet:
    digest = hashlib.sha256(load_credential_secret().encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt_api_key(api_key: str) -> str:
    value = str(api_key or '').strip()
    if len(value) < 20 or len(value) > 500 or re.search(r'\s', value):
        raise ValueError('API 키 형식을 확인해주세요.')
    return _fernet().encrypt(value.encode('utf-8')).decode('ascii')


def _decrypt_api_key(token: str) -> str:
    try:
        return _fernet().decrypt(str(token).encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError('저장된 API 키를 복호화할 수 없습니다.') from exc


def validate_provider(value: Any) -> str:
    provider = str(value or '').strip().lower()
    if provider not in PROVIDERS:
        raise ValueError('제공사는 OpenAI, Claude 중에서 선택해주세요.')
    return provider


def validate_model(provider: str, value: Any) -> str:
    model = str(value or '').strip()
    allowed = PROVIDER_MODELS.get(provider, ())
    if model not in allowed:
        raise ValueError(f'{PROVIDER_LABELS.get(provider, provider)} 모델 목록 중에서 선택해주세요.')
    return model


def validate_preset_id(value: Any) -> str:
    preset_id = str(value or '').strip()
    if preset_id not in PRESET_IDS:
        raise ValueError('프리셋 번호는 1, 2, 3 중 하나여야 합니다.')
    return preset_id


def _ensure_admin_settings_table(conn) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')


def _read_json(conn, key: str) -> dict:
    row = conn.execute('SELECT value, updated_at FROM admin_settings WHERE key=?', (key,)).fetchone()
    if not row or not row['value']:
        return {}
    try:
        data = json.loads(row['value'])
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    data['_updated_at'] = str(row['updated_at'] or '')
    return data


def _legacy_table_fallback(conn) -> dict:
    """구 버전(직원별 스마트공문발송) 설정 중 가장 최근 값을 1회성 기본값으로 넘겨준다."""
    try:
        row = conn.execute('''
            SELECT api_key_encrypted, model, updated_at FROM smart_document_ai_settings
            WHERE api_key_encrypted IS NOT NULL AND api_key_encrypted<>''
            ORDER BY updated_at DESC LIMIT 1
        ''').fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    model = LEGACY_OPENAI_MODELS.get(row['model'], row['model'])
    return {
        'provider': 'openai',
        'api_key_encrypted': row['api_key_encrypted'],
        'model': model if model in PROVIDER_MODELS['openai'] else DEFAULT_MODEL['openai'],
        'updated_by': '',
        'updated_at': str(row['updated_at'] or ''),
    }


def _empty_preset() -> dict:
    return {'provider': DEFAULT_PROVIDER, 'model': DEFAULT_MODEL[DEFAULT_PROVIDER],
            'api_key_encrypted': '', 'updated_by': '', 'updated_at': ''}


def _load_store(conn) -> dict:
    """{'active': '1', 'visible_count': 3, 'presets': {'1': {...}, ...}} 형태로 정규화해 반환한다."""
    _ensure_admin_settings_table(conn)
    data = _read_json(conn, _SETTINGS_KEY)
    if data:
        presets = data.get('presets') if isinstance(data.get('presets'), dict) else {}
        active = str(data.get('active') or '1')
        raw_visible_count = data.get('visible_count')
    else:
        # 프리셋 도입 이전 단일 설정 스키마를 1회성으로 승계한다.
        legacy = _read_json(conn, _LEGACY_SINGLE_KEY) or _legacy_table_fallback(conn)
        presets = {'1': legacy} if legacy else {}
        active = '1'
        raw_visible_count = None
    normalized = {}
    for preset_id in PRESET_IDS:
        raw = presets.get(preset_id) or {}
        preset = _empty_preset()
        preset.update({k: v for k, v in raw.items() if k in preset})
        preset['label'] = str(raw.get('label') or DEFAULT_PRESET_LABELS[preset_id])
        if preset['provider'] not in PROVIDERS:
            preset['provider'] = DEFAULT_PROVIDER
        if preset['provider'] == 'openai':
            preset['model'] = LEGACY_OPENAI_MODELS.get(preset['model'], preset['model'])
        normalized[preset_id] = preset
    if active not in PRESET_IDS:
        active = '1'
    try:
        visible_count = int(raw_visible_count)
    except (TypeError, ValueError):
        visible_count = DEFAULT_VISIBLE_PRESETS
    # 활성 프리셋이나 이미 키가 등록된 프리셋 번호보다는 항상 크거나 같도록 보정한다
    # (예: 다른 관리자가 슬롯을 늘려둔 뒤 이 값을 안 가져온 상태로 저장하는 경우 대비).
    configured_ids = [int(pid) for pid, p in normalized.items() if p.get('api_key_encrypted')]
    floor = max([int(active), *configured_ids, DEFAULT_VISIBLE_PRESETS])
    visible_count = max(min(max(visible_count, 1), MAX_PRESETS), min(floor, MAX_PRESETS))
    return {'active': active, 'visible_count': visible_count, 'presets': normalized}


def _save_store(conn, store: dict) -> None:
    _ensure_admin_settings_table(conn)
    conn.execute('''
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
    ''', (_SETTINGS_KEY, json.dumps(store, ensure_ascii=False)))


def list_presets(conn=None) -> dict[str, Any]:
    """통합관리 화면에 프리셋 3개와 현재 활성 번호를 보여주기 위한 데이터."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
        result = {}
        for preset_id, preset in store['presets'].items():
            key = str(preset.get('api_key_encrypted') or '').strip()
            provider = preset['provider']
            has_key = bool(key)
            result[preset_id] = {
                'preset_id': preset_id,
                'label': preset['label'],
                'provider': provider,
                'provider_label': PROVIDER_LABELS.get(provider, provider),
                'model': preset['model'],
                'model_label': MODEL_LABELS.get(preset['model'], preset['model']),
                'has_menu_key': has_key,
                'masked_key': f"••••••••{key[-4:]}" if has_key else '',
                'updated_by': preset.get('updated_by') or '',
                'updated_at': preset.get('updated_at') or '',
                'configured': has_key,
            }
        return {'active': store['active'], 'visible_count': store['visible_count'], 'presets': result}
    finally:
        if owns_connection:
            conn.close()


def save_preset(preset_id: Any, provider: Any, model: Any, api_key: str = '',
                 clear_key: bool = False, actor: str = '') -> dict[str, Any]:
    preset_id = validate_preset_id(preset_id)
    provider = validate_provider(provider)
    model = validate_model(provider, model)
    api_key = str(api_key or '').strip()
    conn = get_db()
    try:
        store = _load_store(conn)
        existing = store['presets'][preset_id]
        if clear_key:
            token = ''
        elif api_key:
            token = _encrypt_api_key(api_key)
        elif existing['provider'] == provider:
            token = str(existing.get('api_key_encrypted') or '')
        else:
            token = ''  # 제공사가 바뀌면 이전 제공사의 키를 그대로 재사용하지 않는다.
        store['presets'][preset_id] = {
            'label': DEFAULT_PRESET_LABELS[preset_id],
            'provider': provider, 'model': model, 'api_key_encrypted': token,
            'updated_by': str(actor or ''), 'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        _save_store(conn, store)
        conn.commit()
        return list_presets(conn)['presets'][preset_id]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_active_preset(preset_id: Any, actor: str = '') -> dict[str, Any]:
    preset_id = validate_preset_id(preset_id)
    conn = get_db()
    try:
        store = _load_store(conn)
        store['active'] = preset_id
        _save_store(conn, store)
        conn.commit()
        return get_ai_settings(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_preset_slot(actor: str = '') -> int:
    """프리셋 등록 칸을 하나 더 늘린다(최대 MAX_PRESETS개). 새 개수를 반환한다."""
    conn = get_db()
    try:
        store = _load_store(conn)
        if store['visible_count'] >= MAX_PRESETS:
            raise ValueError(f'프리셋은 최대 {MAX_PRESETS}개까지 추가할 수 있습니다.')
        store['visible_count'] += 1
        _save_store(conn, store)
        conn.commit()
        return store['visible_count']
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_ai_settings(conn=None) -> dict[str, Any]:
    """현재 활성 프리셋을 해석해 실제 실행에 쓸 provider/model/api_key를 반환한다."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
        preset_id = store['active']
        preset = store['presets'][preset_id]
        provider = preset['provider']
        stored_token = str(preset.get('api_key_encrypted') or '').strip()
        if stored_token:
            api_key = _decrypt_api_key(stored_token)
            source = 'menu'
        else:
            api_key = ''
            source = 'none'
        return {
            'preset_id': preset_id,
            'preset_label': preset['label'],
            'provider': provider,
            'model': preset['model'],
            'api_key': api_key,
            'source': source,
            'has_menu_key': bool(stored_token),
            'updated_by': preset.get('updated_by') or '',
            'updated_at': preset.get('updated_at') or '',
        }
    finally:
        if owns_connection:
            conn.close()


def get_preset_api_key(preset_id: Any, conn=None) -> str:
    """지정한 프리셋에 저장된 키를 복호화해 반환한다(없으면 빈 문자열).

    연결 테스트처럼 서버 안에서만 잠깐 쓰고 화면에는 절대 노출하지 않는 용도로만 사용한다.
    """
    preset_id = validate_preset_id(preset_id)
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
        preset = store['presets'][preset_id]
        stored_token = str(preset.get('api_key_encrypted') or '').strip()
        return _decrypt_api_key(stored_token) if stored_token else ''
    finally:
        if owns_connection:
            conn.close()


def find_openai_api_key(conn=None) -> dict[str, Any]:
    """OpenAI에만 있는 기능(멀티TTS 음성합성 등)에 쓸 OpenAI 키를 찾는다.

    현재 적용 프리셋이 OpenAI면 그 키를 쓰고, 아니면(예: Claude 적용 중) 키가 등록된
    OpenAI 프리셋 중 번호가 가장 앞선 것을 쓴다. 키가 없으면 api_key가 빈 문자열이다.
    """
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
        presets = store['presets']
        visible_ids = PRESET_IDS[:store['visible_count']]
        candidates = [store['active']] + [pid for pid in visible_ids if pid != store['active']]
        for preset_id in candidates:
            preset = presets[preset_id]
            token = str(preset.get('api_key_encrypted') or '').strip()
            if preset['provider'] == 'openai' and token:
                return {'api_key': _decrypt_api_key(token), 'preset_id': preset_id,
                        'preset_label': preset['label']}
        return {'api_key': '', 'preset_id': '', 'preset_label': ''}
    finally:
        if owns_connection:
            conn.close()


def public_ai_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """비밀정보를 뺀, 화면 표시용 상태를 반환한다."""
    key = settings.get('api_key') or ''
    masked_key = f'••••••••{key[-4:]}' if key else ''
    provider = settings.get('provider') or DEFAULT_PROVIDER
    model = settings.get('model') or DEFAULT_MODEL.get(provider, '')
    source = settings.get('source') or 'none'
    preset_label = settings.get('preset_label') or ''
    provider_label = PROVIDER_LABELS.get(provider, provider)
    model_short_name = MODEL_SHORT_NAMES.get(model, model)
    status_text = (
        f"{model_short_name} 사용중"
        if key else f"{preset_label} · API 미설정" if preset_label else 'AI API 미설정'
    )
    return {
        'preset_id': settings.get('preset_id') or '1',
        'preset_label': preset_label,
        'provider': provider,
        'provider_label': provider_label,
        'model': model,
        'model_label': MODEL_LABELS.get(model, model),
        'model_short_name': model_short_name,
        'source': source,
        'source_label': SOURCE_LABELS.get(source, ''),
        'status_text': status_text,
        'masked_key': masked_key,
        'has_menu_key': bool(settings.get('has_menu_key')),
        'updated_by': settings.get('updated_by') or '',
        'updated_at': settings.get('updated_at') or '',
    }


def test_ai_connection(provider: str, api_key: str, model: str) -> str:
    """토큰을 소비하지 않는 모델 조회 요청으로 인증과 모델 접근을 확인한다."""
    provider = validate_provider(provider)
    if provider == 'claude':
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError('서버에 anthropic 라이브러리가 설치되어 있지 않습니다.') from exc
        client = Anthropic(api_key=api_key, timeout=15.0, max_retries=0)
        result = client.models.retrieve(model)
        return str(getattr(result, 'id', '') or model)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError('서버에 openai 라이브러리가 설치되어 있지 않습니다.') from exc
    client = OpenAI(api_key=api_key, timeout=15.0, max_retries=0)
    result = client.models.retrieve(model)
    return str(getattr(result, 'id', '') or model)


# 현재 이 프리셋을 사용하는 화면과, 각 화면이 호출 기록을 남기는 테이블.
# 새 화면이 이 프리셋으로 AI API를 호출하게 되면 여기에 한 줄만 추가하면
# API 사용량 집계에 자동으로 합산된다.
USAGE_SOURCES = (
    ('smart_document_history', '스마트공문발송'),
    ('ai_agent_history', 'AI에이전트'),
    ('interview_resume_analysis_history', '면접 이력서 분석'),
    ('classroom_guide_ai_history', '교실안내 평면도 분석'),
)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def get_combined_usage_summary(conn=None) -> dict[str, Any]:
    """여러 화면이 같은 프리셋으로 호출한 AI API 사용량을 한 곳에 합산해 보여준다."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        sources = []
        monthly_parts = []
        for table, label in USAGE_SOURCES:
            if not _table_exists(conn, table):
                continue
            row = conn.execute(f'''
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       MIN(created_at) AS first_used_at,
                       MAX(created_at) AS last_used_at
                FROM {table}
            ''').fetchone()
            sources.append({
                'key': table,
                'label': label,
                'requests': int(row['requests'] or 0),
                'input_tokens': int(row['input_tokens'] or 0),
                'output_tokens': int(row['output_tokens'] or 0),
                'total_tokens': int(row['total_tokens'] or 0),
                'first_used_at': str(row['first_used_at'] or ''),
                'last_used_at': str(row['last_used_at'] or ''),
            })
            monthly_parts.append(
                f"SELECT SUBSTR(created_at, 1, 7) AS month, total_tokens FROM {table}"
            )

        totals = {
            'requests': sum(item['requests'] for item in sources),
            'input_tokens': sum(item['input_tokens'] for item in sources),
            'output_tokens': sum(item['output_tokens'] for item in sources),
            'total_tokens': sum(item['total_tokens'] for item in sources),
        }

        monthly = []
        if monthly_parts:
            union_sql = ' UNION ALL '.join(monthly_parts)
            monthly = [
                dict(row) for row in conn.execute(f'''
                    SELECT month, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS total_tokens
                    FROM ({union_sql})
                    GROUP BY month ORDER BY month DESC LIMIT 12
                ''').fetchall()
            ]

        return {'sources': sources, 'totals': totals, 'monthly': monthly}
    finally:
        if owns_connection:
            conn.close()
