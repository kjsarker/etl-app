# ETL File Loader

Upload a file, inspect its structure, and load it into a SQL Server database.

**Supported file formats:** CSV, TSV, TXT, Excel (.xlsx / .xls), JSON, Parquet  
**Supported database:** Microsoft SQL Server

---

## Installation on a New Machine

### Step 1 — Install Python

Download and install Python 3.9 or later from:  
https://www.python.org/downloads/

> **Important:** During installation, check the box **"Add Python to PATH"** before clicking Install.

---

### Step 2 — Install ODBC Driver 17 for SQL Server

Download and install the driver from:  
https://aka.ms/downloadmsodbcsql

This is a free Microsoft driver required to connect to SQL Server.

---

### Step 3 — Download the App

Go to https://github.com/kjsarker/etl-app

Click the green **Code** button → **Download ZIP** → extract the folder anywhere on your machine.

---

### Step 4 — Run the Installer

Inside the extracted folder, double-click **`install.bat`**

This will:
- Verify Python and the ODBC Driver are installed
- Create a self-contained virtual environment (`.venv`)
- Install all required Python packages automatically

If Python or the ODBC Driver is not found, the installer will stop and show the download link.

> You only need to run `install.bat` **once** per machine.

---

## Running the App

After installation, double-click **`run.bat`** every time you want to launch the app.

The browser will open automatically at `http://localhost:8501`

Keep the terminal window open while using the app. Close it to stop the app.

---

## How It Works

1. **Upload a file** — the app auto-detects format, encoding, delimiter, and whether it has a header row
2. **Configure** — set header options, upload a schema file if there is no header, and map columns if needed
3. **Connect to SQL Server** — enter your server, database, and credentials
4. **Load** — choose a target schema and table, optionally add filename and file date as columns, then click Load
