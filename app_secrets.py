import os


def get_secret(name: str, default: str | None = None) -> str | None:
    """Reads a secret from the environment first (DigitalOcean/production),
    falling back to .streamlit/secrets.toml (local dev)."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st

        return st.secrets.get(name, default)
    except Exception:
        return default
