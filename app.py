import io
import json
import re
import warnings
from datetime import datetime

import chardet
import pandas as pd
import streamlit as st

import auth
import vault
from targets import (
    PROVIDERS,
    get_excel_existing_sheet,
    get_google_sheet_headers,
    get_provider_config_schema,
    get_provider_label,
    get_table_columns,
    load_dataframe,
    test_connection,
    validate_provider_config,
)

SQL_PROVIDERS = {"sqlserver", "azuresql", "postgres", "mysql", "databricks"}

warnings.filterwarnings("ignore")

st.set_page_config(page_title="One Minute Loader", page_icon="⚙️", layout="wide")


def detect_meta(raw: bytes, name: str) -> dict:
    enc = chardet.detect(raw)
    encoding = enc.get("encoding") or "utf-8"
    info = {
        "format": None,
        "encoding": encoding,
        "encoding_confidence": f"{enc.get('confidence', 0):.0%}",
        "delimiter": None,
        "has_header_detected": None,
        "error": None,
    }
    n = name.lower()
    if n.endswith((".csv", ".tsv", ".txt", ".dat")):
        sample = raw[:8192].decode(encoding, errors="replace")
        try:
            import csv

            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters=",\t|;")
            info["delimiter"] = dialect.delimiter
        except Exception:
            info["delimiter"] = ","
        try:
            info["has_header_detected"] = csv.Sniffer().has_header(sample)
        except Exception:
            info["has_header_detected"] = True
        info["format"] = "CSV / Delimited Text"
    elif n.endswith((".xlsx", ".xls", ".xlsm")):
        info["format"] = "Excel"
        info["encoding"] = "UTF-8 (Excel internal)"
        info["has_header_detected"] = True
    elif n.endswith(".json"):
        info["format"] = "JSON"
        info["has_header_detected"] = True
    elif n.endswith(".parquet"):
        info["format"] = "Parquet"
        info["encoding"] = "Binary"
        info["has_header_detected"] = True
    else:
        info["error"] = "Unsupported file type. Supported: CSV, TSV, TXT, Excel, JSON, Parquet."
    return info


def build_df(raw: bytes, name: str, meta: dict, has_header: bool, col_names: list | None = None) -> pd.DataFrame:
    enc = meta["encoding"]
    delim = meta.get("delimiter") or ","
    n = name.lower()

    if n.endswith((".csv", ".tsv", ".txt", ".dat")):
        df = pd.read_csv(
            io.BytesIO(raw),
            encoding=enc,
            sep=delim,
            header=0 if has_header else None,
            on_bad_lines="skip",
        )
    elif n.endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(io.BytesIO(raw), header=0 if has_header else None)
    elif n.endswith(".json"):
        data = json.loads(raw.decode(enc, errors="replace"))
        if isinstance(data, list):
            df = pd.DataFrame(data)
        elif isinstance(data, dict):
            df = None
            for v in data.values():
                if isinstance(v, list):
                    df = pd.DataFrame(v)
                    break
            if df is None:
                df = pd.json_normalize(data)
        else:
            raise ValueError("JSON root must be an array or object.")
        return df
    elif n.endswith(".parquet"):
        return pd.read_parquet(io.BytesIO(raw))
    else:
        raise ValueError("Unsupported file type.")

    df.columns = df.columns.astype(str)
    if not has_header and col_names and len(col_names) == len(df.columns):
        df.columns = col_names
    return df


def col_summary(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Column": str(c),
                "Type": str(df[c].dtype),
                "Nulls": int(df[c].isna().sum()),
                "Sample Values": ", ".join(str(v) for v in df[c].dropna().head(3)),
            }
            for c in df.columns
        ]
    )


def extract_date_from_filename(filename: str) -> str | None:
    stem = filename.rsplit(".", 1)[0]
    patterns = [
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{4}/\d{2}/\d{2}", "%Y/%m/%d"),
        (r"\d{2}/\d{2}/\d{4}", "%m/%d/%Y"),
        (r"\d{2}-\d{2}-\d{4}", "%m-%d-%Y"),
        (r"\d{8}", "%Y%m%d"),
    ]
    for pattern, fmt in patterns:
        match = re.search(pattern, stem)
        if match:
            try:
                return datetime.strptime(match.group(0), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


_defaults = {
    "file_raw": None,
    "file_name": None,
    "file_meta": None,
    "file_df": None,
    "custom_col_names": None,
    "db_ok": False,
    "provider_id": "sqlserver",
    "show_preview": False,
    "auth_session": None,
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


if st.session_state.auth_session is None:
    _, login_col, _ = st.columns([2, 1, 2])
    with login_col:
        st.title("⚙️ One Minute Loader")
        st.caption("Sign in to continue")

        tab_signin, tab_signup = st.tabs(["Sign In", "Create Account"])

        with tab_signin:
            with st.form("signin_form"):
                signin_email = st.text_input("Email", key="_signin_email")
                signin_password = st.text_input("Password", type="password", key="_signin_password")
                signin_submitted = st.form_submit_button(
                    "Sign In", type="primary", use_container_width=True
                )
            if signin_submitted:
                try:
                    result = auth.sign_in(signin_email, signin_password)
                    st.session_state.auth_session = {
                        "access_token": result.session.access_token,
                        "refresh_token": result.session.refresh_token,
                        "user_id": result.user.id,
                        "email": result.user.email,
                    }
                    st.rerun()
                except Exception as exc:
                    st.error(f"Sign in failed: {exc}")

        with tab_signup:
            with st.form("signup_form"):
                signup_email = st.text_input("Email", key="_signup_email")
                signup_password = st.text_input("Password", type="password", key="_signup_password")
                signup_password_confirm = st.text_input(
                    "Confirm Password", type="password", key="_signup_password_confirm"
                )
                signup_submitted = st.form_submit_button("Create Account", use_container_width=True)
            if signup_submitted:
                if signup_password != signup_password_confirm:
                    st.error("Passwords do not match.")
                elif len(signup_password) < 8:
                    st.error("Password must be at least 8 characters.")
                else:
                    try:
                        result = auth.sign_up(signup_email, signup_password)
                        if result.session is not None:
                            st.session_state.auth_session = {
                                "access_token": result.session.access_token,
                                "refresh_token": result.session.refresh_token,
                                "user_id": result.user.id,
                                "email": result.user.email,
                            }
                            st.rerun()
                        else:
                            st.success("Account created. Check your email to confirm it, then sign in.")
                    except Exception as exc:
                        st.error(f"Sign up failed: {exc}")

    st.stop()

with st.sidebar:
    st.caption(f"Signed in as **{st.session_state.auth_session['email']}**")
    if st.button("Log out", use_container_width=True):
        try:
            auth.sign_out(
                st.session_state.auth_session["access_token"],
                st.session_state.auth_session["refresh_token"],
            )
        except Exception:
            pass
        st.session_state.auth_session = None
        st.rerun()

st.title("One Minute Loader")
st.caption("Load files into SQL databases, Excel, or Google Sheets")

left, right = st.columns([1, 1], gap="large")

with left:
    st.subheader("1. Upload File")
    uploaded = st.file_uploader(
        "file",
        type=["csv", "tsv", "txt", "dat", "xlsx", "xls", "xlsm", "json", "parquet"],
        label_visibility="collapsed",
    )

    if uploaded and uploaded.name != st.session_state.file_name:
        raw = uploaded.read()
        meta = detect_meta(raw, uploaded.name)
        st.session_state.file_raw = raw
        st.session_state.file_name = uploaded.name
        st.session_state.file_meta = meta
        st.session_state.custom_col_names = None
        st.session_state.show_preview = False
        st.session_state["_has_header"] = bool(meta.get("has_header_detected", True))
        if not meta["error"]:
            st.session_state.file_df = build_df(raw, uploaded.name, meta, st.session_state["_has_header"])

    meta = st.session_state.file_meta
    if meta and not meta["error"]:
        st.success(f"**{st.session_state.file_name}**")
        st.divider()

        hdr_col, schema_col = st.columns([1, 1])
        has_header = hdr_col.checkbox(
            "File has a header row",
            key="_has_header",
            help="Uncheck if the first row contains data, not column names.",
        )
        custom_col_names = st.session_state.custom_col_names

        if not has_header:
            hf = schema_col.file_uploader(
                "Schema file (optional)",
                type=["csv", "txt"],
                key="_header_file",
                label_visibility="collapsed",
                help="CSV with one row of column names",
            )
            if hf:
                try:
                    header_df = pd.read_csv(io.BytesIO(hf.read()))
                    custom_col_names = list(header_df.columns)
                    st.session_state.custom_col_names = custom_col_names
                    schema_col.caption(f"Columns: {', '.join(custom_col_names)}")
                except Exception as e:
                    schema_col.error(f"Could not read schema file: {e}")
        else:
            if st.session_state.custom_col_names is not None:
                st.session_state.custom_col_names = None
            custom_col_names = None

        if st.session_state.file_raw:
            try:
                df = build_df(
                    st.session_state.file_raw,
                    st.session_state.file_name,
                    meta,
                    has_header,
                    custom_col_names,
                )
                st.session_state.file_df = df
            except Exception as e:
                st.error(f"Error reading file: {e}")
                df = None
        else:
            df = st.session_state.file_df

        df = st.session_state.file_df
        if df is not None:
            st.divider()
            if st.button(
                "Hide Preview" if st.session_state.show_preview else "Preview",
                use_container_width=True,
                key="_preview_btn",
            ):
                st.session_state.show_preview = not st.session_state.show_preview

            if st.session_state.show_preview:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Format", meta["format"])
                m2.metric("Rows", f"{len(df):,}")
                m3.metric("Columns", len(df.columns))
                m4.metric("Encoding", meta["encoding"])

                with st.expander("File Properties", expanded=True):
                    st.dataframe(
                        pd.DataFrame(
                            {
                                "Property": ["Format", "Encoding", "Confidence", "Delimiter", "Has Header"],
                                "Value": [
                                    meta["format"],
                                    meta["encoding"],
                                    meta["encoding_confidence"],
                                    repr(meta["delimiter"]) if meta.get("delimiter") else "N/A",
                                    str(has_header),
                                ],
                            }
                        ),
                        use_container_width=True,
                        hide_index=True,
                    )

                with st.expander("Column Analysis", expanded=True):
                    st.dataframe(col_summary(df), use_container_width=True, hide_index=True)

                with st.expander("Data Preview (first 100 rows)"):
                    st.dataframe(df.head(100), use_container_width=True)
    elif meta and meta["error"]:
        st.error(meta["error"])

with right:
    st.subheader("2. Choose Target")
    provider_id = st.selectbox(
        "Destination",
        [p["id"] for p in PROVIDERS],
        format_func=get_provider_label,
        key="provider_id",
    )
    st.caption("The same uploaded file can now be written to SQL databases, Excel, or Google Sheets.")

    _supa = auth.get_session_client(
        st.session_state.auth_session["access_token"],
        st.session_state.auth_session["refresh_token"],
    )
    _user_id = st.session_state.auth_session["user_id"]

    try:
        _saved_connections = vault.list_credentials(_supa, _user_id)
    except Exception as exc:
        _saved_connections = []
        st.caption(f"Could not load saved connections: {exc}")
    _matching_connections = [c["connection_name"] for c in _saved_connections if c["provider_id"] == provider_id]

    def _load_saved_connection():
        name = st.session_state.get("_load_conn_select")
        if not name or name == "— New —":
            return
        try:
            loaded = vault.load_credential(_supa, _user_id, name)
            if loaded:
                for f in get_provider_config_schema(loaded["provider_id"]):
                    st.session_state[f"target_{loaded['provider_id']}_{f['name']}"] = loaded["config"].get(
                        f["name"], ""
                    )
        except Exception as exc:
            st.error(f"Could not load connection: {exc}")

    st.selectbox(
        "Load a saved connection",
        ["— New —"] + _matching_connections,
        key="_load_conn_select",
        on_change=_load_saved_connection,
    )

    schema = get_provider_config_schema(provider_id)
    config = {}
    for field in schema:
        name = field["name"]
        label = field["label"]
        field_type = field.get("type", "text")
        if field_type == "select":
            options = field.get("options", [])
            default = field.get("default", options[0] if options else "")
            index = options.index(default) if default in options else 0
            value = st.selectbox(label, options, index=index, key=f"target_{provider_id}_{name}")
        elif field_type == "textarea":
            value = st.text_area(label, placeholder=field.get("placeholder", ""), key=f"target_{provider_id}_{name}")
        else:
            is_secret = any(
                token in name.lower() or token in label.lower()
                for token in ("password", "secret", "token")
            )
            value = st.text_input(
                label,
                placeholder=field.get("placeholder", ""),
                type="password" if is_secret else "default",
                key=f"target_{provider_id}_{name}",
            )
        config[name] = value

    if provider_id == "excel":
        existing_excel_file = st.file_uploader(
            "Existing Excel file (optional — appends new rows into it)",
            type=["xlsx"],
            key="_excel_existing_file",
            help="Pick a workbook from your computer to add the loaded rows into its matching sheet, "
            "instead of starting from a blank file. Other sheets in that workbook are kept as-is.",
        )
        if existing_excel_file is not None:
            config["_existing_file_bytes"] = existing_excel_file.read()

    save_col1, save_col2 = st.columns([3, 1])
    save_conn_name = save_col1.text_input(
        "Save current settings as", placeholder="my_prod_db", key="_save_conn_name"
    )
    if save_col2.button("Save", use_container_width=True, key="_save_conn_btn"):
        if not save_conn_name.strip():
            st.error("Enter a name for this connection.")
        else:
            try:
                vault.save_credential(_supa, _user_id, save_conn_name.strip(), provider_id, config)
                st.success(f"Saved '{save_conn_name.strip()}'.")
            except Exception as exc:
                st.error(f"Could not save connection: {exc}")

    st.divider()
    st.subheader("3. Connect & Load")

    target_name = "etl_output"
    schema_name = None
    if_exists = "append"
    write_mode = "append"
    column_mapping = {}

    st.markdown("**Extra Columns (optional)**")
    st.caption("Manually opt in to add extra columns captured from the uploaded filename. Applies to every sink.")
    ec1, ec2 = st.columns(2)
    add_filename = ec1.checkbox("Add filename as column", key="_add_filename")
    filename_col = ec1.text_input(
        "Filename column name", value="source_file", disabled=not add_filename, key="_filename_col"
    )

    add_filedate = ec2.checkbox("Add file date as column", key="_add_filedate")
    _auto_date = extract_date_from_filename(st.session_state.file_name or "") if st.session_state.file_name else None
    if add_filedate and not _auto_date:
        ec2.warning("No date found in filename — enter it manually.")
    file_date_val = ec2.text_input(
        "File date (YYYY-MM-DD)",
        value=_auto_date or "",
        disabled=not add_filedate,
        key=f"_filedate_{st.session_state.file_name}",
    )
    filedate_col = ec2.text_input(
        "File date column name", value="file_date", disabled=not add_filedate, key="_filedate_col"
    )

    extra_cols = []
    if add_filename and filename_col:
        extra_cols.append(filename_col)
    if add_filedate and filedate_col:
        extra_cols.append(filedate_col)

    source_columns = (
        [str(c) for c in st.session_state.file_df.columns] + extra_cols
        if st.session_state.file_df is not None
        else []
    )

    st.divider()

    if provider_id == "googlesheets":
        write_mode = st.radio(
            "Write mode",
            ["replace", "append"],
            format_func=lambda value: "Replace sheet contents" if value == "replace" else "Append rows",
            horizontal=True,
            key="google_write_mode",
        )
        column_mapping = {}
        if st.session_state.file_df is not None:
            try:
                existing_headers = get_google_sheet_headers(config)
                if write_mode == "append":
                    st.caption("Append matches the existing sheet headers automatically. You can override the target header names below if needed.")
                    if existing_headers:
                        st.caption(f"Existing headers: {', '.join(existing_headers)}")
                    else:
                        st.caption("No existing headers found yet; a new header row will be created.")
                else:
                    if existing_headers and existing_headers != source_columns:
                        st.warning(
                            f"Existing headers {existing_headers} differ from the incoming headers {source_columns}. Replace will overwrite the sheet contents."
                        )
            except Exception as exc:
                st.caption(f"Could not read sheet headers: {exc}")
                existing_headers = []

            if write_mode == "append":
                st.caption("Map each source column to an existing sheet header for append mode.")
                if existing_headers:
                    for idx, column in enumerate(source_columns):
                        target_options = existing_headers
                        default_target = str(column) if str(column) in existing_headers else existing_headers[min(idx, len(existing_headers) - 1)]
                        cols_map = st.columns([1, 1])
                        with cols_map[0]:
                            st.text_input(
                                f"Source header for {column}",
                                value=str(column),
                                disabled=True,
                                key=f"google_column_source_{idx}",
                            )
                        with cols_map[1]:
                            target_header = st.selectbox(
                                f"Target header for {column}",
                                target_options,
                                index=target_options.index(default_target) if default_target in target_options else 0,
                                key=f"google_column_map_{idx}",
                            )
                        column_mapping[str(column)] = target_header
                else:
                    st.caption("No existing sheet headers detected yet. Incoming headers will be used.")
                    for idx, column in enumerate(source_columns):
                        cols_map = st.columns([1, 1])
                        with cols_map[0]:
                            st.text_input(
                                f"Source header for {column}",
                                value=str(column),
                                disabled=True,
                                key=f"google_column_source_{idx}",
                            )
                        with cols_map[1]:
                            st.text_input(
                                f"Target header for {column}",
                                value=str(column),
                                disabled=True,
                                key=f"google_column_target_{idx}",
                            )
                        column_mapping[str(column)] = str(column)
        else:
            existing_headers = []
            column_mapping = {}
    else:
        target_name = st.text_input("Target name / table / sheet", placeholder="customers")
        schema_placeholder = "silver" if provider_id == "databricks" else "dbo"
        schema_help = (
            "Unity Catalog schema, e.g. silver for a medallion silver table."
            if provider_id == "databricks"
            else None
        )
        schema_name = st.text_input(
            "Schema (optional for SQL targets)",
            placeholder=schema_placeholder,
            help=schema_help,
        )
        if provider_id == "excel":
            if_exists = st.radio(
                "If the sheet already has data",
                ["append", "truncate", "replace"],
                horizontal=True,
                help="append = add rows below existing data | truncate = clear existing rows, then load | replace = discard the sheet and write only the new rows",
            )
            st.caption(
                "Upload an existing workbook above to apply this into its matching sheet. "
                "Without an upload, this always produces a fresh file to download."
            )
        else:
            if_exists = st.radio(
                "If target already exists",
                ["append", "truncate", "replace", "fail"],
                horizontal=True,
                help="append = add rows | truncate = empty the table, then load | replace = drop & recreate table | fail = raise an error",
            )
        write_mode = None
        column_mapping = {}

        if (
            provider_id in SQL_PROVIDERS
            and if_exists in {"append", "truncate"}
            and target_name.strip()
            and st.session_state.file_df is not None
            and not validate_provider_config(provider_id, config)
        ):
            target_columns = get_table_columns(provider_id, config, target_name, schema=schema_name or None)
            if target_columns:
                st.caption(f"Existing table columns: {', '.join(target_columns)}")
                st.caption("Map each source column to an existing table column.")
                for idx, column in enumerate(source_columns):
                    default_target = str(column) if str(column) in target_columns else target_columns[min(idx, len(target_columns) - 1)]
                    cols_map = st.columns([1, 1])
                    with cols_map[0]:
                        st.text_input(
                            f"Source column for {column}",
                            value=str(column),
                            disabled=True,
                            key=f"sql_column_source_{idx}",
                        )
                    with cols_map[1]:
                        target_col = st.selectbox(
                            f"Target column for {column}",
                            target_columns,
                            index=target_columns.index(default_target),
                            key=f"sql_column_map_{provider_id}_{target_name}_{idx}",
                        )
                    column_mapping[str(column)] = target_col

        if (
            provider_id == "excel"
            and if_exists in {"append", "truncate"}
            and st.session_state.file_df is not None
        ):
            existing_sheet = get_excel_existing_sheet(config)
            if existing_sheet is not None and not existing_sheet.empty:
                target_columns = [str(c) for c in existing_sheet.columns]
                st.caption(f"Existing sheet columns: {', '.join(target_columns)}")
                st.caption("Preview of existing data currently in the sheet:")
                st.dataframe(existing_sheet.tail(5), use_container_width=True)
                st.caption("Map each source column to an existing sheet column (auto-matched by name where possible).")
                for idx, column in enumerate(source_columns):
                    default_target = str(column) if str(column) in target_columns else target_columns[min(idx, len(target_columns) - 1)]
                    cols_map = st.columns([1, 1])
                    with cols_map[0]:
                        st.text_input(
                            f"Source column for {column}",
                            value=str(column),
                            disabled=True,
                            key=f"excel_column_source_{idx}",
                        )
                    with cols_map[1]:
                        target_col = st.selectbox(
                            f"Target column for {column}",
                            target_columns,
                            index=target_columns.index(default_target),
                            key=f"excel_column_map_{idx}",
                        )
                    column_mapping[str(column)] = target_col

    c1, c2 = st.columns(2)
    if c1.button("Test Connection", use_container_width=True):
        if st.session_state.file_df is None:
            st.error("Upload a file first.")
        else:
            errors = validate_provider_config(provider_id, config)
            if errors:
                st.error("; ".join(errors.values()))
            else:
                with st.spinner("Checking target connection…"):
                    ok, msg = test_connection(provider_id, config)
                if ok:
                    st.session_state.db_ok = True
                    st.success(msg)
                else:
                    st.session_state.db_ok = False
                    st.error(msg)

    if c2.button("Load Data →", type="primary", use_container_width=True):
        if st.session_state.file_df is None:
            st.error("Upload a file first.")
        else:
            errors = validate_provider_config(provider_id, config)
            if errors:
                st.error("; ".join(errors.values()))
            else:
                try:
                    src = st.session_state.file_df.copy()
                    src.columns = src.columns.astype(str)
                    if add_filename and filename_col:
                        src[filename_col] = st.session_state.file_name
                    if add_filedate and filedate_col:
                        src[filedate_col] = file_date_val
                    with st.spinner("Writing data to target…"):
                        if provider_id == "googlesheets":
                            rows, msg, file_bytes = load_dataframe(
                                provider_id,
                                src,
                                config,
                                target_name or "etl_output",
                                write_mode or "append",
                                schema=schema_name or None,
                                column_mapping=column_mapping,
                                write_mode=write_mode or "append",
                            )
                        else:
                            rows, msg, file_bytes = load_dataframe(
                                provider_id,
                                src,
                                config,
                                target_name or "etl_output",
                                if_exists,
                                schema=schema_name or None,
                                column_mapping=column_mapping,
                            )
                    st.success(f"Loaded {rows:,} rows. {msg}")
                    if file_bytes is not None:
                        st.download_button(
                            "Download Excel file",
                            data=file_bytes,
                            file_name=(config.get("file_name", "").strip() or "etl_output.xlsx"),
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                        )
                    else:
                        st.balloons()
                except Exception as exc:
                    st.error(f"Load failed: {exc}")

    st.divider()
    st.info(
        "Authentication notes:\n"
        "- SQL Server / Azure SQL: use SQL auth or Azure AD password/service principal where supported.\n"
        "- Databricks: use a Personal Access Token (simplest) or a Service Principal OAuth client ID/secret. "
        "Leave Schema blank to land data in the 'silver' schema automatically.\n"
        "- Google Sheets: paste a service-account JSON key with spreadsheet access."
    )
