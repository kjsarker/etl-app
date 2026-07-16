from supabase import Client, create_client

from app_secrets import get_secret


def get_anon_client() -> Client:
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY are not configured.")
    return create_client(url, key)


def get_session_client(access_token: str, refresh_token: str) -> Client:
    """A client scoped to a signed-in user's session, so Supabase's Row Level
    Security policies (auth.uid() = user_id) apply to every query it makes."""
    client = get_anon_client()
    client.auth.set_session(access_token, refresh_token)
    return client


def sign_up(email: str, password: str):
    client = get_anon_client()
    return client.auth.sign_up({"email": email, "password": password})


def sign_in(email: str, password: str):
    client = get_anon_client()
    return client.auth.sign_in_with_password({"email": email, "password": password})


def sign_out(access_token: str, refresh_token: str) -> None:
    client = get_session_client(access_token, refresh_token)
    client.auth.sign_out()
