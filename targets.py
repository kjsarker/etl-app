import json
import re
from typing import Any

import pandas as pd
import pyodbc
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from urllib.parse import quote_plus


PROVIDERS = [
    {"id": "sqlserver", "label": "Microsoft SQL Server"},
    {"id": "azuresql", "label": "Azure SQL"},
    {"id": "postgres", "label": "PostgreSQL"},
    {"id": "mysql", "label": "MySQL"},
    {"id": "databricks", "label": "Databricks SQL Warehouse"},
    {"id": "excel", "label": "Excel File"},
    {"id": "googlesheets", "label": "Google Sheets"},
]


def get_provider_label(provider_id: str) -> str:
    for provider in PROVIDERS:
        if provider["id"] == provider_id:
            return provider["label"]
    return provider_id


def extract_google_spreadsheet_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    url_match = re.search(r"/d/([a-zA-Z0-9_-]+)", value)
    if url_match:
        return url_match.group(1)

    query_match = re.search(r"(?:[?&]key=)([a-zA-Z0-9_-]+)", value)
    if query_match:
        return query_match.group(1)

    return value


def get_google_sheet_headers(config: dict[str, Any]) -> list[str]:
    import gspread
    from google.oauth2 import service_account

    credentials_json = config.get("credentials_json", "").strip()
    if not credentials_json:
        raise RuntimeError("Service-account JSON is required for Google Sheets.")

    info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(credentials)
    spreadsheet_id = extract_google_spreadsheet_id(config.get("spreadsheet_id", ""))
    spreadsheet = client.open_by_key(spreadsheet_id)
    worksheet_name = config.get("worksheet_name", "Sheet1") or "Sheet1"
    worksheet = spreadsheet.worksheet(worksheet_name)
    values = worksheet.get_all_values()
    if not values:
        return []
    return [str(value).strip() for value in values[0]]


def _graph_get_access_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    import requests

    tenant_id = (tenant_id or "").strip()
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not (tenant_id and client_id and client_secret):
        raise RuntimeError("Tenant ID, Client ID, and Client Secret are required for OneDrive/SharePoint.")

    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not authenticate with Azure AD: {resp.text}")
    return resp.json()["access_token"]


def _graph_encode_share_url(share_url: str) -> str:
    import base64

    encoded = base64.urlsafe_b64encode(share_url.strip().encode("utf-8")).decode("utf-8")
    return "u!" + encoded.rstrip("=")


def _graph_resolve_share_link(access_token: str, share_link: str) -> tuple[str, str]:
    import requests

    share_link = (share_link or "").strip()
    if not share_link:
        raise RuntimeError("A OneDrive/SharePoint file link is required.")

    encoded = _graph_encode_share_url(share_link)
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/shares/{encoded}/driveItem",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"$select": "id,parentReference"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not resolve the OneDrive/SharePoint link: {resp.text}")
    item = resp.json()
    return item["parentReference"]["driveId"], item["id"]


def _graph_download_item_bytes(access_token: str, drive_id: str, item_id: str) -> bytes:
    import requests

    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not download the OneDrive/SharePoint file: {resp.text}")
    return resp.content


def _graph_upload_item_bytes(access_token: str, drive_id: str, item_id: str, content: bytes) -> None:
    import requests

    resp = requests.put(
        f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/content",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/octet-stream",
        },
        data=content,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Could not save the file back to OneDrive/SharePoint: {resp.text}")


def _excel_cloud_fetch(config: dict[str, Any]) -> tuple[str, str, str, bytes | None]:
    """Authenticate against Graph and resolve the share link once, returning
    (access_token, drive_id, item_id, existing_file_bytes_or_None) so callers
    can reuse the same drive/item id for the follow-up upload."""
    token = _graph_get_access_token(
        config.get("tenant_id", ""), config.get("client_id", ""), config.get("client_secret", "")
    )
    drive_id, item_id = _graph_resolve_share_link(token, config.get("share_link", ""))
    try:
        existing_bytes = _graph_download_item_bytes(token, drive_id, item_id)
    except Exception:
        existing_bytes = None
    return token, drive_id, item_id, existing_bytes


def get_excel_existing_sheet(config: dict[str, Any]) -> pd.DataFrame | None:
    existing_bytes = config.get("_existing_file_bytes")
    if not existing_bytes and config.get("location") == "OneDrive / SharePoint":
        try:
            _, _, _, existing_bytes = _excel_cloud_fetch(config)
        except Exception:
            return None
    if not existing_bytes:
        return None

    import io

    sheet_name = config.get("sheet_name", "Sheet1") or "Sheet1"
    try:
        sheets = pd.read_excel(io.BytesIO(existing_bytes), sheet_name=None)
    except Exception:
        return None
    sheet = sheets.get(sheet_name)
    if sheet is None:
        return None
    sheet.columns = sheet.columns.astype(str)
    return sheet


def get_table_columns(provider_id: str, config: dict[str, Any], target_name: str, schema: str | None = None) -> list[str]:
    target_name = (target_name or "").strip()
    if not target_name:
        return []

    if provider_id in {"sqlserver", "azuresql", "postgres", "mysql"}:
        try:
            engine = create_engine(_sqlalchemy_url(provider_id, config), pool_pre_ping=True)
            inspector = sa.inspect(engine)
            if not inspector.has_table(target_name, schema=schema or None):
                return []
            return [col["name"] for col in inspector.get_columns(target_name, schema=schema or None)]
        except Exception:
            return []

    if provider_id == "databricks":
        try:
            catalog = config.get("catalog", "").strip() or "main"
            schema_part = (schema or "silver").strip() or "silver"
            full_table = f"{_databricks_ident(catalog)}.{_databricks_ident(schema_part)}.{_databricks_ident(target_name)}"
            conn = _databricks_connect(config)
            with conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"DESCRIBE TABLE {full_table}")
                    rows = cursor.fetchall()
                finally:
                    cursor.close()
            columns = []
            for row in rows:
                col_name = (row[0] or "").strip()
                if not col_name or col_name.startswith("#"):
                    break
                columns.append(col_name)
            return columns
        except Exception:
            return []

    return []


def get_provider_config_schema(provider_id: str) -> list[dict[str, Any]]:
    schemas = {
        "sqlserver": [
            {"name": "server", "label": "Server / Host", "type": "text", "placeholder": "localhost"},
            {"name": "port", "label": "Port", "type": "text", "placeholder": "1433"},
            {"name": "database", "label": "Database", "type": "text", "placeholder": "my_db"},
            {"name": "auth_mode", "label": "Authentication", "type": "select", "options": ["sql", "windows"], "default": "sql"},
            {"name": "username", "label": "Username", "type": "text", "placeholder": "sa"},
            {"name": "password", "label": "Password", "type": "password"},
        ],
        "azuresql": [
            {"name": "server", "label": "Server / Host", "type": "text", "placeholder": "myserver.database.windows.net"},
            {"name": "port", "label": "Port", "type": "text", "placeholder": "1433"},
            {"name": "database", "label": "Database", "type": "text", "placeholder": "my_db"},
            {"name": "auth_mode", "label": "Authentication", "type": "select", "options": ["sql", "aad_password", "service_principal"], "default": "sql"},
            {"name": "username", "label": "Username / Client ID", "type": "text", "placeholder": "user@tenant.com"},
            {"name": "password", "label": "Password / Secret", "type": "password"},
            {"name": "tenant_id", "label": "Tenant ID (optional)", "type": "text", "placeholder": "optional"},
        ],
        "postgres": [
            {"name": "host", "label": "Host", "type": "text", "placeholder": "localhost"},
            {"name": "port", "label": "Port", "type": "text", "placeholder": "5432"},
            {"name": "database", "label": "Database", "type": "text", "placeholder": "postgres"},
            {"name": "schema", "label": "Schema", "type": "text", "placeholder": "public"},
            {"name": "username", "label": "Username", "type": "text", "placeholder": "postgres"},
            {"name": "password", "label": "Password", "type": "password"},
        ],
        "mysql": [
            {"name": "host", "label": "Host", "type": "text", "placeholder": "localhost"},
            {"name": "port", "label": "Port", "type": "text", "placeholder": "3306"},
            {"name": "database", "label": "Database", "type": "text", "placeholder": "my_db"},
            {"name": "schema", "label": "Schema", "type": "text", "placeholder": ""},
            {"name": "username", "label": "Username", "type": "text", "placeholder": "root"},
            {"name": "password", "label": "Password", "type": "password"},
        ],
        "databricks": [
            {"name": "server_hostname", "label": "Server Hostname", "type": "text", "placeholder": "adb-xxxxxxxxxxxx.xx.azuredatabricks.net"},
            {"name": "http_path", "label": "HTTP Path (SQL Warehouse)", "type": "text", "placeholder": "/sql/1.0/warehouses/xxxxxxxxxxxxxxxx"},
            {"name": "catalog", "label": "Catalog", "type": "text", "placeholder": "main"},
            {"name": "auth_mode", "label": "Authentication", "type": "select", "options": ["token", "oauth_m2m"], "default": "token"},
            {"name": "token", "label": "Personal Access Token", "type": "password"},
            {"name": "client_id", "label": "Service Principal Client ID (OAuth)", "type": "text", "placeholder": "for OAuth authentication"},
            {"name": "client_secret", "label": "Service Principal Client Secret (OAuth)", "type": "password"},
        ],
        "excel": [
            {
                "name": "location",
                "label": "File location",
                "type": "select",
                "options": ["Local file", "OneDrive / SharePoint"],
                "default": "Local file",
            },
            {"name": "file_name", "label": "Output file name", "type": "text", "placeholder": "output.xlsx"},
            {"name": "sheet_name", "label": "Sheet name", "type": "text", "placeholder": "Sheet1"},
            {"name": "share_link", "label": "OneDrive/SharePoint file link", "type": "text", "placeholder": "https://yourtenant-my.sharepoint.com/:x:/g/personal/.../EX..."},
            {"name": "tenant_id", "label": "Azure AD Tenant ID", "type": "text", "placeholder": ""},
            {"name": "client_id", "label": "Azure AD App Client ID", "type": "text", "placeholder": ""},
            {"name": "client_secret", "label": "Azure AD App Client Secret", "type": "password"},
        ],
        "googlesheets": [
            {"name": "spreadsheet_id", "label": "Spreadsheet ID or URL", "type": "text", "placeholder": "1AbCd..."},
            {"name": "worksheet_name", "label": "Worksheet name", "type": "text", "placeholder": "Sheet1"},
            {"name": "credentials_json", "label": "Service account JSON (optional)", "type": "textarea", "placeholder": "Paste JSON content here"},
        ],
    }
    return schemas.get(provider_id, [])


def build_connection_string(provider_id: str, config: dict[str, Any]) -> str | None:
    if provider_id == "sqlserver":
        server = config.get("server", "").strip()
        port = config.get("port", "").strip()
        database = config.get("database", "").strip()
        auth_mode = config.get("auth_mode", "sql")
        user = config.get("username", "").strip()
        pwd = config.get("password", "").strip()
        srv = f"{server},{port}" if port else server
        base = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={srv};DATABASE={database};"
        if auth_mode == "windows":
            return base + "Trusted_Connection=yes;"
        return base + f"UID={user};PWD={pwd};"

    if provider_id == "azuresql":
        server = config.get("server", "").strip()
        port = config.get("port", "").strip()
        database = config.get("database", "").strip()
        auth_mode = config.get("auth_mode", "sql")
        user = config.get("username", "").strip()
        pwd = config.get("password", "").strip()
        tenant_id = config.get("tenant_id", "").strip()
        srv = f"{server},{port}" if port else server
        base = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={srv};DATABASE={database};"
        if auth_mode == "aad_password":
            return base + f"UID={user};PWD={pwd};Authentication=ActiveDirectoryPassword;"
        if auth_mode == "service_principal":
            if tenant_id:
                return base + f"UID={user};PWD={pwd};Authentication=ActiveDirectoryServicePrincipal;TenantId={tenant_id};"
            return base + f"UID={user};PWD={pwd};Authentication=ActiveDirectoryServicePrincipal;"
        return base + f"UID={user};PWD={pwd};"

    if provider_id == "postgres":
        host = config.get("host", "").strip()
        port = config.get("port", "").strip() or "5432"
        database = config.get("database", "").strip()
        username = config.get("username", "").strip()
        password = config.get("password", "").strip()
        return f"postgresql+psycopg2://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/{database}"

    if provider_id == "mysql":
        host = config.get("host", "").strip()
        port = config.get("port", "").strip() or "3306"
        database = config.get("database", "").strip()
        username = config.get("username", "").strip()
        password = config.get("password", "").strip()
        return f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/{database}"

    if provider_id == "excel":
        return config.get("file_name", "").strip()

    if provider_id == "googlesheets":
        return config.get("spreadsheet_id", "").strip()

    return None


def _sqlalchemy_url(provider_id: str, config: dict[str, Any]) -> str:
    """SQLAlchemy needs its own URL scheme, not the raw ODBC connection string
    pyodbc expects, so sqlserver/azuresql get wrapped via the odbc_connect param."""
    if provider_id in {"sqlserver", "azuresql"}:
        odbc_str = build_connection_string(provider_id, config)
        return f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc_str)}"
    return build_connection_string(provider_id, config)


def validate_provider_config(provider_id: str, config: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}

    if provider_id in {"sqlserver", "azuresql"}:
        if not config.get("server", "").strip():
            errors["server"] = "Server is required."
        if not config.get("database", "").strip():
            errors["database"] = "Database is required."
        auth_mode = config.get("auth_mode", "sql")
        if auth_mode in {"sql", "aad_password", "service_principal"}:
            if not config.get("username", "").strip():
                errors["username"] = "Username is required."
            if not config.get("password", "").strip():
                errors["password"] = "Password is required."
        return errors

    if provider_id == "postgres":
        if not config.get("host", "").strip():
            errors["host"] = "Host is required."
        if not config.get("database", "").strip():
            errors["database"] = "Database is required."
        if not config.get("username", "").strip():
            errors["username"] = "Username is required."
        if not config.get("password", "").strip():
            errors["password"] = "Password is required."
        return errors

    if provider_id == "mysql":
        if not config.get("host", "").strip():
            errors["host"] = "Host is required."
        if not config.get("database", "").strip():
            errors["database"] = "Database is required."
        if not config.get("username", "").strip():
            errors["username"] = "Username is required."
        if not config.get("password", "").strip():
            errors["password"] = "Password is required."
        return errors

    if provider_id == "databricks":
        if not config.get("server_hostname", "").strip():
            errors["server_hostname"] = "Server hostname is required."
        if not config.get("http_path", "").strip():
            errors["http_path"] = "HTTP Path is required."
        auth_mode = config.get("auth_mode", "token")
        if auth_mode == "oauth_m2m":
            if not config.get("client_id", "").strip():
                errors["client_id"] = "Client ID is required for OAuth."
            if not config.get("client_secret", "").strip():
                errors["client_secret"] = "Client secret is required for OAuth."
        else:
            if not config.get("token", "").strip():
                errors["token"] = "Personal access token is required."
        return errors

    if provider_id == "excel":
        if config.get("location") == "OneDrive / SharePoint":
            if not config.get("tenant_id", "").strip():
                errors["tenant_id"] = "Azure AD Tenant ID is required."
            if not config.get("client_id", "").strip():
                errors["client_id"] = "Azure AD App Client ID is required."
            if not config.get("client_secret", "").strip():
                errors["client_secret"] = "Azure AD App Client Secret is required."
            if not config.get("share_link", "").strip():
                errors["share_link"] = "OneDrive/SharePoint file link is required."
        return errors

    if provider_id == "googlesheets":
        if not config.get("spreadsheet_id", "").strip():
            errors["spreadsheet_id"] = "Spreadsheet ID or URL is required."
        if not config.get("worksheet_name", "").strip():
            errors["worksheet_name"] = "Worksheet name is required."
        return errors

    return errors


def _databricks_ident(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def _databricks_connect(config: dict[str, Any]):
    from databricks import sql as databricks_sql

    server_hostname = config.get("server_hostname", "").strip()
    http_path = config.get("http_path", "").strip()
    auth_mode = config.get("auth_mode", "token")

    if auth_mode == "oauth_m2m":
        from databricks.sdk.core import Config as DatabricksConfig
        from databricks.sdk.core import oauth_service_principal

        client_id = config.get("client_id", "").strip()
        client_secret = config.get("client_secret", "").strip()

        def credentials_provider():
            cfg = DatabricksConfig(
                host=f"https://{server_hostname}",
                client_id=client_id,
                client_secret=client_secret,
            )
            return oauth_service_principal(cfg)

        return databricks_sql.connect(
            server_hostname=server_hostname,
            http_path=http_path,
            credentials_provider=credentials_provider,
        )

    return databricks_sql.connect(
        server_hostname=server_hostname,
        http_path=http_path,
        access_token=config.get("token", "").strip(),
    )


def _databricks_col_type(dtype) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    return "STRING"


def _databricks_sql_literal(value) -> str:
    if pd.isna(value):
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if hasattr(value, "isoformat"):
        return f"TIMESTAMP'{value}'"
    return "'" + str(value).replace("'", "''") + "'"


def _mysql_col_type(dtype) -> str:
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "DATETIME"
    return "TEXT"


def _mysql_create_table_ddl(engine: Engine, target_name: str, dataframe: pd.DataFrame, schema: str | None) -> str:
    preparer = engine.dialect.identifier_preparer
    full_name = f"{preparer.quote(schema)}.{preparer.quote(target_name)}" if schema else preparer.quote(target_name)
    col_defs = ", ".join(
        f"{preparer.quote(str(col))} {_mysql_col_type(dataframe[col].dtype)}" for col in dataframe.columns
    )

    pk_name = "_pk_id"
    existing_lower = {str(col).lower() for col in dataframe.columns}
    while pk_name.lower() in existing_lower:
        pk_name = "_" + pk_name
    pk_col = preparer.quote(pk_name)

    return f"CREATE TABLE {full_name} ({pk_col} BIGINT AUTO_INCREMENT PRIMARY KEY, {col_defs})"


def _create_mysql_table_with_pk(engine: Engine, target_name: str, dataframe: pd.DataFrame, schema: str | None) -> None:
    ddl = _mysql_create_table_ddl(engine, target_name, dataframe, schema)
    with engine.begin() as conn:
        conn.execute(sa.text(ddl))


def test_connection(provider_id: str, config: dict[str, Any]) -> tuple[bool, str]:
    errors = validate_provider_config(provider_id, config)
    if errors:
        return False, "; ".join(errors.values())

    if provider_id in {"sqlserver", "azuresql"}:
        try:
            cs = build_connection_string(provider_id, config)
            with pyodbc.connect(cs, timeout=10) as conn:
                conn.cursor().execute("SELECT 1")
            return True, "Connection successful"
        except Exception as exc:
            return False, str(exc)

    if provider_id in {"postgres", "mysql"}:
        try:
            engine = create_engine(_sqlalchemy_url(provider_id, config), pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            return True, "Connection successful"
        except Exception as exc:
            return False, str(exc)

    if provider_id == "databricks":
        try:
            conn = _databricks_connect(config)
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.close()
            return True, "Databricks connection successful"
        except Exception as exc:
            return False, str(exc)

    if provider_id == "excel":
        if config.get("location") == "OneDrive / SharePoint":
            try:
                _excel_cloud_fetch(config)
                return True, "Connected to the OneDrive/SharePoint file successfully."
            except Exception as exc:
                return False, str(exc)
        try:
            import io

            buffer = io.BytesIO()
            pd.DataFrame({"test": [1]}).to_excel(buffer, index=False)
            return True, "Excel export is ready — the file downloads to your browser after Load Data."
        except Exception as exc:
            return False, str(exc)

    if provider_id == "googlesheets":
        try:
            import gspread
            from google.oauth2 import service_account

            credentials_json = config.get("credentials_json", "").strip()
            if credentials_json:
                info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
            else:
                credentials = None

            if credentials is None:
                return False, "Provide service-account JSON for Google Sheets authentication."

            client = gspread.authorize(credentials)
            spreadsheet_id = extract_google_spreadsheet_id(config.get("spreadsheet_id", ""))
            client.open_by_key(spreadsheet_id)
            return True, "Google Sheets connection successful"
        except Exception as exc:
            return False, str(exc)

    return False, "Unsupported provider."


def load_dataframe(
    provider_id: str,
    dataframe: pd.DataFrame,
    config: dict[str, Any],
    target_name: str,
    if_exists: str,
    schema: str | None = None,
    column_mapping: dict[str, str] | None = None,
    write_mode: str = "append",
) -> tuple[int, str, bytes | None]:
    if provider_id in {"sqlserver", "azuresql", "postgres", "mysql"}:
        try:
            engine = create_engine(_sqlalchemy_url(provider_id, config), pool_pre_ping=True)
            dataframe = dataframe.copy()
            dataframe.columns = dataframe.columns.astype(str)
            if column_mapping:
                dataframe = dataframe.rename(columns={k: v for k, v in column_mapping.items() if v})

            effective_if_exists = if_exists
            if if_exists == "truncate":
                inspector = sa.inspect(engine)
                if inspector.has_table(target_name, schema=schema or None):
                    preparer = engine.dialect.identifier_preparer
                    full_name = (
                        f"{preparer.quote(schema)}.{preparer.quote(target_name)}"
                        if schema
                        else preparer.quote(target_name)
                    )
                    with engine.begin() as conn:
                        conn.execute(sa.text(f"TRUNCATE TABLE {full_name}"))
                effective_if_exists = "append"

            if provider_id == "mysql":
                # Managed MySQL (e.g. Aiven) commonly enforces sql_require_primary_key,
                # which rejects the plain CREATE TABLE pandas.to_sql would otherwise issue.
                inspector = sa.inspect(engine)
                table_exists = inspector.has_table(target_name, schema=schema or None)
                if effective_if_exists == "replace" and table_exists:
                    preparer = engine.dialect.identifier_preparer
                    full_name = (
                        f"{preparer.quote(schema)}.{preparer.quote(target_name)}"
                        if schema
                        else preparer.quote(target_name)
                    )
                    with engine.begin() as conn:
                        conn.execute(sa.text(f"DROP TABLE {full_name}"))
                    table_exists = False
                if not table_exists:
                    _create_mysql_table_with_pk(engine, target_name, dataframe, schema)
                effective_if_exists = "append"

            dataframe.to_sql(
                name=target_name,
                con=engine,
                schema=schema or None,
                if_exists=effective_if_exists,
                index=False,
                chunksize=1000,
                method="multi",
            )
            return len(dataframe), "Load complete", None
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    if provider_id == "databricks":
        try:
            catalog = config.get("catalog", "").strip() or "main"
            schema_part = (schema or "silver").strip() or "silver"
            table = target_name.strip()
            quoted_schema = f"{_databricks_ident(catalog)}.{_databricks_ident(schema_part)}"
            full_table = f"{quoted_schema}.{_databricks_ident(table)}"

            dataframe = dataframe.copy()
            dataframe.columns = dataframe.columns.astype(str)
            if column_mapping:
                dataframe = dataframe.rename(columns={k: v for k, v in column_mapping.items() if v})

            conn = _databricks_connect(config)
            with conn:
                cursor = conn.cursor()
                try:
                    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")

                    table_like = table.replace("'", "''")
                    if if_exists == "replace":
                        cursor.execute(f"DROP TABLE IF EXISTS {full_table}")
                    elif if_exists == "fail":
                        cursor.execute(f"SHOW TABLES IN {quoted_schema} LIKE '{table_like}'")
                        if cursor.fetchall():
                            raise RuntimeError(f"Table {full_table} already exists.")
                    elif if_exists == "truncate":
                        cursor.execute(f"SHOW TABLES IN {quoted_schema} LIKE '{table_like}'")
                        if cursor.fetchall():
                            cursor.execute(f"TRUNCATE TABLE {full_table}")

                    col_defs = ", ".join(
                        f"{_databricks_ident(col)} {_databricks_col_type(dataframe[col].dtype)}" for col in dataframe.columns
                    )
                    cursor.execute(f"CREATE TABLE IF NOT EXISTS {full_table} ({col_defs}) USING DELTA")

                    columns_sql = ", ".join(_databricks_ident(c) for c in dataframe.columns)
                    chunk_size = 500
                    rows = dataframe.values.tolist()
                    for i in range(0, len(rows), chunk_size):
                        chunk = rows[i : i + chunk_size]
                        values_sql = ", ".join(
                            "(" + ", ".join(_databricks_sql_literal(v) for v in row) + ")" for row in chunk
                        )
                        cursor.execute(f"INSERT INTO {full_table} ({columns_sql}) VALUES {values_sql}")
                finally:
                    cursor.close()
            return len(dataframe), f"Loaded into {catalog}.{schema_part}.{table} (Delta)", None
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    if provider_id == "excel":
        try:
            import io

            is_cloud = config.get("location") == "OneDrive / SharePoint"
            sheet_name = config.get("sheet_name", "Sheet1") or "Sheet1"
            dataframe = dataframe.copy()
            dataframe.columns = dataframe.columns.astype(str)
            if column_mapping:
                dataframe = dataframe.rename(columns={k: v for k, v in column_mapping.items() if v})
            new_row_count = len(dataframe)

            other_sheets: dict[str, pd.DataFrame] = {}
            existing_target = None
            token = drive_id = item_id = None
            if is_cloud:
                token, drive_id, item_id, existing_bytes = _excel_cloud_fetch(config)
            else:
                existing_bytes = config.get("_existing_file_bytes")

            if existing_bytes:
                try:
                    existing_sheets = pd.read_excel(io.BytesIO(existing_bytes), sheet_name=None)
                except Exception as exc:
                    raise RuntimeError(f"Could not read the existing Excel file: {exc}") from exc
                existing_target = existing_sheets.pop(sheet_name, None)
                other_sheets = existing_sheets
                if existing_target is not None:
                    existing_target.columns = existing_target.columns.astype(str)

            has_existing_rows = existing_target is not None and len(existing_target) > 0

            if if_exists == "append" and has_existing_rows:
                combined = pd.concat([existing_target, dataframe], ignore_index=True)
                base_msg = f"Added {new_row_count} rows to the '{sheet_name}' sheet ({len(combined)} total)"
            elif has_existing_rows:
                combined = dataframe
                verb = "Cleared" if if_exists == "truncate" else "Replaced"
                base_msg = f"{verb} the existing rows in '{sheet_name}' and loaded {new_row_count} new rows"
            else:
                combined = dataframe
                base_msg = "Excel file generated"

            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                for name, sheet_df in other_sheets.items():
                    sheet_df.to_excel(writer, sheet_name=name, index=False)
                combined.to_excel(writer, sheet_name=sheet_name, index=False)
            file_content = buffer.getvalue()

            if is_cloud:
                _graph_upload_item_bytes(token, drive_id, item_id, file_content)
                return new_row_count, f"{base_msg} — saved back to the OneDrive/SharePoint file.", None
            return new_row_count, f"{base_msg} — download the file below.", file_content
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    if provider_id == "googlesheets":
        try:
            import gspread
            from google.oauth2 import service_account
            from gspread.exceptions import WorksheetNotFound

            credentials_json = config.get("credentials_json", "").strip()
            if not credentials_json:
                raise RuntimeError("Service-account JSON is required for Google Sheets.")

            info = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            client = gspread.authorize(credentials)
            spreadsheet_id = extract_google_spreadsheet_id(config.get("spreadsheet_id", ""))
            spreadsheet = client.open_by_key(spreadsheet_id)
            worksheet_name = config.get("worksheet_name", "Sheet1") or "Sheet1"
            try:
                worksheet = spreadsheet.worksheet(worksheet_name)
            except WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title=worksheet_name,
                    rows=max(1000, len(dataframe) + 1),
                    cols=max(1, len(dataframe.columns)),
                )

            work_df = dataframe.copy()
            if column_mapping:
                work_df = work_df.rename(columns={k: v for k, v in column_mapping.items() if v})

            current_values = worksheet.get_all_values()
            if current_values and write_mode == "append":
                existing_headers = [str(value).strip() for value in current_values[0]]
                aligned_df = pd.DataFrame(index=work_df.index, columns=existing_headers)
                for column in work_df.columns:
                    if column in existing_headers:
                        aligned_df[column] = work_df[column]
                work_df = aligned_df
                values = work_df.where(pd.notnull(work_df), "").astype(str).values.tolist()
                worksheet.append_rows(values, value_input_option="RAW")
                return len(dataframe), "Google Sheets updated", None

            headers = work_df.columns.astype(str).tolist()
            values = work_df.where(pd.notnull(work_df), "").astype(str).values.tolist()
            if write_mode == "replace":
                worksheet.clear()
                worksheet.update([headers] + values, value_input_option="RAW")
            else:
                if not current_values:
                    worksheet.update([headers] + values, value_input_option="RAW")
                else:
                    worksheet.append_rows(values, value_input_option="RAW")
            return len(dataframe), "Google Sheets updated", None
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    raise RuntimeError("Unsupported provider")
