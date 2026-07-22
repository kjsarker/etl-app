import importlib.util
import io
from pathlib import Path

import pandas as pd

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


def test_excel_target_has_no_required_fields():
    # Excel exports build an in-memory file for browser download rather than
    # writing to a server-side path, so nothing is required up front.
    errors = targets.validate_provider_config("excel", {})
    assert errors == {}


def test_excel_load_merges_uploaded_existing_workbook():
    existing_buffer = io.BytesIO()
    pd.DataFrame({"a": [1, 2]}).to_excel(existing_buffer, sheet_name="Data", index=False)

    rows, msg, file_bytes = targets.load_dataframe(
        "excel",
        pd.DataFrame({"a": [3]}),
        {"sheet_name": "Data", "_existing_file_bytes": existing_buffer.getvalue()},
        target_name="Data",
        if_exists="append",
    )

    assert rows == 1
    assert file_bytes is not None

    result = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Data")
    assert result["a"].tolist() == [1, 2, 3]


def test_excel_load_without_existing_file_returns_fresh_bytes():
    rows, msg, file_bytes = targets.load_dataframe(
        "excel",
        pd.DataFrame({"a": [1]}),
        {"sheet_name": "Data"},
        target_name="Data",
        if_exists="append",
    )

    assert rows == 1
    assert file_bytes is not None


def test_google_sheets_validation_uses_worksheet_name():
    errors = targets.validate_provider_config(
        "googlesheets",
        {"spreadsheet_id": "1AbCdEfG", "worksheet_name": "Sheet1"},
    )
    assert errors == {}
