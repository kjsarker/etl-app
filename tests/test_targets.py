import importlib.util
import io
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

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


def test_excel_load_truncate_clears_existing_rows():
    existing_buffer = io.BytesIO()
    pd.DataFrame({"a": [1, 2]}).to_excel(existing_buffer, sheet_name="Data", index=False)

    rows, msg, file_bytes = targets.load_dataframe(
        "excel",
        pd.DataFrame({"a": [9]}),
        {"sheet_name": "Data", "_existing_file_bytes": existing_buffer.getvalue()},
        target_name="Data",
        if_exists="truncate",
    )

    assert rows == 1
    result = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Data")
    assert result["a"].tolist() == [9]


def test_excel_load_replace_discards_existing_rows():
    existing_buffer = io.BytesIO()
    pd.DataFrame({"a": [1, 2]}).to_excel(existing_buffer, sheet_name="Data", index=False)

    rows, msg, file_bytes = targets.load_dataframe(
        "excel",
        pd.DataFrame({"b": [9]}),
        {"sheet_name": "Data", "_existing_file_bytes": existing_buffer.getvalue()},
        target_name="Data",
        if_exists="replace",
    )

    assert rows == 1
    result = pd.read_excel(io.BytesIO(file_bytes), sheet_name="Data")
    assert result["b"].tolist() == [9]
    assert "a" not in result.columns


def test_excel_load_preserves_other_sheets():
    existing_buffer = io.BytesIO()
    with pd.ExcelWriter(existing_buffer, engine="openpyxl") as writer:
        pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="Data", index=False)
        pd.DataFrame({"x": [1]}).to_excel(writer, sheet_name="Other", index=False)

    rows, msg, file_bytes = targets.load_dataframe(
        "excel",
        pd.DataFrame({"a": [2]}),
        {"sheet_name": "Data", "_existing_file_bytes": existing_buffer.getvalue()},
        target_name="Data",
        if_exists="append",
    )

    sheets = pd.read_excel(io.BytesIO(file_bytes), sheet_name=None)
    assert "Other" in sheets
    assert sheets["Other"]["x"].tolist() == [1]


def test_get_excel_existing_sheet_returns_matching_sheet():
    existing_buffer = io.BytesIO()
    pd.DataFrame({"a": [1, 2]}).to_excel(existing_buffer, sheet_name="Data", index=False)

    sheet = targets.get_excel_existing_sheet(
        {"sheet_name": "Data", "_existing_file_bytes": existing_buffer.getvalue()}
    )

    assert sheet is not None
    assert sheet["a"].tolist() == [1, 2]


def test_get_excel_existing_sheet_returns_none_without_upload():
    assert targets.get_excel_existing_sheet({"sheet_name": "Data"}) is None


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


def test_mysql_create_table_ddl_includes_auto_increment_primary_key():
    # Aiven (and other managed MySQL providers) reject CREATE TABLE statements
    # with no primary key when sql_require_primary_key is set, so new tables
    # need an explicit auto-increment id column pandas.to_sql wouldn't add.
    engine = sa.create_engine("mysql+pymysql://user:pass@localhost/db")
    df = pd.DataFrame({"name": ["a"], "email": ["a@b.com"]})

    ddl = targets._mysql_create_table_ddl(engine, "customer", df, None)

    assert "AUTO_INCREMENT PRIMARY KEY" in ddl
    assert "customer" in ddl
    assert "name" in ddl and "email" in ddl


def test_mysql_create_table_ddl_qualifies_with_schema():
    engine = sa.create_engine("mysql+pymysql://user:pass@localhost/db")
    df = pd.DataFrame({"name": ["a"]})

    ddl = targets._mysql_create_table_ddl(engine, "customer", df, "myschema")

    assert "myschema.customer" in ddl


def test_google_sheets_validation_uses_worksheet_name():
    errors = targets.validate_provider_config(
        "googlesheets",
        {"spreadsheet_id": "1AbCdEfG", "worksheet_name": "Sheet1"},
    )
    assert errors == {}
