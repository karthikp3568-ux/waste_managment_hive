# Bengaluru Waste Intelligence

FIELDNOTE is a Flask application for importing authorized CSV/Excel waste records into Apache Hive and exploring only the values stored there. HiveServer2 and HiveQL are required; this app has no analytical database fallback.

## Data status

No Bengaluru dataset was present in the workspace. No sample measurements are created. Analysis pages stay empty or explain why the selected Hive dataset does not support them until a user imports data.

## Requirements and setup

- Python 3.10+
- Docker Engine and Docker Compose v2 for the documented local Apache Hive service
- First-time network access to pull the Hive container image

The environment used to implement this app did not have Docker, Hadoop, or Hive CLI commands available. The compose file is configuration only; it is not evidence that Hive is installed or running.

Start Hive from the repository directory:

```powershell
docker compose up -d
docker compose ps
```

Wait for the Hive service health check. HiveServer2 is published on port `10000`; its web UI is on port `10002`.

### One-command Windows startup

With Docker Desktop running and the Python dependencies installed, run from the repository directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The launcher starts the existing Compose Hive service, waits for PyHive to return `1` from `SELECT 1`, creates/verifies the application database and tables, then starts Flask and opens the browser. It checks port `5000`, prints Docker/Hive startup errors, and does not start Flask if HiveQL is unavailable. Flask logs and its managed process ID are kept under `%TEMP%\bengaluru-waste-intelligence`.

Stop the launcher-managed Flask app and Compose Hive service with:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop.ps1
```

This preserves the `hive-warehouse` volume. If Python dependencies are missing, install them once with `python -m pip install -r requirements.txt`; the launcher does not silently install packages.

Install dependencies and start the local app:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open `http://127.0.0.1:5000`. `FLASK_DEBUG` is off by default. The application binds to loopback. Configure `HIVE_HOST`, `HIVE_PORT`, `HIVE_DATABASE`, `HIVE_USER`, `HIVE_AUTH`, and `PORT` through environment variables or `.env`. Do not expose the development server or unauthenticated HiveServer2 publicly.

## Import flow

1. Upload CSV, XLSX, or XLS up to 32 MB and 100,000 rows.
2. Review inferred column types, missing values, detected date range, and a sample of rows.
3. Map fields that are present. Total waste or at least one material category is required; dates, periods, areas, ward IDs, and geography are optional.
4. Confirm the unit and ward geography system. Unknown ward systems remain unknown and are never joined across datasets by ward number.
5. Review validation warnings and flagged row reasons, then import. The import result is successful only after Hive reports matching observation, category, category-field, and catalog counts.

Hive `NULL` represents missing/unmapped values; measured zero remains `0`. Invalid dates, negative values, non-finite values, and rows with no amount are excluded and counted as flagged. Up to 50 row reasons are returned. Upload and validation state is held in app memory until import.

For Hive imports made before `waste_category_fields` was added, categories with recorded values are recovered from `waste_categories`; a mapped category that was entirely missing cannot be reconstructed from those older rows.

Import IDs are created at preview and reused for retries. If the dataset catalog exists and Hive row counts match, retry returns the existing import instead of inserting it again. If a previous failure left observation/category rows without a catalog record, the app refuses a retry to prevent duplicate writes. The orphaned Hive rows require operator cleanup before the same upload can be attempted again. Separate uploads receive separate dataset IDs.

## Hive schema

The app creates database `bengaluru_waste` (override with `HIVE_DATABASE`) and ORC tables:

- `waste_observations`: dataset ID, source record index, observation date (`DATE`), reported period, area name, ward ID, geography system, total waste (`DOUBLE`), and source filename.
- `waste_categories`: one stored non-null amount per source record/category, with category name, numeric amount, and unit.
- `waste_category_fields`: one row for every mapped category, including columns with all values missing. This preserves the distinction between a supplied but missing category and an unmapped category.
- `waste_datasets`: source file, selected waste unit, declared geography system, flagged-row count, and import time.

Units are metadata and are not converted. Category rows preserve zero values; missing category values have no amount row but remain listed in `waste_category_fields`. Query values use PyHive parameter binding; uploaded names and values are not interpolated into HiveQL identifiers.

## Analytics and safeguards

- **Overview:** HiveQL record/area/date coverage and recorded waste totals, plus SVG trend, material-total, and top-area charts. Charts use bounded Hive aggregates, show record coverage on hover, and explain when dates, amounts, units, duplicates, or geography prevent a safe chart.
- **Area Analysis:** HiveQL ranks area/ward totals, record coverage, optional date range, and up to 500 results. Search and sorting apply to the returned result set. Comparisons are withheld for duplicate or incomplete keys, mixed periods/geography, or mixed area-name/ward-ID types. Unknown ward geography is labelled as within-dataset only.
- **Category Analysis:** HiveQL category sums, record counts, measured zero counts, and missing counts. Share of reported total appears only with complete category/total coverage and a known matching unit. A per-record category sum discrepancy is flagged above 1% (or 0.000001 for zero totals); source values are never modified.
- **Trends:** HiveQL daily, monthly, or yearly sums from usable observation dates only. Missing date buckets are inserted as explicit gaps with zero records, not zero waste. Daily trends are limited to 370 days and responses to 800 periods.
- **Clustering Lab:** Hive records are retrieved by dataset and source record index. It requires known-unit numeric waste/category fields, two to eight clusters, enough complete cases, feature variation, and distinct profiles. At most the first 5,000 records by source index are used. Features are standardized with `StandardScaler`; K-means uses seed 42 and `n_init=10`. Complete cases only are used; excluded counts are shown. Cluster assignments can be exported with source record index, area/ward, date, and cluster label. Clusters describe patterns; they do not establish causes or recommendations.
- **Economics:** Not calculated. The current schema has no reported costs, prices, or revenue inputs.

Charts use native SVG and do not add a charting library or send data to an external service.

Queries are scoped to the selected dataset. Area rows, trend periods, categories, and clustering inputs are bounded. Unknown units, missing keys, repeated observations, and incompatible reporting coverage are shown as limitations or result in withheld analysis.

## Verification

The repository has no configured test/lint suite. Checks performed for a local run should include `python -m py_compile app.py`, `node --check static/app.js`, and a live import plus API query against Hive. A successful Hive connection badge alone is not proof of import/query behavior. The implementation environment did not have Docker/Hive running; live Hive validation must be performed in an environment with the service available.

Stop Hive with `docker compose down`. The named `hive-warehouse` volume persists data. `docker compose down -v` deletes the warehouse volume and its data.
