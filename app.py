import streamlit as st
import pandas as pd
import chardet
import csv
import json
import io
import re
import pyodbc
import urllib
from datetime import datetime
from sqlalchemy import create_engine
import warnings

warnings.filterwarnings("ignore")

st.set_page_config(page_title="ETL File Loader", page_icon="⚙️", layout="wide")


# ── File Parsing ────────────────────────────────────────────────────────────────

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
        sniffer = csv.Sniffer()
        try:
            dialect = sniffer.sniff(sample, delimiters=",\t|;")
            info["delimiter"] = dialect.delimiter
        except Exception:
            info["delimiter"] = ","
        try:
            info["has_header_detected"] = sniffer.has_header(sample)
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

    # Always normalize column labels to strings (pandas uses int labels when header=None)
    df.columns = df.columns.astype(str)

    if not has_header and col_names and len(col_names) == len(df.columns):
        df.columns = col_names

    return df


def col_summary(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "Column": str(c),
            "Type": str(df[c].dtype),
            "Nulls": int(df[c].isna().sum()),
            "Sample Values": ", ".join(str(v) for v in df[c].dropna().head(3)),
        }
        for c in df.columns
    ])


def extract_date_from_filename(filename: str):
    """
    Search the filename for any recognised date pattern and return a date object.
    Tries every format listed, most-specific first, so longer/unambiguous patterns
    win over shorter ones. Returns None if nothing matches.
    """
    stem = filename.rsplit(".", 1)[0]

    S = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"          # short month
    L = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"  # long month

    # (regex, strptime_format)  — ordered most-specific → least-specific
    PATTERNS = [
        # ISO with T
        (r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",              "%Y-%m-%dT%H:%M:%S"),
        # datetime with space
        (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",              "%Y-%m-%d %H:%M:%S"),
        (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}",                    "%Y-%m-%d %H:%M"),
        (r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2}",              "%m-%d-%Y %H:%M:%S"),
        (r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}",                    "%m-%d-%Y %H:%M"),
        (r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}",              "%m/%d/%Y %H:%M:%S"),
        (r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}",                    "%m/%d/%Y %H:%M"),
        (r"\d{2}/\d{2}/\d{4} \d{1,2}:\d{2} (?:AM|PM)",        "%m/%d/%Y %I:%M %p"),
        (r"\d{2}-\d{2}-\d{4} \d{1,2}:\d{2} (?:AM|PM)",        "%m-%d-%Y %I:%M %p"),
        # date only — 4-digit year
        (r"\d{4}-\d{2}-\d{2}",                                 "%Y-%m-%d"),
        (r"\d{4}/\d{2}/\d{2}",                                 "%Y/%m/%d"),
        (r"\d{4}\.\d{2}\.\d{2}",                               "%Y.%m.%d"),
        (r"\d{2}-\d{2}-\d{4}",                                 "%m-%d-%Y"),
        (r"\d{2}/\d{2}/\d{4}",                                 "%m/%d/%Y"),
        (r"\d{2}\.\d{2}\.\d{4}",                               "%m.%d.%Y"),
        # date only — 2-digit year
        (r"\d{2}-\d{2}-\d{2}",                                 "%m-%d-%y"),
        (r"\d{2}/\d{2}/\d{2}",                                 "%m/%d/%y"),
        (r"\d{2}\.\d{2}\.\d{2}",                               "%m.%d.%y"),
        # long month name
        (L + r" \d{1,2}, \d{4}",                               "%B %d, %Y"),
        (L + r" \d{1,2} \d{4}",                                "%B %d %Y"),
        (r"\d{1,2} " + L + r" \d{4}",                         "%d %B %Y"),
        (r"\d{4}-" + L + r"-\d{2}",                            "%Y-%B-%d"),
        # short month name
        (S + r" \d{1,2}, \d{4}",                               "%b %d, %Y"),
        (S + r" \d{1,2} \d{4}",                                "%b %d %Y"),
        (r"\d{1,2} " + S + r" \d{4}",                         "%d %b %Y"),
        (r"\d{2}-" + S + r"-\d{4}",                            "%d-%b-%Y"),
        (r"\d{2}-" + S + r"-\d{2}",                            "%d-%b-%y"),
        (r"\d{4}-" + S + r"-\d{2}",                            "%Y-%b-%d"),
    ]

    for pattern, fmt in PATTERNS:
        m = re.search(pattern, stem, re.IGNORECASE)
        if m:
            raw = m.group(0)
            # Try as-is, then title-cased (handles JAN → Jan for %b/%B)
            for attempt in (raw, raw.title(), raw.upper()):
                try:
                    return datetime.strptime(attempt, fmt).date()
                except ValueError:
                    continue

    # 8-digit compact: try yyyyMMdd then MMddyyyy
    m = re.search(r"\b\d{8}\b", stem)
    if m:
        raw = m.group(0)
        for fmt in ("%Y%m%d", "%m%d%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue

    # 6-digit compact: MMddyy
    m = re.search(r"\b\d{6}\b", stem)
    if m:
        try:
            return datetime.strptime(m.group(0), "%m%d%y").date()
        except ValueError:
            pass

    return None


# ── Database ────────────────────────────────────────────────────────────────────

def make_conn_str(server, port, database, auth, user, pwd):
    srv = f"{server},{port}" if port and port.strip() else server
    base = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={srv};DATABASE={database};"
    if auth == "Windows Authentication":
        return base + "Trusted_Connection=yes;"
    return base + f"UID={user};PWD={pwd};"


def test_conn(cs):
    try:
        pyodbc.connect(cs, timeout=10).close()
        return True, "Connection successful"
    except Exception as e:
        return False, str(e)


def get_schemas(cs):
    try:
        with pyodbc.connect(cs, timeout=10) as c:
            rows = c.cursor().execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA ORDER BY SCHEMA_NAME"
            ).fetchall()
        return [r[0] for r in rows] or ["dbo"]
    except Exception:
        return ["dbo"]


def table_exists(cs, schema, table):
    try:
        with pyodbc.connect(cs, timeout=10) as c:
            n = c.cursor().execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA=? AND TABLE_NAME=?",
                schema, table,
            ).fetchone()[0]
        return n > 0
    except Exception:
        return False


def get_table_cols(cs, schema, table):
    try:
        with pyodbc.connect(cs, timeout=10) as c:
            rows = c.cursor().execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=? AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",
                schema, table,
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def make_engine(cs):
    params = urllib.parse.quote_plus(cs)
    return create_engine(f"mssql+pyodbc:///?odbc_connect={params}", fast_executemany=True)


# ── Session State ───────────────────────────────────────────────────────────────

_defaults = {
    "file_raw": None,
    "file_name": None,
    "file_meta": None,
    "file_df": None,
    "custom_col_names": None,
    "db_cs": None,
    "db_ok": False,
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ── UI ──────────────────────────────────────────────────────────────────────────

st.title("ETL File Loader")
st.caption("Upload a file → configure → load to SQL Server")

left, right = st.columns([1, 1], gap="large")


# ═══════════════════════ LEFT: FILE ════════════════════════════════════════════
with left:
    st.subheader("1. Upload File")

    uploaded = st.file_uploader(
        "file",
        type=["csv", "tsv", "txt", "dat", "xlsx", "xls", "xlsm", "json", "parquet"],
        label_visibility="collapsed",
    )

    # New file → detect metadata
    if uploaded and uploaded.name != st.session_state.file_name:
        raw = uploaded.read()
        meta = detect_meta(raw, uploaded.name)
        st.session_state.file_raw = raw
        st.session_state.file_name = uploaded.name
        st.session_state.file_meta = meta
        st.session_state.custom_col_names = None
        # Seed checkbox to auto-detected value
        st.session_state["_has_header"] = bool(meta.get("has_header_detected", True))
        if not meta["error"]:
            st.session_state.file_df = build_df(
                raw, uploaded.name, meta, st.session_state["_has_header"]
            )

    meta = st.session_state.file_meta

    if meta and not meta["error"]:
        st.success(f"**{st.session_state.file_name}**")
        st.divider()

        # ── Header settings ─────────────────────────────────────────────────
        st.subheader("Header Settings")

        has_header = st.checkbox(
            "File has a header row",
            key="_has_header",
            help="Uncheck if the first row contains data, not column names.",
        )

        custom_col_names = st.session_state.custom_col_names

        if not has_header:
            st.info("No header row. Upload an optional header file to name the columns.")
            hf = st.file_uploader(
                "Header file — CSV with one row of column names (optional)",
                type=["csv", "txt"],
                key="_header_file",
            )
            if hf:
                try:
                    header_df = pd.read_csv(io.BytesIO(hf.read()))
                    custom_col_names = list(header_df.columns)
                    st.session_state.custom_col_names = custom_col_names
                    st.success(f"Columns: {', '.join(custom_col_names)}")
                except Exception as e:
                    st.error(f"Could not read header file: {e}")
        else:
            # Reset custom names when header row is enabled
            if st.session_state.custom_col_names is not None:
                st.session_state.custom_col_names = None
            custom_col_names = None

        # Rebuild df when header settings change
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
                    pd.DataFrame({
                        "Property": ["Format", "Encoding", "Confidence", "Delimiter", "Has Header"],
                        "Value": [
                            meta["format"],
                            meta["encoding"],
                            meta["encoding_confidence"],
                            repr(meta["delimiter"]) if meta.get("delimiter") else "N/A",
                            str(has_header),
                        ],
                    }),
                    use_container_width=True, hide_index=True,
                )

            with st.expander("Column Analysis", expanded=True):
                st.dataframe(col_summary(df), use_container_width=True, hide_index=True)

            with st.expander("Data Preview (first 100 rows)"):
                st.dataframe(df.head(100), use_container_width=True)

    elif meta and meta["error"]:
        st.error(meta["error"])


# ═══════════════════════ RIGHT: DATABASE ════════════════════════════════════════
with right:
    st.subheader("2. Connect to SQL Server")

    with st.form("db_form"):
        server = st.text_input("Server / Host", placeholder="localhost or 192.168.1.1")
        port   = st.text_input("Port", placeholder="1433  (leave blank for default)")
        database = st.text_input("Database", placeholder="my_database")
        auth = st.radio(
            "Authentication",
            ["SQL Server Authentication", "Windows Authentication"],
            horizontal=True,
        )
        sql_auth = auth == "SQL Server Authentication"
        user = st.text_input("Username", disabled=not sql_auth)
        pwd  = st.text_input("Password", type="password", disabled=not sql_auth)
        do_connect = st.form_submit_button("Test Connection", use_container_width=True)

    if do_connect:
        if not server or not database:
            st.error("Server and Database are required.")
        else:
            cs = make_conn_str(server, port, database, auth, user, pwd)
            with st.spinner("Connecting…"):
                ok, msg = test_conn(cs)
            if ok:
                st.session_state.db_cs = cs
                st.session_state.db_ok = True
                st.success(msg)
            else:
                st.session_state.db_ok = False
                st.error(f"Failed: {msg}")

    df = st.session_state.file_df

    if st.session_state.db_ok and df is not None:
        st.divider()
        st.subheader("3. Load to Database")

        schemas = get_schemas(st.session_state.db_cs)
        schema = st.selectbox(
            "Schema", schemas,
            index=schemas.index("dbo") if "dbo" in schemas else 0,
        )

        default_tbl = (
            st.session_state.file_name.rsplit(".", 1)[0].replace(" ", "_").replace("-", "_")
            if st.session_state.file_name else ""
        )
        table_name = st.text_input("Target Table Name", value=default_tbl)

        if_exists = st.radio(
            "If table already exists",
            ["fail", "replace", "append"],
            horizontal=True,
            help="fail = raise error | replace = drop & recreate | append = insert rows",
        )

        # ── Column Mapping ──────────────────────────────────────────────────
        col_mapping = None

        if table_name and table_exists(st.session_state.db_cs, schema, table_name):
            st.info(f"Table `{schema}.{table_name}` already exists in the database.")

            if if_exists == "append":
                tbl_cols = get_table_cols(st.session_state.db_cs, schema, table_name)
                if tbl_cols:
                    with st.expander("Column Mapping", expanded=True):
                        st.caption(
                            "Map each file column to a table column. "
                            "Choose **— skip —** to exclude it from the load."
                        )

                        file_cols = [str(c) for c in df.columns]
                        options = ["— skip —"] + tbl_cols

                        # Header row
                        h1, _, h2 = st.columns([5, 1, 5])
                        h1.markdown("**File Column**")
                        h2.markdown("**Table Column**")
                        st.divider()

                        no_header = not st.session_state.get("_has_header", True)
                        col_mapping = {}
                        for i, fc in enumerate(file_cols):
                            if no_header:
                                # Serial positional mapping: file col i → table col i
                                auto_idx = (i + 1) if i < len(tbl_cols) else 0
                            else:
                                # Name-match mapping (case-insensitive)
                                auto_idx = 0
                                for j, tc in enumerate(tbl_cols, start=1):
                                    if tc.lower() == fc.lower():
                                        auto_idx = j
                                        break

                            c1, c2, c3 = st.columns([5, 1, 5])
                            c1.markdown(f"`{fc}`")
                            c2.markdown("→")
                            chosen = c3.selectbox(
                                fc,
                                options=options,
                                index=auto_idx,
                                key=f"cmap_{i}",
                                label_visibility="collapsed",
                            )
                            col_mapping[fc] = None if chosen == "— skip —" else chosen

        # ── Extra columns ───────────────────────────────────────────────────
        st.divider()
        st.markdown("**Extra Columns (optional)**")

        ec1, ec2 = st.columns(2)
        add_filename = ec1.checkbox("Add filename as column")
        filename_col = ec1.text_input("Filename column name", value="source_file", disabled=not add_filename)

        add_filedate = ec2.checkbox("Add date from filename as column")
        if add_filedate:
            _auto_date = extract_date_from_filename(st.session_state.file_name or "")
            if not _auto_date:
                ec2.warning("No date found in filename — defaulting to today.")
            # Key tied to filename → resets automatically when a new file is uploaded
            file_date_val = ec2.date_input(
                "File date",
                value=_auto_date or datetime.today().date(),
                key=f"_filedate_{st.session_state.file_name}",
            )
        else:
            file_date_val = None
        filedate_col = ec2.text_input("File date column name", value="file_date", disabled=not add_filedate)

        st.divider()
        if st.button("Load Data →", type="primary", use_container_width=True):
            if not table_name:
                st.error("Table name is required.")
            else:
                # Ensure all column labels are strings before any mapping
                src = df.copy()
                src.columns = src.columns.astype(str)

                if col_mapping:
                    # Validate: no two file columns mapped to the same table column
                    mapped_targets = [tc for tc in col_mapping.values() if tc is not None]
                    dupes = {tc for tc in mapped_targets if mapped_targets.count(tc) > 1}
                    if dupes:
                        st.error(
                            f"Duplicate mapping: {sorted(dupes)} are assigned more than once. "
                            "Each table column can only receive one file column."
                        )
                        st.stop()

                    if not mapped_targets:
                        st.error("No columns mapped. Map at least one file column to a table column.")
                        st.stop()

                    # Build final_df column-by-column from the mapping
                    final_df = pd.DataFrame()
                    for file_col, table_col in col_mapping.items():
                        if table_col is not None:
                            final_df[table_col] = src[file_col].values
                else:
                    final_df = src.copy()

                # Append extra columns
                if add_filename and filename_col:
                    final_df[filename_col] = st.session_state.file_name
                if add_filedate and filedate_col:
                    final_df[filedate_col] = file_date_val

                # Replace pandas NA/NaN with None so SQL Server receives proper NULLs
                final_df = final_df.where(pd.notnull(final_df), None)

                # Preview before insert
                with st.expander("Preview: data being inserted", expanded=True):
                    st.dataframe(final_df.head(10), use_container_width=True)

                try:
                    engine = make_engine(st.session_state.db_cs)
                    with st.spinner(f"Loading {len(final_df):,} rows into {schema}.{table_name}…"):
                        final_df.to_sql(
                            name=table_name,
                            con=engine,
                            schema=schema,
                            if_exists=if_exists,
                            index=False,
                            chunksize=1000,
                        )
                    st.success(f"Loaded **{len(final_df):,} rows** into `{schema}.{table_name}`")
                    st.balloons()
                except Exception as e:
                    st.error(f"Load failed: {e}")
