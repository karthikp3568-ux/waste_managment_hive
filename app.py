import math
import os
import re
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from pyhive import hive
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.secret_key = os.environ.get("APP_SECRET")
app.config["JSON_SORT_KEYS"] = False

SUPPORTED = ["observation_date", "period", "area_name", "ward_id", "geography_system", "total_waste"]
uploads = {}
in_memory_datasets = {}
import_lock = threading.Lock()
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DB = os.environ.get("HIVE_DATABASE", "bengaluru_waste")
if not IDENTIFIER.fullmatch(DB):
    raise RuntimeError("HIVE_DATABASE must be a lowercase Hive identifier (letters, digits, underscores).")
TABLE = "waste_observations"
CATEGORY_TABLE = "waste_categories"
CATEGORY_FIELDS_TABLE = "waste_category_fields"
DATASET_TABLE = "waste_datasets"


class HiveUnavailable(Exception):
    pass


def connect():
    try:
        return hive.Connection(
            host=os.environ.get("HIVE_HOST", "localhost"),
            port=int(os.environ.get("HIVE_PORT", "10000")),
            username=os.environ.get("HIVE_USER", "hive"),
            database="default",
            auth=os.environ.get("HIVE_AUTH", "NONE"),
        )
    except Exception as exc:
        raise HiveUnavailable(str(exc)) from exc


def ensure_schema(cursor):
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB}`")
    cursor.execute(f"USE `{DB}`")
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        dataset_id STRING, record_index BIGINT, observation_date DATE, period STRING,
        area_name STRING, ward_id STRING, geography_system STRING,
        total_waste DOUBLE, source_file STRING
    ) STORED AS ORC""")
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {CATEGORY_TABLE} (
        dataset_id STRING, record_index BIGINT, category_name STRING, amount DOUBLE,
        unit STRING
    ) STORED AS ORC""")
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {CATEGORY_FIELDS_TABLE} (
        dataset_id STRING, category_name STRING, unit STRING
    ) STORED AS ORC""")
    cursor.execute(f"""CREATE TABLE IF NOT EXISTS {DATASET_TABLE} (
        dataset_id STRING, source_file STRING, waste_unit STRING, geography_system STRING,
        flagged_records BIGINT, imported_at STRING
    ) STORED AS ORC""")


def dataset_id_arg(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) else None


def scoped_where(dataset_id, date_from=None, date_to=None):
    where = "dataset_id = %s"
    params = [dataset_id]
    if date_from:
        where += " AND observation_date >= %s"
        params.append(date_from)
    if date_to:
        where += " AND observation_date <= %s"
        params.append(date_to)
    return where, tuple(params)


def quality_snapshot(cursor, dataset_id, date_from=None, date_to=None):
    where, params = scoped_where(dataset_id, date_from, date_to)
    cursor.execute(f"""SELECT COUNT(*), COUNT(DISTINCT period), COUNT(DISTINCT geography_system),
        SUM(CASE WHEN period IS NULL THEN 1 ELSE 0 END),
        SUM(CASE WHEN total_waste IS NULL THEN 1 ELSE 0 END),
        MIN(observation_date), MAX(observation_date), COUNT(DISTINCT observation_date),
        COUNT(DISTINCT CASE WHEN area_name IS NOT NULL THEN CONCAT('area:', area_name) ELSE CONCAT('ward:', ward_id) END),
        COUNT(DISTINCT CASE WHEN area_name IS NOT NULL THEN 'area_name' WHEN ward_id IS NOT NULL THEN 'ward_id' END)
        FROM {TABLE} WHERE {where}""", params)
    row = cursor.fetchone()
    cursor.execute(f"""SELECT COUNT(*), COUNT(DISTINCT CONCAT(
        CAST(LENGTH(COALESCE(CAST(observation_date AS STRING),'')) AS STRING), '#', COALESCE(CAST(observation_date AS STRING),''), '|',
        CAST(LENGTH(COALESCE(period,'')) AS STRING), '#', COALESCE(period,''), '|',
        CAST(LENGTH(COALESCE(area_name,'')) AS STRING), '#', COALESCE(area_name,''), '|',
        CAST(LENGTH(COALESCE(ward_id,'')) AS STRING), '#', COALESCE(ward_id,'')))
        FROM {TABLE} WHERE {where} AND (observation_date IS NOT NULL OR period IS NOT NULL)
        AND (area_name IS NOT NULL OR ward_id IS NOT NULL)""", params)
    duplicate_row = cursor.fetchone()
    eligible = int(duplicate_row[0] or 0)
    distinct_keys = int(duplicate_row[1] or 0)
    duplicates = eligible > distinct_keys
    missing_keys = eligible < int(row[0] or 0)
    mixed_period_coverage = int(row[1] or 0) > 0 and int(row[3] or 0) > 0
    mixed_area_kinds = int(row[9] or 0) > 1
    incompatible = int(row[1] or 0) > 1 or int(row[2] or 0) > 1 or mixed_period_coverage or missing_keys or mixed_area_kinds
    return {
        "records": int(row[0] or 0), "period_count": int(row[1] or 0), "geography_count": int(row[2] or 0),
        "missing_periods": int(row[3] or 0), "missing_total": int(row[4] or 0), "date_from": row[5], "date_to": row[6],
        "date_count": int(row[7] or 0), "area_count": int(row[8] or 0), "area_kind_count": int(row[9] or 0),
        "duplicates": duplicates, "missing_keys": missing_keys, "incompatible": incompatible,
        "mixed_area_kinds": mixed_area_kinds,
    }
def hive_status():
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            result = cur.fetchone()
            if not result or int(result[0]) != 1:
                raise RuntimeError("HiveServer2 did not return the expected SELECT 1 result.")
            ensure_schema(cur)
        return {"connected": True, "state": "ready", "message": "HiveServer2 ready; HiveQL and the application schema are available."}
    except Exception as exc:
        return {"connected": False, "state": "failed", "message": f"Hive unavailable: {str(exc)[:240]}"}


def read_upload(file):
    name = file.filename or "upload"
    ext = os.path.splitext(name)[1].lower()
    if ext == ".csv":
        frame = pd.read_csv(file, dtype=object, keep_default_na=True)
    elif ext in (".xlsx", ".xls"):
        frame = pd.read_excel(file, dtype=object)
    else:
        raise ValueError("Unsupported file. Upload a CSV or Excel (.xlsx, .xls) file.")
    if frame.empty or len(frame.columns) == 0:
        raise ValueError("The file has no data rows or columns.")
    if len(frame) > 100000:
        raise ValueError("This MVP accepts up to 100,000 rows per import.")
    frame.columns = [str(c).strip() for c in frame.columns]
    if any(not c for c in frame.columns) or len(set(frame.columns)) != len(frame.columns):
        raise ValueError("Column names must be non-empty and unique.")
    return frame, name


def infer_type(series):
    vals = series.dropna()
    if vals.empty:
        return "empty"
    if pd.api.types.is_numeric_dtype(vals):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(vals):
        return "date"
    numeric = pd.to_numeric(vals.astype(str).str.replace(",", "", regex=False), errors="coerce")
    if numeric.notna().all():
        return "number"
    if len(vals) and vals.astype(str).str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() > 0.7:
        return "date-like text"
    return "text"


def preview_frame(frame):
    date_col = next((c for c in frame.columns if "date" in c.lower()), None)
    date_range = None
    if date_col:
        parsed = pd.to_datetime(frame[date_col], errors="coerce").dropna()
        if not parsed.empty:
            date_range = {"from": parsed.min().date().isoformat(), "to": parsed.max().date().isoformat()}
    return {
        "columns": [{"name": c, "inferred_type": infer_type(frame[c]), "missing": int(frame[c].isna().sum())} for c in frame.columns],
        "row_count": int(len(frame)),
        "sample": frame.head(8).where(pd.notna(frame.head(8)), None).to_dict(orient="records"),
        "date_range": date_range,
    }


def compute_general_stats(frame, name="dataset"):
    df = frame.copy()
    df.columns = [str(c).strip() for c in df.columns]
    
    date_col = next((c for c in df.columns if any(k in c.lower() for k in ["date", "day", "time"])), None)
    ward_col = next((c for c in df.columns if any(k in c.lower() for k in ["ward_id", "ward_no", "wardid", "code"])), None)
    area_col = next((c for c in df.columns if c != ward_col and any(k in c.lower() for k in ["area_name", "ward_name", "area", "zone", "ward", "location", "division"])), None)
    if not area_col and ward_col:
        area_col = ward_col
    total_col = next((c for c in df.columns if any(k in c.lower() for k in ["total_waste", "total", "waste_total", "waste_amount", "amount"])), None)
    
    cat_keywords = ["dry", "wet", "plastic", "sanitary", "organic", "metal", "glass", "inert", "paper", "hazardous", "e_waste", "c_and_d"]
    category_cols = [c for c in df.columns if any(k in c.lower() for k in cat_keywords) and c != total_col]
    
    numeric_cols = []
    for c in df.columns:
        if c in [date_col, area_col, ward_col]:
            continue
        try:
            converted = pd.to_numeric(df[c].astype(str).str.replace(",", "", regex=False), errors="coerce")
            if converted.notna().sum() > 0:
                df[f"_num_{c}"] = converted
                numeric_cols.append(c)
        except Exception:
            pass
            
    if not total_col:
        if category_cols:
            df["_num__derived_total"] = df[[f"_num_{c}" for c in category_cols if f"_num_{c}" in df.columns]].sum(axis=1)
            total_col = "_derived_total"
            numeric_cols.append(total_col)
        elif numeric_cols:
            total_col = numeric_cols[0]
            
    total_waste_val = float(df[f"_num_{total_col}"].sum()) if total_col and f"_num_{total_col}" in df.columns else 0.0
    
    date_range = {"from": None, "to": None}
    unique_dates = 1
    if date_col:
        parsed_dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
        if not parsed_dates.empty:
            date_range = {"from": parsed_dates.min().strftime("%Y-%m-%d"), "to": parsed_dates.max().strftime("%Y-%m-%d")}
            unique_dates = max(1, parsed_dates.dt.date.nunique())
            
    area_breakdown = []
    top_ward_name = "—"
    top_ward_val = 0.0
    unique_wards = 0
    if area_col and total_col and f"_num_{total_col}" in df.columns:
        grouped = df.groupby(area_col)[f"_num_{total_col}"].agg(["sum", "count"]).reset_index()
        grouped.columns = ["area", "total", "records"]
        grouped = grouped.sort_values(by="total", ascending=False)
        unique_wards = len(grouped)
        if not grouped.empty:
            top_ward_name = str(grouped.iloc[0]["area"])
            top_ward_val = float(grouped.iloc[0]["total"])
        area_breakdown = [
            {"area": str(r["area"]), "total": round(float(r["total"]), 2), "records": int(r["records"])}
            for _, r in grouped.iterrows()
        ]
        
    category_breakdown = []
    if category_cols:
        cat_sums = {}
        for c in category_cols:
            if f"_num_{c}" in df.columns:
                cat_sums[c] = float(df[f"_num_{c}"].sum())
        cat_total = sum(cat_sums.values())
        for c, s in cat_sums.items():
            category_breakdown.append({
                "name": c.replace("_", " ").title(),
                "raw_name": c,
                "total": round(s, 2),
                "share": round(100.0 * s / cat_total, 1) if cat_total > 0 else 0.0
            })
            
    trend_points = []
    if date_col and total_col and f"_num_{total_col}" in df.columns:
        df["_dt_str"] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
        valid_dates = df.dropna(subset=["_dt_str"])
        if not valid_dates.empty:
            grouped_d = valid_dates.groupby("_dt_str")[f"_num_{total_col}"].agg(["sum", "count"]).reset_index()
            grouped_d.columns = ["date", "total", "records"]
            grouped_d = grouped_d.sort_values(by="date")
            trend_points = [
                {"period": str(r["date"]), "total": round(float(r["total"]), 2), "records": int(r["records"]), "gap": False}
                for _, r in grouped_d.iterrows()
            ]
            
    total_cells = df.shape[0] * df.shape[1]
    missing_cells = int(df.isna().sum().sum())
    completeness = round(100.0 * (total_cells - missing_cells) / max(1, total_cells), 1)
    
    sample_records = frame.head(50).where(pd.notna(frame.head(50)), None).to_dict(orient="records")
    
    return {
        "dataset_name": name,
        "row_count": int(len(frame)),
        "col_count": int(len(frame.columns)),
        "total_waste": round(total_waste_val, 2),
        "avg_daily": round(total_waste_val / unique_dates, 2),
        "unique_wards": unique_wards or 1,
        "date_range": date_range,
        "top_ward": {"name": top_ward_name, "total": round(top_ward_val, 2)},
        "completeness": completeness,
        "unit": "tonnes",
        "area_breakdown": area_breakdown,
        "category_breakdown": category_breakdown,
        "trend_points": trend_points,
        "sample_records": sample_records,
        "columns": list(frame.columns),
        "detected_cols": {
            "date": date_col,
            "area": area_col,
            "ward": ward_col,
            "total": total_col,
            "categories": category_cols
        }
    }


def register_in_memory_dataset(frame, name, unit="tonnes", geography_system="BBMP 2022", dataset_id=None):
    d_id = dataset_id or uuid.uuid4().hex
    stats = compute_general_stats(frame, name)
    det = stats["detected_cols"]
    
    meta = {
        "id": d_id,
        "name": name,
        "unit": unit,
        "geography_system": geography_system,
        "flagged": 0
    }
    
    in_memory_datasets[d_id] = {
        "id": d_id,
        "meta": meta,
        "frame": frame,
        "general_stats": stats,
        "date_col": det["date"],
        "area_col": det["area"],
        "ward_col": det["ward"],
        "total_col": det["total"],
        "category_cols": det["categories"]
    }
    return d_id


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify({"hive": hive_status(), "dataset": None})


@app.get("/api/datasets")
def datasets():
    hive_datasets = []
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            ensure_schema(cur)
            cur.execute(f"SELECT dataset_id, source_file, waste_unit, geography_system, flagged_records FROM {DATASET_TABLE} ORDER BY imported_at DESC")
            hive_datasets = [{"id": r[0], "name": r[1], "unit": r[2], "geography_system": r[3], "flagged": int(r[4] or 0)} for r in cur.fetchall()]
    except Exception as exc:
        if not in_memory_datasets:
            return jsonify(error=f"Hive unavailable: {str(exc)[:240]}", hive_verified=False), 503

    local_datasets = [
        {"id": d_id, "name": f"{ds['meta']['name']}", "unit": ds['meta']['unit'], "geography_system": ds['meta']['geography_system'], "flagged": ds['meta'].get("flagged", 0)}
        for d_id, ds in in_memory_datasets.items()
    ]
    return jsonify(hive_datasets + local_datasets)


@app.post("/api/upload")
def upload():
    if "file" not in request.files:
        return jsonify(error="Choose a CSV or Excel file."), 400
    try:
        frame, name = read_upload(request.files["file"])
        token = uuid.uuid4().hex
        dataset_id = uuid.uuid4().hex
        uploads[token] = {"frame": frame, "name": name, "dataset_id": dataset_id}
        register_in_memory_dataset(frame, name, dataset_id=dataset_id)
        info = preview_frame(frame)
        info["token"] = token
        info["dataset_id"] = dataset_id
        info["name"] = name
        info["general_stats"] = compute_general_stats(frame, name)
        return jsonify(info)
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.route("/api/sample", methods=["GET", "POST"])
def load_sample():
    sample_path = os.path.join(os.path.dirname(__file__), "bengaluru_ward_waste_sample.csv")
    if os.path.exists(sample_path):
        frame = pd.read_csv(sample_path)
        dataset_id = register_in_memory_dataset(frame, "Bengaluru Wards Solid Waste Sample 2026", unit="tonnes", geography_system="BBMP 2022")
        return jsonify({"status": "loaded", "dataset_id": dataset_id, "name": "Bengaluru Wards Solid Waste Sample 2026"})
    return jsonify(error="Sample file not found"), 404


@app.get("/api/analytics/general_stats")
def get_general_stats():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if dataset_id and dataset_id in in_memory_datasets:
        return jsonify(in_memory_datasets[dataset_id]["general_stats"])
    return jsonify(error="Dataset general stats not available."), 404


@app.post("/api/analytics/filter")
def filter_analytics():
    body = request.get_json(silent=True) or {}
    dataset_id = dataset_id_arg(body.get("dataset_id"))
    if not dataset_id or dataset_id not in in_memory_datasets:
        return jsonify(error="Valid in-memory dataset required for live filtering."), 400
    
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"].copy()
    
    ward = body.get("ward")
    if ward and ward != "ALL":
        area_col = ds.get("area_col") or "area_name"
        ward_col = ds.get("ward_col") or "ward_id"
        if area_col in frame.columns and (frame[area_col].astype(str) == str(ward)).any():
            frame = frame[frame[area_col].astype(str) == str(ward)]
        elif ward_col in frame.columns and (frame[ward_col].astype(str) == str(ward)).any():
            frame = frame[frame[ward_col].astype(str) == str(ward)]
            
    date_from, date_to = body.get("date_from"), body.get("date_to")
    date_col = ds.get("date_col") or "observation_date"
    if date_col in frame.columns:
        if date_from:
            frame = frame[frame[date_col].astype(str) >= str(date_from)]
        if date_to:
            frame = frame[frame[date_col].astype(str) <= str(date_to)]
            
    search = (body.get("search") or "").strip().lower()
    if search:
        mask = frame.astype(str).apply(lambda row: row.str.lower().str.contains(search, regex=False)).any(axis=1)
        frame = frame[mask]
        
    filtered_stats = compute_general_stats(frame, ds["meta"]["name"])
    return jsonify(filtered_stats)


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify(error="File exceeds the 32 MB upload limit."), 413


def parse_number(value):
    if pd.isna(value) or str(value).strip() == "":
        return None
    number = float(str(value).replace(",", "").strip())
    if not math.isfinite(number):
        raise ValueError("Waste amounts must be finite numbers.")
    if number < 0:
        raise ValueError("Waste amounts cannot be negative.")
    return number


def insert_values(cursor, table, rows, date_field_index=None):
    if not rows:
        return
    width = len(rows[0])
    row_sql = "(" + ",".join("CAST(%s AS DATE)" if i == date_field_index else "%s" for i in range(width)) + ")"
    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        sql = f"INSERT INTO TABLE {table} VALUES " + ",".join(row_sql for _ in batch)
        params = tuple(value for row in batch for value in row)
        cursor.execute(sql, params)


@app.post("/api/validate")
def validate():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Validation request must be a JSON object."), 400
    token = body.get("token")
    item = uploads.get(token) if isinstance(token, str) else None
    if not item:
        return jsonify(error="Upload expired. Please upload the file again."), 400
    if item.get("imported"):
        return jsonify(error="This upload is already imported. Upload the source again to create a separate dataset version."), 409
    mapping = body.get("mapping", {})
    if not isinstance(mapping, dict):
        return jsonify(error="Column mapping must be an object."), 400
    units = body.get("unit", "unknown")
    if not isinstance(units, str):
        return jsonify(error="Waste unit must be text."), 400
    custom_unit = body.get("custom_unit", "")
    if not isinstance(custom_unit, str):
        return jsonify(error="Custom waste unit must be text."), 400
    if units == "other":
        units = custom_unit.strip() or "unknown"
    geography = body.get("geography_system", "unknown")
    if not isinstance(geography, str):
        return jsonify(error="Geography system must be text."), 400
    geography = geography.strip() or "unknown"
    frame = item["frame"]
    errors, warnings = [], []
    if body.get("unit") == "other" and units == "unknown":
        errors.append("Enter the waste unit for the 'other' unit selection.")
    for canonical, source in mapping.items():
        if canonical not in SUPPORTED and not canonical.startswith("category:"):
            errors.append(f"Unsupported target field: {canonical}")
        if source is not None and not isinstance(source, str):
            errors.append(f"Source column for {canonical} must be text.")
            continue
        if source and source not in frame.columns:
            errors.append(f"Column not found: {source}")
    category_map = {k.split(":", 1)[1]: v for k, v in mapping.items() if isinstance(k, str) and k.startswith("category:") and isinstance(v, str) and v}
    if any(not name for name in category_map):
        errors.append("Material category names cannot be empty.")
    if len(set(category_map.values())) != len(category_map):
        errors.append("A source column cannot be mapped to multiple material categories.")
    if mapping.get("total_waste") and mapping.get("total_waste") in category_map.values():
        errors.append("The total waste column cannot also be mapped as a material category.")
    if not mapping.get("total_waste") and not category_map:
        errors.append("Map total waste or at least one material category.")
    if category_map and units == "unknown":
        errors.append("Choose the unit for mapped waste amount fields.")
    records, flagged = [], []
    for idx, row in frame.iterrows():
        try:
            amount = parse_number(row[mapping["total_waste"]]) if mapping.get("total_waste") else None
            cats = {name: parse_number(row[col]) for name, col in category_map.items()}
            cats = {k: v for k, v in cats.items() if v is not None}
            if amount is None and not cats:
                flagged.append({"row": int(idx) + 2, "reason": "No waste amount recorded"})
                continue
            raw_date = row[mapping["observation_date"]] if mapping.get("observation_date") else None
            obs_date = None
            if raw_date is not None and not pd.isna(raw_date) and str(raw_date).strip():
                try:
                    obs_date = pd.to_datetime(raw_date, errors="raise").date().isoformat()
                except Exception:
                    raise ValueError("Invalid date")
            def val(field):
                col = mapping.get(field)
                if not col or pd.isna(row[col]) or str(row[col]).strip() == "":
                    return None
                return str(row[col]).strip()
            records.append({"record_index": int(idx) + 1, "date": obs_date, "period": val("period"), "area": val("area_name"),
                            "ward": val("ward_id"), "geo": val("geography_system") or geography, "total": amount, "categories": cats})
        except Exception as exc:
            flagged.append({"row": int(idx) + 2, "reason": str(exc)})
    if len(flagged) == len(frame):
        errors.append("No valid records were found.")
    if not mapping.get("observation_date"):
        warnings.append("No date mapped; date coverage will be unavailable.")
    if units == "unknown" and mapping.get("total_waste"):
        warnings.append("Waste unit is unknown; values are unscaled and must not be compared with datasets in other units.")
    if not mapping.get("area_name") and not mapping.get("ward_id"):
        warnings.append("No area or ward identifier mapped; area counts will be unavailable.")
    if geography.lower() == "unknown" and mapping.get("ward_id"):
        warnings.append("Ward geography system is unknown; ward identifiers will not be compared across datasets.")
    row_geographies = {r["geo"] for r in records if r["geo"]}
    if mapping.get("geography_system") and len(row_geographies) > 1:
        warnings.append("Multiple geography systems occur in this file; area totals will be withheld.")
    stored_geography = next(iter(row_geographies)) if len(row_geographies) == 1 else ("mixed" if len(row_geographies) > 1 else geography)
    has_duplicates = False
    keys = [(r["date"], r["period"], r["area"], r["ward"]) for r in records
            if (r["date"] or r["period"]) and (r["area"] or r["ward"])]
    has_duplicates = len(keys) != len(set(keys))
    if has_duplicates:
        warnings.append("Duplicate observation keys detected; dashboard totals will be withheld to avoid double counting.")
    key_coverage_incomplete = len(keys) < len(records)
    if key_coverage_incomplete:
        warnings.append("Some rows lack a date/period or area/ward key; duplicate observations cannot be ruled out for those rows.")
    result = {"errors": errors, "warnings": warnings, "valid_records": len(records), "flagged_count": len(flagged), "flagged_records": flagged[:50],
                    "date_range": {"from": min((r["date"] for r in records if r["date"]), default=None),
                                   "to": max((r["date"] for r in records if r["date"]), default=None)},
                    "preview_id": body.get("token"), "unit": units, "geography_system": geography,
                    "duplicate_check": has_duplicates, "can_import": not errors and bool(records)}
    stash_validation(body.get("token"), mapping, units, stored_geography, records, flagged, errors, list(category_map))
    return jsonify(result)


def stash_validation(token, mapping, unit, geography, records, flagged, errors, category_names):
    if token in uploads:
        uploads[token].update({"mapping": mapping, "unit": unit, "geography": geography, "records": records, "flagged": flagged, "errors": errors, "category_names": category_names})
    return True


@app.post("/api/import")
def import_data():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Import request must be a JSON object."), 400
    token = body.get("token")
    item = uploads.get(token) if isinstance(token, str) else None
    if not item or "records" not in item:
        return jsonify(error="Validate this upload before importing."), 400
    if item.get("errors"):
        return jsonify(error="Mapping validation failed. Correct the mapping and validate again."), 400
    if len(item["flagged"]) == len(item["frame"]):
        return jsonify(error="There are no valid records to import."), 400
    dataset_id = item["dataset_id"]
    with import_lock:
        try:
            with closing(connect()) as conn:
                cur = conn.cursor()
                ensure_schema(cur)
                cur.execute(f"SELECT COUNT(*) FROM {DATASET_TABLE} WHERE dataset_id = %s", (dataset_id,))
                catalog_count = int(cur.fetchone()[0])
                cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE dataset_id = %s", (dataset_id,))
                existing_records = int(cur.fetchone()[0])
                cur.execute(f"SELECT COUNT(*) FROM {CATEGORY_TABLE} WHERE dataset_id = %s", (dataset_id,))
                existing_categories = int(cur.fetchone()[0])
                cur.execute(f"SELECT COUNT(*) FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s", (dataset_id,))
                existing_category_fields = int(cur.fetchone()[0])
                expected_categories = sum(len(r["categories"]) for r in item["records"])
                expected_fields = len(item.get("category_names", []))
                if catalog_count:
                    if catalog_count != 1 or existing_records != len(item["records"]) or existing_categories != expected_categories or existing_category_fields != expected_fields:
                        return jsonify(error="Existing Hive import is incomplete; refusing to report or duplicate it.", dataset_id=dataset_id, hive_verified=False), 409
                    item.update({"loaded": existing_records, "imported": True})
                    return jsonify({"dataset_id": dataset_id, "loaded": existing_records, "flagged": len(item["flagged"]), "hive_verified": True, "already_imported": True})
                if existing_records or existing_categories or existing_category_fields:
                    return jsonify(error="A prior import partially wrote this dataset to Hive. The app will not retry and duplicate it; remove the orphaned rows before uploading again.", dataset_id=dataset_id, hive_verified=False), 409

                observation_rows = [(
                    dataset_id, r["record_index"], r["date"], r["period"], r["area"], r["ward"], r["geo"], r["total"], item["name"]
                ) for r in item["records"]]
                insert_values(cur, TABLE, observation_rows, date_field_index=2)
                category_rows = [
                    (dataset_id, r["record_index"], category, amount, item["unit"])
                    for r in item["records"] for category, amount in r["categories"].items()
                ]
                insert_values(cur, CATEGORY_TABLE, category_rows)
                category_field_rows = [(dataset_id, name, item["unit"]) for name in item.get("category_names", [])]
                insert_values(cur, CATEGORY_FIELDS_TABLE, category_field_rows)
                cur.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE dataset_id = %s", (dataset_id,))
                loaded = int(cur.fetchone()[0])
                cur.execute(f"SELECT COUNT(*) FROM {CATEGORY_TABLE} WHERE dataset_id = %s", (dataset_id,))
                category_count = int(cur.fetchone()[0])
                cur.execute(f"SELECT COUNT(*) FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s", (dataset_id,))
                category_field_count = int(cur.fetchone()[0])
                if loaded != len(observation_rows) or category_count != expected_categories or category_field_count != expected_fields:
                    raise RuntimeError("Hive row-count verification did not match the staged import.")
                imported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                insert_values(cur, DATASET_TABLE, [(
                    dataset_id, item["name"], item["unit"], item["geography"], len(item["flagged"]), imported_at
                )])
                cur.execute(f"SELECT COUNT(*) FROM {DATASET_TABLE} WHERE dataset_id = %s", (dataset_id,))
                if int(cur.fetchone()[0]) != 1:
                    raise RuntimeError("Hive dataset catalog verification failed.")
            item.update({"loaded": loaded, "imported": True, "imported_at": imported_at})
            return jsonify({"dataset_id": dataset_id, "loaded": loaded, "flagged": len(item["flagged"]), "hive_verified": True, "already_imported": False})
        except Exception as exc:
            return jsonify(error=f"Hive import failed; no success is claimed: {str(exc)[:300]}", dataset_id=dataset_id, hive_verified=False), 503


def load_dataset_metadata(cursor, dataset_id):
    cursor.execute(f"SELECT source_file, waste_unit, geography_system, flagged_records FROM {DATASET_TABLE} WHERE dataset_id = %s", (dataset_id,))
    row = cursor.fetchone()
    if not row:
        return None
    return {"id": dataset_id, "name": row[0], "unit": row[1], "geography_system": row[2], "flagged": int(row[3] or 0)}


def category_results(cursor, dataset_id, quality, meta):
    cursor.execute(f"""SELECT f.category_name, SUM(c.amount), f.unit, COUNT(DISTINCT c.record_index),
        COUNT(DISTINCT c.unit), COUNT(DISTINCT CASE WHEN c.amount = 0 THEN c.record_index END), f.field_unit_count
        FROM (SELECT dataset_id, category_name, MAX(unit) AS unit, COUNT(DISTINCT unit) AS field_unit_count
              FROM (SELECT dataset_id, category_name, unit FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s
                    UNION SELECT dataset_id, category_name, unit FROM {CATEGORY_TABLE} WHERE dataset_id = %s) inventory
              GROUP BY dataset_id, category_name) f
        LEFT JOIN {CATEGORY_TABLE} c ON f.dataset_id = c.dataset_id AND f.category_name = c.category_name
        GROUP BY f.category_name, f.unit ORDER BY f.category_name LIMIT 200""", (dataset_id, dataset_id))
    rows = cursor.fetchall()
    cursor.execute(f"""SELECT COUNT(DISTINCT category_name), COUNT(DISTINCT unit) FROM (
        SELECT dataset_id, category_name, unit FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s
        UNION SELECT dataset_id, category_name, unit FROM {CATEGORY_TABLE} WHERE dataset_id = %s
    ) category_inventory""", (dataset_id, dataset_id))
    counts = cursor.fetchone()
    category_types, category_units = int(counts[0] or 0), int(counts[1] or 0)
    waste_total = None
    if quality["missing_total"] == 0 and not quality["incompatible"] and not quality["duplicates"]:
        cursor.execute(f"SELECT SUM(total_waste) FROM {TABLE} WHERE dataset_id = %s", (dataset_id,))
        val = cursor.fetchone()[0]
        waste_total = float(val) if val is not None else None
    full_category_coverage = False
    discrepancy_count = None
    if category_types and category_units == 1 and meta["unit"] != "unknown" and rows and rows[0][2] == meta["unit"]:
        cursor.execute(f"""SELECT COUNT(*) FROM (
            SELECT o.record_index, o.total_waste, COUNT(DISTINCT c.category_name) AS category_count,
                   COUNT(DISTINCT c.unit) AS unit_count, MAX(c.unit) AS category_unit, SUM(c.amount) AS category_total
            FROM {TABLE} o LEFT JOIN {CATEGORY_TABLE} c
              ON o.dataset_id = c.dataset_id AND o.record_index = c.record_index
            WHERE o.dataset_id = %s
            GROUP BY o.record_index, o.total_waste
        ) coverage WHERE category_count = %s AND unit_count = 1 AND category_unit = %s AND total_waste IS NOT NULL""",
            (dataset_id, category_types, meta["unit"]))
        complete_rows = int(cursor.fetchone()[0] or 0)
        full_category_coverage = complete_rows == quality["records"] and quality["missing_total"] == 0
        if full_category_coverage and not quality["incompatible"] and not quality["duplicates"]:
            cursor.execute(f"""SELECT COUNT(*) FROM (
                SELECT o.record_index, o.total_waste, SUM(c.amount) AS category_total
                FROM {TABLE} o JOIN {CATEGORY_TABLE} c
                  ON o.dataset_id = c.dataset_id AND o.record_index = c.record_index
                WHERE o.dataset_id = %s
                GROUP BY o.record_index, o.total_waste
                HAVING ABS(SUM(c.amount) - o.total_waste) > CASE WHEN ABS(o.total_waste) < 0.000001 THEN 0.000001 ELSE ABS(o.total_waste) * 0.01 END
            ) discrepancies""", (dataset_id,))
            discrepancy_count = int(cursor.fetchone()[0] or 0)
    safe_totals = not quality["incompatible"] and not quality["duplicates"] and category_units <= 1
    result = []
    for row in rows:
        records = int(row[3] or 0)
        unit_ok = int(row[4] or 0) == 1 and int(row[6] or 0) == 1 and row[2] == meta["unit"] and meta["unit"] != "unknown"
        result.append({"name": row[0], "total": float(row[1]) if row[1] is not None and safe_totals else None,
                       "unit": row[2], "records": records, "missing_records": max(0, quality["records"] - records), "zero_records": int(row[5] or 0),
                       "share_percent": round(100 * float(row[1]) / waste_total, 2) if full_category_coverage and unit_ok and waste_total and row[1] is not None else None})
    return {"categories": result, "total_waste": waste_total,
            "category_count": category_types, "categories_truncated": category_types > len(rows),
            "comparison": {"available": discrepancy_count is not None, "discrepant_records": discrepancy_count,
                           "tolerance": "1% per record (or 0.000001 for a zero total)"},
            "limitations": (["Category totals withheld because duplicate or incompatible period/geography records were detected."] if not safe_totals else []) + (["Category share requires complete category coverage and a known matching unit."] if rows and not full_category_coverage else [])}


def in_memory_overview(dataset_id):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"]
    meta = ds["meta"]
    stats = ds["general_stats"]
    cats = [
        {"name": c["name"], "total": c["total"], "unit": meta["unit"], "records": len(frame), "missing_records": 0, "zero_records": 0, "share_percent": c.get("share")}
        for c in stats.get("category_breakdown", [])
    ]
    return {
        "dataset": meta,
        "record_count": stats["row_count"],
        "waste_record_count": stats["row_count"],
        "area_count": stats["unique_wards"],
        "date_from": stats["date_range"].get("from"),
        "date_to": stats["date_range"].get("to"),
        "waste_total": stats["total_waste"],
        "categories": cats,
        "category_count": len(cats),
        "categories_truncated": False,
        "category_total_waste": stats["total_waste"],
        "category_comparison": {"available": True, "discrepant_records": 0, "tolerance": "1%"},
        "limitations": []
    }


def in_memory_areas(dataset_id, start=None, end=None, search=None, sort="total"):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"]
    meta = ds["meta"]
    area_col = ds.get("area_col") or "area_name"
    total_col = ds.get("total_col") or "total_waste"
    date_col = ds.get("date_col") or "observation_date"
    
    df = frame.copy()
    df.columns = [str(c).strip() for c in df.columns]
    
    if date_col in df.columns:
        if start:
            df = df[df[date_col].astype(str) >= str(start)]
        if end:
            df = df[df[date_col].astype(str) <= str(end)]
            
    if area_col not in df.columns:
        return {"supported": False, "reason": "No area or ward column found in dataset."}
        
    if total_col in df.columns:
        df["_amount"] = pd.to_numeric(df[total_col].astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)
    else:
        df["_amount"] = 1.0
        
    grouped = df.groupby(area_col).agg(total=("_amount", "sum"), records=("_amount", "count")).reset_index()
    
    if search:
        grouped = grouped[grouped[area_col].astype(str).str.lower().str.contains(search.lower())]
        
    if sort == "total":
        grouped = grouped.sort_values(by="total", ascending=False)
    elif sort == "records":
        grouped = grouped.sort_values(by="records", ascending=False)
    elif sort == "name":
        grouped = grouped.sort_values(by=area_col, ascending=True)
        
    rows = [
        {"geography": meta.get("geography_system", "BBMP 2022"), "identifier_type": "area_name", "area": str(r[area_col]), "total": round(float(r["total"]), 2), "records": int(r["records"]), "records_with_amount": int(r["records"])}
        for _, r in grouped.iterrows()
    ]
    return {
        "supported": True,
        "dataset": meta,
        "unit": meta["unit"],
        "date_from": start,
        "date_to": end,
        "rows": rows[:500],
        "truncated": len(rows) > 500,
        "warning": None,
        "date_filter_available": True
    }


def in_memory_categories(dataset_id):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"]
    meta = ds["meta"]
    stats = ds["general_stats"]
    cats = [
        {"name": c["name"], "total": c["total"], "unit": meta["unit"], "records": len(frame), "missing_records": 0, "zero_records": 0, "share_percent": c.get("share")}
        for c in stats.get("category_breakdown", [])
    ]
    return {
        "supported": bool(cats),
        "dataset": meta,
        "categories": cats,
        "category_count": len(cats),
        "categories_truncated": False,
        "total_waste": stats["total_waste"],
        "comparison": {"available": True, "discrepant_records": 0, "tolerance": "1%"},
        "limitations": []
    }


def in_memory_trends(dataset_id, grain="month", start=None, end=None):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"]
    meta = ds["meta"]
    date_col = ds.get("date_col") or "observation_date"
    total_col = ds.get("total_col") or "total_waste"
    
    if date_col not in frame.columns:
        return {"supported": False, "reason": "No date column found in dataset."}
        
    df = frame.copy()
    df["_date"] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=["_date"])
    
    if start:
        df = df[df["_date"] >= pd.to_datetime(start)]
    if end:
        df = df[df["_date"] <= pd.to_datetime(end)]
        
    if df.empty:
        return {"supported": False, "reason": "No records in selected date range."}
        
    if total_col in df.columns:
        df["_amount"] = pd.to_numeric(df[total_col].astype(str).str.replace(",", "", regex=False), errors="coerce").fillna(0)
    else:
        df["_amount"] = 1.0
        
    if grain == "day":
        df["_period"] = df["_date"].dt.strftime("%Y-%m-%d")
    elif grain == "year":
        df["_period"] = df["_date"].dt.strftime("%Y")
    else:
        df["_period"] = df["_date"].dt.strftime("%Y-%m")
        
    grouped = df.groupby("_period").agg(total=("_amount", "sum"), records=("_amount", "count")).reset_index().sort_values(by="_period")
    points = [
        {"period": str(r["_period"]), "total": round(float(r["total"]), 2), "records": int(r["records"]), "records_with_amount": int(r["records"]), "gap": False}
        for _, r in grouped.iterrows()
    ]
    min_date = df["_date"].min().date()
    max_date = df["_date"].max().date()
    points = fill_trend_gaps(points, min_date, max_date, grain)
    return {
        "supported": True,
        "dataset": meta,
        "unit": meta["unit"],
        "grain": grain,
        "date_from": str(min_date),
        "date_to": str(max_date),
        "points": points,
        "date_filter_available": True,
        "gaps_inserted": any(p["gap"] for p in points),
        "empty_periods": sum(1 for p in points if p["gap"])
    }


def in_memory_clustering_features(dataset_id):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"]
    meta = ds["meta"]
    features = []
    total_col = ds.get("total_col")
    if total_col and total_col in frame.columns:
        features.append({"id": "total_waste", "label": f"Total waste ({meta['unit']})"})
    for cat in ds.get("category_cols", []):
        features.append({"id": f"category:{cat}", "label": f"{cat} ({meta['unit']})"})
    return {
        "supported": bool(features),
        "reason": None if features else "No numeric waste features found.",
        "dataset": meta,
        "records": len(frame),
        "features": features,
        "maximum_rows": 5000,
        "features_truncated": False
    }


def in_memory_clustering_run(dataset_id, features, k):
    ds = in_memory_datasets[dataset_id]
    frame = ds["frame"].copy()
    meta = ds["meta"]
    
    col_map = {}
    for f in features:
        if f == "total_waste":
            col_map[f] = ds.get("total_col") or "total_waste"
        elif f.startswith("category:"):
            col_map[f] = f.split(":", 1)[1]
            
    for f, col in col_map.items():
        if col not in frame.columns:
            return {"error": f"Feature {f} not in dataset"}, 400
        frame[col] = pd.to_numeric(frame[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
        
    sub = frame[[col_map[f] for f in features]].dropna()
    sub.columns = features
    if len(sub) <= k:
        return {"supported": False, "reason": f"Only {len(sub)} complete rows available; need at least {k + 1}."}
        
    standardized, labels = fit_kmeans(sub, k)
    if labels is None:
        return {"supported": False, "reason": "Fewer distinct observations than clusters."}
        
    area_col = ds.get("area_col") or "area_name"
    ward_col = ds.get("ward_col") or "ward_id"
    date_col = ds.get("date_col") or "observation_date"
    
    summaries, assignments = [], []
    for cluster_id in sorted(set(labels)):
        idx = [i for i, l in enumerate(labels) if l == cluster_id]
        means = {f: round(float(sub.iloc[idx][f].mean()), 4) for f in features}
        summaries.append({"cluster": int(cluster_id) + 1, "label": f"Cluster {cluster_id + 1} Profile", "size": len(idx), "means": means})
        for i in idx:
            orig_row = frame.iloc[sub.index[i]]
            assignments.append({
                "record_index": int(sub.index[i]) + 1,
                "area_name": str(orig_row.get(area_col, "")) if area_col in orig_row else None,
                "ward_id": str(orig_row.get(ward_col, "")) if ward_col in orig_row else None,
                "date": str(orig_row.get(date_col, "")) if date_col in orig_row else None,
                "cluster": int(cluster_id) + 1
            })
            
    return {
        "supported": True,
        "dataset": meta,
        "clusters": k,
        "features": features,
        "random_seed": 42,
        "scaling": "StandardScaler; complete cases only",
        "dataset_records": len(frame),
        "retrieved_rows": len(sub),
        "usable_rows": len(sub),
        "excluded_rows": len(frame) - len(sub),
        "truncated": False,
        "note": "Deterministic K-means clusters fitted over in-memory observations.",
        "summaries": summaries,
        "assignments": assignments
    }


@app.get("/api/overview")
def overview():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if not dataset_id:
        return jsonify(error="Select an imported dataset.", hive=hive_status()), 400
    if dataset_id in in_memory_datasets:
        return jsonify(in_memory_overview(dataset_id))
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            ensure_schema(cur)
            meta = load_dataset_metadata(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            quality = quality_snapshot(cur, dataset_id)
            cur.execute(f"SELECT SUM(total_waste) FROM {TABLE} WHERE dataset_id = %s", (dataset_id,))
            total_value = cur.fetchone()[0]
            total = float(total_value) if total_value is not None and not quality["incompatible"] and not quality["duplicates"] else None
            cats = category_results(cur, dataset_id, quality, meta)
        return jsonify({"dataset": meta, "record_count": quality["records"],
            "waste_record_count": quality["records"] - quality["missing_total"],
            "area_count": None if quality["geography_count"] > 1 or quality["mixed_area_kinds"] else quality["area_count"],
            "date_from": str(quality["date_from"]) if quality["date_from"] else None,
            "date_to": str(quality["date_to"]) if quality["date_to"] else None,
            "waste_total": total, "categories": cats["categories"], "category_count": cats["category_count"],
            "categories_truncated": cats["categories_truncated"],
            "category_total_waste": cats["total_waste"], "category_comparison": cats["comparison"],
            "limitations": ((["Potential duplicate observations detected; totals withheld."] if quality["duplicates"] else []) +
                (["Some records lack keys needed to rule out duplicate observations; totals withheld."] if quality["missing_keys"] else []) +
                (["Multiple periods, geography systems, or area identifier types detected; totals withheld."] if quality["incompatible"] and not quality["missing_keys"] else []) +
                (["Area count withheld because area names and ward IDs are mixed."] if quality["mixed_area_kinds"] else []) +
                (["Units are unknown; do not compare this dataset with others."] if meta["unit"] == "unknown" else []) + cats["limitations"])})
    except Exception as exc:
        return jsonify(error=f"Hive query failed: {str(exc)[:300]}", hive_verified=False), 503


def date_filter_args(args):
    start, end = args.get("date_from"), args.get("date_to")
    try:
        if start:
            datetime.strptime(start, "%Y-%m-%d")
        if end:
            datetime.strptime(end, "%Y-%m-%d")
        if start and end and start > end:
            return None, None, "Start date must be on or before end date."
    except ValueError:
        return None, None, "Dates must use YYYY-MM-DD format."
    return start, end, None


def fill_trend_gaps(points, start, end, grain):
    observed = {point["period"]: point for point in points}
    first, last = datetime.strptime(str(start), "%Y-%m-%d").date(), datetime.strptime(str(end), "%Y-%m-%d").date()
    periods = []
    if grain == "day":
        current = first
        while current <= last:
            periods.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
    elif grain == "month":
        year, month = first.year, first.month
        while (year, month) <= (last.year, last.month):
            periods.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    else:
        periods = [str(year) for year in range(first.year, last.year + 1)]
    series = [observed.get(period, {"period": period, "total": None, "records": 0, "records_with_amount": 0, "gap": True}) for period in periods]
    return series


def fit_kmeans(feature_matrix, clusters):
    scaler = StandardScaler()
    standardized = scaler.fit_transform(feature_matrix.to_numpy(dtype=float))
    if len(pd.DataFrame(standardized).drop_duplicates()) < clusters:
        return standardized, None
    model = KMeans(n_clusters=clusters, random_state=42, n_init=10)
    return standardized, model.fit_predict(standardized)


def open_analytics_dataset(cursor, dataset_id):
    ensure_schema(cursor)
    meta = load_dataset_metadata(cursor, dataset_id)
    if not meta:
        return None, None
    return meta, quality_snapshot(cursor, dataset_id)


@app.get("/api/analytics/areas")
def area_analytics():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if not dataset_id:
        return jsonify(error="Select a dataset imported into Hive."), 400
    start, end, date_error = date_filter_args(request.args)
    if date_error:
        return jsonify(error=date_error), 400
    search = (request.args.get("q") or "").strip()[:80]
    sort = request.args.get("sort", "total")
    if dataset_id in in_memory_datasets:
        return jsonify(in_memory_areas(dataset_id, start, end, search, sort))
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            meta, whole = open_analytics_dataset(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            if (start or end) and not whole["date_count"]:
                return jsonify(supported=False, reason="This dataset has no usable observation dates for a time filter."), 200
            quality = quality_snapshot(cur, dataset_id, start, end)
            if not quality["records"]:
                return jsonify(supported=False, reason="No records fall in the selected date range."), 200
            if quality["area_count"] == 0:
                return jsonify(supported=False, reason="No area name or ward identifier was imported."), 200
            if quality["duplicates"] or quality["incompatible"] or quality["mixed_area_kinds"]:
                return jsonify(supported=False, reason="Area ranking is withheld because duplicate keys, missing comparison keys, mixed periods/geography systems, or mixed area identifier types make aggregation unsafe."), 200
            where, params = scoped_where(dataset_id, start, end)
            if search:
                where += " AND LOWER(COALESCE(area_name, ward_id)) LIKE %s"
                params += ("%" + search.lower().replace("%", "\\%").replace("_", "\\_") + "%",)
            cur.execute(f"""SELECT geography_system,
                CASE WHEN area_name IS NOT NULL THEN 'area_name' ELSE 'ward_id' END AS identifier_type,
                COALESCE(area_name, ward_id) AS area, SUM(total_waste), COUNT(*), COUNT(total_waste)
                FROM {TABLE} WHERE {where}
                GROUP BY geography_system, CASE WHEN area_name IS NOT NULL THEN 'area_name' ELSE 'ward_id' END, COALESCE(area_name, ward_id)
                ORDER BY SUM(total_waste) DESC LIMIT 501""", params)
            rows = cur.fetchall()
        truncated = len(rows) > 500
        rows = rows[:500]
        warning = "Ward geography is unknown; this ranking is only meaningful within this dataset." if meta["geography_system"] in ("unknown", "mixed") else None
        return jsonify(supported=True, dataset=meta, unit=meta["unit"], date_from=start, date_to=end,
            rows=[{"geography": r[0], "identifier_type": r[1], "area": r[2], "total": float(r[3]) if r[3] is not None else None,
                   "records": int(r[4]), "records_with_amount": int(r[5])} for r in rows],
            truncated=truncated, warning=warning, date_filter_available=bool(whole["date_count"] and not whole["incompatible"]))
    except Exception as exc:
        return jsonify(error=f"Hive area query failed: {str(exc)[:300]}", hive_verified=False), 503


@app.get("/api/analytics/categories")
def category_analytics():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if not dataset_id:
        return jsonify(error="Select a dataset imported into Hive."), 400
    if dataset_id in in_memory_datasets:
        return jsonify(in_memory_categories(dataset_id))
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            meta, quality = open_analytics_dataset(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            result = category_results(cur, dataset_id, quality, meta)
        return jsonify(supported=bool(result["categories"]), dataset=meta, **result)
    except Exception as exc:
        return jsonify(error=f"Hive category query failed: {str(exc)[:300]}", hive_verified=False), 503


@app.get("/api/analytics/trends")
def trend_analytics():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if not dataset_id:
        return jsonify(error="Select a dataset imported into Hive."), 400
    start, end, date_error = date_filter_args(request.args)
    if date_error:
        return jsonify(error=date_error), 400
    grain = request.args.get("grain", "month")
    grain_sql = {"day": "CAST(observation_date AS STRING)", "month": "DATE_FORMAT(CAST(observation_date AS TIMESTAMP), 'yyyy-MM')",
                 "year": "DATE_FORMAT(CAST(observation_date AS TIMESTAMP), 'yyyy')"}
    if grain not in grain_sql:
        return jsonify(error="Aggregation must be day, month, or year."), 400
    if dataset_id in in_memory_datasets:
        return jsonify(in_memory_trends(dataset_id, grain, start, end))
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            meta, whole = open_analytics_dataset(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            if not whole["date_count"]:
                return jsonify(supported=False, reason="Time trends need imported observation dates. Period labels are not a standard date frequency in this dataset."), 200
            quality = quality_snapshot(cur, dataset_id, start, end)
            if quality["incompatible"] or quality["duplicates"]:
                return jsonify(supported=False, reason="Trend totals are withheld because observations have duplicate keys or incompatible period/geography coverage."), 200
            if not quality["date_from"] or not quality["date_to"]:
                return jsonify(supported=False, reason="The selected rows do not have a usable date range."), 200
            if grain == "day" and (quality["date_to"] - quality["date_from"]).days > 370:
                return jsonify(supported=False, reason="Daily view is limited to a 370-day span. Choose monthly or yearly aggregation."), 200
            where, params = scoped_where(dataset_id, start, end)
            cur.execute(f"""SELECT {grain_sql[grain]} AS period_start, SUM(total_waste), COUNT(*), COUNT(total_waste)
                FROM {TABLE} WHERE {where} AND observation_date IS NOT NULL
                GROUP BY {grain_sql[grain]} ORDER BY period_start LIMIT 801""", params)
            rows = cur.fetchall()
        if len(rows) > 800:
            return jsonify(supported=False, reason="This selection contains more than 800 periods. Use a coarser aggregation."), 200
        points = [{"period": str(r[0]), "total": float(r[1]) if r[1] is not None else None,
                   "records": int(r[2]), "records_with_amount": int(r[3]), "gap": False} for r in rows]
        points = fill_trend_gaps(points, quality["date_from"], quality["date_to"], grain)
        return jsonify(supported=True, dataset=meta, unit=meta["unit"], grain=grain, date_from=str(quality["date_from"]),
                       date_to=str(quality["date_to"]), points=points,
                       date_filter_available=True, gaps_inserted=any(point["gap"] for point in points), empty_periods=sum(1 for point in points if point["gap"]))
    except Exception as exc:
        return jsonify(error=f"Hive trend query failed: {str(exc)[:300]}", hive_verified=False), 503


@app.get("/api/analytics/clustering/features")
def clustering_features():
    dataset_id = dataset_id_arg(request.args.get("dataset_id"))
    if not dataset_id:
        return jsonify(error="Select a dataset imported into Hive."), 400
    if dataset_id in in_memory_datasets:
        return jsonify(in_memory_clustering_features(dataset_id))
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            meta, quality = open_analytics_dataset(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            cur.execute(f"""SELECT DISTINCT category_name FROM (
                SELECT category_name FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s
                UNION SELECT category_name FROM {CATEGORY_TABLE} WHERE dataset_id = %s
            ) inventory ORDER BY category_name LIMIT 101""", (dataset_id, dataset_id))
            category_rows = cur.fetchall()
            categories = [r[0] for r in category_rows[:100]]
        features = ([{"id": "total_waste", "label": f"Total waste ({meta['unit']})"}] if meta["unit"] != "unknown" else [])
        features.extend({"id": "category:" + c, "label": c + " (" + meta["unit"] + ")"} for c in categories if meta["unit"] != "unknown")
        if quality["incompatible"] or quality["duplicates"]:
            return jsonify(supported=False, reason="Clustering is disabled because duplicate keys or incompatible periods/geography systems were detected.", features=features)
        return jsonify(supported=bool(features), reason=None if features else "Clustering needs a numeric waste or mapped material field with a known unit.",
                       dataset=meta, records=quality["records"], features=features, maximum_rows=5000,
                       features_truncated=len(category_rows) > 100)
    except Exception as exc:
        return jsonify(error=f"Hive feature query failed: {str(exc)[:300]}", hive_verified=False), 503


@app.post("/api/analytics/clustering/run")
def clustering_run():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Clustering request must be a JSON object."), 400
    dataset_id = dataset_id_arg(body.get("dataset_id"))
    features = body.get("features")
    try:
        k = int(body.get("clusters", 3))
    except (TypeError, ValueError):
        return jsonify(error="Cluster count must be an integer from 2 to 8."), 400
    if not dataset_id or not isinstance(features, list) or not all(isinstance(f, str) for f in features) or not 1 <= len(features) <= 8 or len(set(features)) != len(features) or not 2 <= k <= 8:
        return jsonify(error="Choose a dataset, one to eight unique numeric features, and two to eight clusters."), 400
    if dataset_id in in_memory_datasets:
        res = in_memory_clustering_run(dataset_id, features, k)
        status_code = 400 if isinstance(res, tuple) and res[1] == 400 else 200
        data = res[0] if isinstance(res, tuple) else res
        return jsonify(data), status_code
    try:
        with closing(connect()) as conn:
            cur = conn.cursor()
            meta, quality = open_analytics_dataset(cur, dataset_id)
            if not meta:
                return jsonify(error="Dataset was not found in Hive."), 404
            if quality["incompatible"] or quality["duplicates"]:
                return jsonify(supported=False, reason="Clustering is disabled because duplicate keys or incompatible periods/geography systems were detected."), 200
            cur.execute(f"""SELECT DISTINCT category_name FROM (
                SELECT category_name FROM {CATEGORY_FIELDS_TABLE} WHERE dataset_id = %s
                UNION SELECT category_name FROM {CATEGORY_TABLE} WHERE dataset_id = %s
            ) inventory""", (dataset_id, dataset_id))
            valid_categories = {r[0] for r in cur.fetchall()}
            categories = [f.split(":", 1)[1] for f in features if isinstance(f, str) and f.startswith("category:")]
            if any(f != "total_waste" and not f.startswith("category:") for f in features) or any(c not in valid_categories for c in categories):
                return jsonify(error="One or more selected features are not available in this Hive dataset."), 400
            if "total_waste" in features and meta["unit"] == "unknown":
                return jsonify(error="Total waste has no known unit and cannot be used for clustering."), 400
            aliases = {feature: f"feature_{i}" for i, feature in enumerate(features)}
            category_features = [(feature, aliases[feature], feature.split(":", 1)[1]) for feature in features if feature.startswith("category:")]
            if category_features:
                cases = ", ".join(f"MAX(CASE WHEN category_name = %s THEN amount END) AS {alias}" for _, alias, _ in category_features)
                values = [category for _, _, category in category_features]
                selected = ", ".join(f"c.{aliases[f]}" if f.startswith("category:") else "o.total_waste" for f in features)
                sql = f"""SELECT o.record_index, o.area_name, o.ward_id, CAST(o.observation_date AS STRING), {selected}
                    FROM {TABLE} o LEFT JOIN (SELECT dataset_id, record_index, {cases} FROM {CATEGORY_TABLE}
                    WHERE dataset_id = %s GROUP BY dataset_id, record_index) c
                    ON o.dataset_id = c.dataset_id AND o.record_index = c.record_index
                    WHERE o.dataset_id = %s ORDER BY o.record_index LIMIT 5001"""
                params = tuple(values) + (dataset_id, dataset_id)
            else:
                sql = f"SELECT record_index, area_name, ward_id, CAST(observation_date AS STRING), total_waste FROM {TABLE} WHERE dataset_id = %s ORDER BY record_index LIMIT 5001"
                params = (dataset_id,)
            cur.execute(sql, params)
            rows = cur.fetchall()
        dataset_records = quality["records"]
        if len(rows) > 5000:
            rows = rows[:5000]
            truncated = True
        else:
            truncated = False
        output_rows = []
        for row in rows:
            values = row[4:]
            if any(v is None for v in values):
                continue
            output_rows.append((row, [float(v) for v in values]))
        if len(output_rows) <= k:
            return jsonify(supported=False, reason=f"Only {len(output_rows)} complete rows are available; at least {k + 1} are required.", input_records=len(rows), usable_records=len(output_rows)), 200
        matrix = pd.DataFrame([r[1] for r in output_rows], columns=features)
        if any(matrix[col].nunique() < 2 for col in matrix.columns):
            return jsonify(supported=False, reason="At least one selected feature has no variation; remove it or choose another feature."), 200
        standardized, labels = fit_kmeans(matrix, k)
        if labels is None:
            return jsonify(supported=False, reason="The selected feature profiles contain fewer distinct observations than clusters."), 200
        grand_means = matrix.mean()
        scaled_means = pd.DataFrame(standardized, columns=features)
        assignments, summaries = [], []
        for cluster_id in sorted(set(int(x) for x in labels)):
            indices = [i for i, label in enumerate(labels) if int(label) == cluster_id]
            means = {feature: round(float(matrix.iloc[indices][feature].mean()), 4) for feature in features}
            deltas = [(feature, float(scaled_means.iloc[indices][feature].mean())) for feature in features]
            deltas.sort(key=lambda pair: abs(pair[1]), reverse=True)
            direction = "higher" if deltas[0][1] > 0 else "lower"
            descriptor = f"{direction} {deltas[0][0]} profile" if abs(deltas[0][1]) > 1e-9 else "near-average profile"
            summaries.append({"cluster": cluster_id + 1, "label": descriptor, "size": len(indices), "means": means})
            for i in indices:
                row = output_rows[i][0]
                assignments.append({"record_index": int(row[0]), "area_name": row[1], "ward_id": row[2], "date": str(row[3]) if row[3] else None, "cluster": cluster_id + 1})
        return jsonify(supported=True, dataset=meta, clusters=k, features=features, random_seed=42, scaling="StandardScaler; complete cases only",
            dataset_records=dataset_records, retrieved_rows=len(rows), usable_rows=len(output_rows), excluded_rows=len(rows) - len(output_rows), truncated=truncated,
            note="Exploratory clusters depend on selected features, scaling, available records, and cluster count. They do not establish causes or recommendations.",
            summaries=summaries, assignments=assignments)
    except Exception as exc:
        return jsonify(error=f"Hive clustering query failed: {str(exc)[:300]}", hive_verified=False), 503


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG", "0") == "1")
