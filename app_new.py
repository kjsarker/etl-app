import io
import json
import re
import warnings
from datetime import datetime

import chardet
import pandas as pd
import streamlit as st

from targets import (
    PROVIDERS,
    get_provider_config_schema,
    get_provider_label,
    load_dataframe,
    test_connection,
    validate_provider_config,
)

warnings.filterwarnings("ignore")

st.set_page_config(page_title="ETL File Loader", page_icon="⚙️", layout="wide")


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
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


st.title("ETL File Loader")
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
            value = st.text_input(
                label,
                placeholder=field.get("placeholder", ""),
                type="password" if "password" in label.lower() or name == "password" else "default",
                key=f"target_{provider_id}_{name}",
            )
        config[name] = value

    st.divider()
    st.subheader("3. Connect & Load")
    target_name = st.text_input("Target name / table / sheet", placeholder="customers")
    schema_name = st.text_input("Schema (optional for SQL targets)", placeholder="dbo")
    if_exists = st.radio("If target already exists", ["append", "replace", "fail"], horizontal=True)

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
                    with st.spinner("Writing data to target…"):
                        rows, msg = load_dataframe(provider_id, src, config, target_name or "etl_output", if_exists, schema=schema_name or None)
                    st.success(f"Loaded {rows:,} rows. {msg}")
                    st.balloons()
                except Exception as exc:
                    st.error(f"Load failed: {exc}")

    st.divider()
    st.info(
        "Authentication notes:\n"
        "- SQL Server / Azure SQL: use SQL auth or Azure AD password/service principal where supported.\n"
        "- Google Sheets: paste a service-account JSON key with spreadsheet access."
    )
