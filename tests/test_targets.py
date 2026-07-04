import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "targets.py"

spec = importlib.util.spec_from_file_location("targets", MODULE_PATH)
targets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(targets)


def test_sql_server_conn_str_uses_odbc_driver():
    cs = targets.build_connection_string(
        "sqlserver",
        {
            "server": "localhost",
            "port": "1433",
            "database": "demo",
            "auth_mode": "sql",
            "username": "sa",
            "password": "pass",
        },
    )
    assert "ODBC Driver 17 for SQL Server" in cs
    assert "SERVER=localhost,1433" in cs


def test_postgres_conn_str_uses_postgresql_prefix():
    cs = targets.build_connection_string(
        "postgres",
        {
            "host": "db.example.com",
            "port": "5432",
            "database": "analytics",
            "username": "app",
            "password": "secret",
        },
    )
    assert cs.startswith("postgresql+psycopg2://")
    assert "db.example.com" in cs


def test_excel_target_requires_output_path():
    errors = targets.validate_provider_config(
        "excel",
        {"output_path": ""},
    )
    assert "output_path" in errors


def test_google_sheets_validation_uses_worksheet_name():
    errors = targets.validate_provider_config(
        "googlesheets",
        {"spreadsheet_id": "1AbCdEfG", "worksheet_name": "Sheet1"},
    )
    assert errors == {}
