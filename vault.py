import json
from typing import Any

from supabase import Client

from crypto import decrypt_text, encrypt_text

TABLE = "credentials_vault"


def save_credential(
    client: Client, user_id: str, connection_name: str, provider_id: str, config: dict[str, Any]
) -> None:
    encrypted = encrypt_text(json.dumps(config))
    client.table(TABLE).upsert(
        {
            "user_id": user_id,
            "connection_name": connection_name,
            "provider_id": provider_id,
            "encrypted_config": encrypted,
        },
        on_conflict="user_id,connection_name",
    ).execute()


def list_credentials(client: Client, user_id: str) -> list[dict[str, Any]]:
    resp = (
        client.table(TABLE)
        .select("connection_name,provider_id,updated_at")
        .eq("user_id", user_id)
        .order("connection_name")
        .execute()
    )
    return resp.data or []


def load_credential(client: Client, user_id: str, connection_name: str) -> dict[str, Any] | None:
    resp = (
        client.table(TABLE)
        .select("provider_id,encrypted_config")
        .eq("user_id", user_id)
        .eq("connection_name", connection_name)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return None
    row = rows[0]
    return {"provider_id": row["provider_id"], "config": json.loads(decrypt_text(row["encrypted_config"]))}


def delete_credential(client: Client, user_id: str, connection_name: str) -> None:
    client.table(TABLE).delete().eq("user_id", user_id).eq("connection_name", connection_name).execute()
