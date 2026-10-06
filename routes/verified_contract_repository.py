"""인증전자계약 데이터와 감사기록 저장 계층."""

from __future__ import annotations

import json
from collections.abc import Mapping


# 강사전자계약 / 임직원전자계약은 계약 테이블과 감사기록 테이블을 각각 따로 쓴다.
# 예전 verified_contracts / verified_contract_events 는 1회 복사 후 백업용으로만 남긴다.
LEGACY_CONTRACT_TABLE = "verified_contracts"
LEGACY_EVENT_TABLE = "verified_contract_events"
CONTRACT_TABLES = {
    "instructor": "instructor_verified_contracts",
    "employee": "employee_verified_contracts",
}
EVENT_TABLES = {
    "instructor": "instructor_verified_contract_events",
    "employee": "employee_verified_contract_events",
}
SPLIT_MIGRATION_KEY = "verified_contract_split_v1"


def current_kind() -> str | None:
    """요청 중이면 지금 처리 중인 메뉴(강사/임직원)를 돌려준다."""
    try:
        from flask import g, has_app_context

        return g.get("vc_kind") if has_app_context() else None
    except Exception:
        return None


def contract_table(kind: str | None = None) -> str:
    """kind가 없으면(스크립트·테스트) 예전 통합 테이블을 쓴다."""
    return CONTRACT_TABLES.get(kind or current_kind(), LEGACY_CONTRACT_TABLE)


def event_table(kind: str | None = None) -> str:
    return EVENT_TABLES.get(kind or current_kind(), LEGACY_EVENT_TABLE)


def contract_source(kinds=None) -> str:
    """여러 메뉴의 계약을 한 번에 조회할 때 쓰는 FROM 절 조각(UNION ALL 서브쿼리)."""
    names = [CONTRACT_TABLES[k] for k in (kinds or CONTRACT_TABLES) if k in CONTRACT_TABLES]
    if not names:
        return f"(SELECT * FROM {LEGACY_CONTRACT_TABLE} WHERE 0) AS vc"
    return "(" + " UNION ALL ".join(f"SELECT * FROM {name}" for name in names) + ") AS vc"


VERIFIED_CONTRACT_FIELDS = (
    "contract_type",
    "school_name",
    "department",
    "signer_name",
    "signer_email",
    "signer_phone",
    "signer_address",
    "signer_rrn_encrypted",
    "signer_bank_encrypted",
    "signer_account_encrypted",
    "contract_data_json",
    "status",
    "version",
    "title_snapshot",
    "terms1_snapshot",
    "terms2_snapshot",
    "company_snapshot_json",
    "agreement_snapshot_json",
    "invitation_token_hash",
    "invitation_expires_at",
    "invitation_sent_at",
    "opened_at",
    "otp_hash",
    "otp_expires_at",
    "otp_attempts",
    "otp_sent_at",
    "verified_at",
    "confirmed_name",
    "signature_filename",
    "signed_at",
    "ip_address",
    "user_agent",
    "pdf_filename",
    "pdf_sha256",
    "invite_mail_status",
    "invite_mail_error",
    "invite_channel",
    "invite_message_id",
    "otp_channel",
    "otp_message_id",
    "completion_mail_status",
    "completion_mail_error",
    "created_by",
)


def _ensure_contract_table(conn, table: str, events: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_type TEXT NOT NULL,
            school_name TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            signer_name TEXT NOT NULL,
            signer_email TEXT NOT NULL DEFAULT '',
            signer_phone TEXT NOT NULL DEFAULT '',
            signer_address TEXT NOT NULL DEFAULT '',
            signer_rrn_encrypted TEXT NOT NULL DEFAULT '',
            signer_bank_encrypted TEXT NOT NULL DEFAULT '',
            signer_account_encrypted TEXT NOT NULL DEFAULT '',
            contract_data_json TEXT NOT NULL DEFAULT '{{}}',
            status TEXT NOT NULL DEFAULT 'pending',
            version INTEGER NOT NULL DEFAULT 1,
            title_snapshot TEXT NOT NULL DEFAULT '',
            terms1_snapshot TEXT NOT NULL DEFAULT '',
            terms2_snapshot TEXT NOT NULL DEFAULT '',
            company_snapshot_json TEXT NOT NULL DEFAULT '{{}}',
            agreement_snapshot_json TEXT NOT NULL DEFAULT '[]',
            invitation_token_hash TEXT NOT NULL UNIQUE,
            invitation_expires_at TEXT NOT NULL,
            invitation_sent_at TEXT,
            opened_at TEXT,
            otp_hash TEXT,
            otp_expires_at TEXT,
            otp_attempts INTEGER NOT NULL DEFAULT 0,
            otp_sent_at TEXT,
            verified_at TEXT,
            confirmed_name TEXT,
            signature_filename TEXT,
            signed_at TEXT,
            ip_address TEXT,
            user_agent TEXT,
            pdf_filename TEXT,
            pdf_sha256 TEXT,
            invite_mail_status TEXT NOT NULL DEFAULT 'waiting',
            invite_mail_error TEXT,
            invite_channel TEXT NOT NULL DEFAULT '',
            invite_message_id TEXT,
            otp_channel TEXT NOT NULL DEFAULT '',
            otp_message_id TEXT,
            completion_mail_status TEXT,
            completion_mail_error TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing_columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for column in (
        "signer_rrn_encrypted",
        "signer_bank_encrypted",
        "signer_account_encrypted",
    ):
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    optional_columns = {
        "invite_channel": "TEXT NOT NULL DEFAULT ''",
        "invite_message_id": "TEXT",
        "otp_channel": "TEXT NOT NULL DEFAULT ''",
        "otp_message_id": "TEXT",
    }
    for column, definition in optional_columns.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {events} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details_json TEXT NOT NULL DEFAULT '{{}}',
            FOREIGN KEY(contract_id) REFERENCES {table}(id)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status, created_at)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_email ON {table}(signer_email)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_phone ON {table}(signer_phone)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{events}_contract ON {events}(contract_id, event_at)"
    )


def ensure_verified_contract_schema(conn) -> None:
    _ensure_contract_table(conn, LEGACY_CONTRACT_TABLE, LEGACY_EVENT_TABLE)
    for kind, table in CONTRACT_TABLES.items():
        _ensure_contract_table(conn, table, EVENT_TABLES[kind])


def split_verified_contracts(conn, kind_of) -> dict[str, int]:
    """예전 verified_contracts(+감사기록)를 강사/임직원 테이블로 1회 복사한다.

    kind_of(contract_type)은 'instructor' 또는 'employee'를 돌려주는 함수다.
    id를 그대로 유지하므로 계약서 파일명·서명 링크·감사기록 연결이 모두 이어진다.
    원본 테이블은 지우지 않으며, 한 번 끝나면 표시를 남겨 다시 복사하지 않는다
    (복사 후 삭제한 계약이 되살아나지 않게 하기 위함).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM admin_settings WHERE key=?", (SPLIT_MIGRATION_KEY,)
    ).fetchone():
        return {}
    ensure_verified_contract_schema(conn)
    ids_by_kind: dict[str, list[int]] = {kind: [] for kind in CONTRACT_TABLES}
    for row in conn.execute(
        f"SELECT id, contract_type FROM {LEGACY_CONTRACT_TABLE} ORDER BY id"
    ).fetchall():
        kind = kind_of(row[1])
        ids_by_kind[kind if kind in CONTRACT_TABLES else "instructor"].append(int(row[0]))
    legacy_columns = {
        str(r[1]) for r in conn.execute(f"PRAGMA table_info({LEGACY_CONTRACT_TABLE})").fetchall()
    }
    copied: dict[str, int] = {}
    for kind, ids in ids_by_kind.items():
        table, events = CONTRACT_TABLES[kind], EVENT_TABLES[kind]
        columns = [
            str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if str(r[1]) in legacy_columns
        ]
        column_sql = ", ".join(columns)
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(
                f"INSERT OR IGNORE INTO {table} ({column_sql}) "
                f"SELECT {column_sql} FROM {LEGACY_CONTRACT_TABLE} WHERE id IN ({marks})",
                chunk,
            )
            conn.execute(
                f"INSERT INTO {events} "
                f"(contract_id, event_type, event_at, ip_address, user_agent, details_json) "
                f"SELECT contract_id, event_type, event_at, ip_address, user_agent, details_json "
                f"FROM {LEGACY_EVENT_TABLE} WHERE contract_id IN ({marks}) ORDER BY id",
                chunk,
            )
        copied[kind] = len(ids)
    conn.execute(
        "INSERT INTO admin_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (SPLIT_MIGRATION_KEY, json.dumps(copied)),
    )
    return copied


def insert_verified_contract(conn, values: Mapping[str, object]) -> int:
    allowed = {
        key: value
        for key, value in values.items()
        if key in VERIFIED_CONTRACT_FIELDS
    }
    columns = list(allowed)
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    cursor = conn.execute(
        f"INSERT INTO {contract_table()} ({column_sql}) VALUES ({placeholders})",
        [allowed[column] for column in columns],
    )
    return int(cursor.lastrowid)


def update_verified_contract(conn, contract_id: int, values: Mapping[str, object]) -> int:
    allowed = {
        key: value
        for key, value in values.items()
        if key in VERIFIED_CONTRACT_FIELDS
    }
    if not allowed:
        return 0
    assignments = ", ".join(f"{key}=?" for key in allowed)
    cursor = conn.execute(
        f"""
        UPDATE {contract_table()}
        SET {assignments}, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        [*allowed.values(), int(contract_id)],
    )
    return int(cursor.rowcount)


def add_verified_contract_event(
    conn,
    contract_id: int,
    event_type: str,
    event_at: str,
    *,
    ip_address: str = "",
    user_agent: str = "",
    details: Mapping[str, object] | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {event_table()} (
            contract_id, event_type, event_at, ip_address, user_agent, details_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(contract_id),
            str(event_type),
            str(event_at),
            str(ip_address or ""),
            str(user_agent or "")[:500],
            json.dumps(dict(details or {}), ensure_ascii=False),
        ),
    )
