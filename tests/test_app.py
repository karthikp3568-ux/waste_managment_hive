import io
import json
import math
import re
import pytest
import pandas as pd
import numpy as np
from datetime import date
from unittest.mock import MagicMock, patch

import app
from app import (
    app as flask_app,
    parse_number,
    infer_type,
    read_upload,
    preview_frame,
    fit_kmeans,
    dataset_id_arg,
    scoped_where,
    HiveUnavailable,
    uploads,
)


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    app.in_memory_datasets.clear()
    app.uploads.clear()
    with flask_app.test_client() as test_client:
        yield test_client
    app.in_memory_datasets.clear()
    app.uploads.clear()


# ---------------------------------------------------------
# Helper Unit Tests
# ---------------------------------------------------------

def test_parse_number():
    assert parse_number(None) is None
    assert parse_number("") is None
    assert parse_number("  ") is None
    assert parse_number(np.nan) is None
    assert parse_number(123) == 123.0
    assert parse_number("1,234.56") == 1234.56
    assert parse_number("0") == 0.0

    with pytest.raises(ValueError, match="Waste amounts cannot be negative"):
        parse_number("-5")

    with pytest.raises(ValueError, match="Waste amounts must be finite"):
        parse_number("inf")

    with pytest.raises(ValueError):
        parse_number("abc")


def test_infer_type():
    empty_series = pd.Series([None, np.nan])
    assert infer_type(empty_series) == "empty"

    num_series = pd.Series([1, 2, 3])
    assert infer_type(num_series) == "number"

    str_num_series = pd.Series(["1,000", "250.5", "30"])
    assert infer_type(str_num_series) == "number"

    date_series = pd.Series(pd.to_datetime(["2026-01-01", "2026-01-02"]))
    assert infer_type(date_series) == "date"

    date_str_series = pd.Series(["2026-01-01", "2026-01-02", "2026-01-03", "invalid"])
    assert infer_type(date_str_series) == "date-like text"

    text_series = pd.Series(["East Zone", "West Zone", "North Zone"])
    assert infer_type(text_series) == "text"


def test_dataset_id_arg():
    valid_id = "a" * 32
    assert dataset_id_arg(valid_id) == valid_id
    assert dataset_id_arg("short") is None
    assert dataset_id_arg(12345) is None
    assert dataset_id_arg("g" * 32) is None  # not valid hex


def test_scoped_where():
    where, params = scoped_where("abc123", "2026-01-01", "2026-01-31")
    assert "dataset_id = %s" in where
    assert "observation_date >= %s" in where
    assert "observation_date <= %s" in where
    assert params == ("abc123", "2026-01-01", "2026-01-31")


def test_read_upload():
    # Valid CSV
    csv_bytes = io.BytesIO(b"date,area,total_waste\n2026-01-01,Zone A,100\n")
    csv_bytes.filename = "test.csv"
    frame, name = read_upload(csv_bytes)
    assert len(frame) == 1
    assert list(frame.columns) == ["date", "area", "total_waste"]
    assert name == "test.csv"

    # Unsupported extension
    txt_file = io.BytesIO(b"content")
    txt_file.filename = "test.txt"
    with pytest.raises(ValueError, match="Unsupported file"):
        read_upload(txt_file)

    # Empty file
    empty_file = io.BytesIO(b"")
    empty_file.filename = "empty.csv"
    with pytest.raises(Exception):
        read_upload(empty_file)

    # Duplicate column names when stripped ("area", "area ")
    dup_file = io.BytesIO(b"area,area \nZone A,Zone B\n")
    dup_file.filename = "dup.csv"
    with pytest.raises(ValueError, match="Column names must be non-empty and unique"):
        read_upload(dup_file)


def test_preview_frame():
    df = pd.DataFrame({
        "Date": ["2026-01-01", "2026-01-02", "2026-01-03"],
        "Area": ["North", "South", "East"],
        "Waste": ["100", "200", "300"]
    })
    preview = preview_frame(df)
    assert preview["row_count"] == 3
    assert len(preview["columns"]) == 3
    assert preview["date_range"] is not None
    assert preview["date_range"]["from"] == "2026-01-01"
    assert preview["date_range"]["to"] == "2026-01-03"


def test_fit_kmeans():
    df = pd.DataFrame({
        "feat1": [1.0, 2.0, 3.0, 10.0, 11.0, 12.0],
        "feat2": [2.0, 3.0, 1.0, 12.0, 10.0, 11.0],
    })
    standardized, labels = fit_kmeans(df, clusters=2)
    assert standardized.shape == (6, 2)
    assert len(labels) == 6
    assert set(labels) == {0, 1}

    # Too few distinct observations for clusters
    df_identical = pd.DataFrame({
        "feat1": [1.0, 1.0, 1.0],
        "feat2": [1.0, 1.0, 1.0],
    })
    _, labels_none = fit_kmeans(df_identical, clusters=3)
    assert labels_none is None


# ---------------------------------------------------------
# Flask Route Tests (Base functionality)
# ---------------------------------------------------------

def test_index_route(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Waste intelligence | Bengaluru" in response.data
    assert b"FIELDNOTE" in response.data


def test_static_files(client):
    for asset in ["styles.css", "modern.css", "app.js"]:
        response = client.get(f"/static/{asset}")
        assert response.status_code == 200


def test_api_status_hive_down(client):
    with patch("app.connect", side_effect=HiveUnavailable("Connection refused")):
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.get_json()
        assert data["hive"]["connected"] is False
        assert "Hive unavailable" in data["hive"]["message"]


def test_api_datasets_hive_down(client):
    with patch("app.connect", side_effect=HiveUnavailable("Connection refused")):
        response = client.get("/api/datasets")
        assert response.status_code == 503
        data = response.get_json()
        assert "Hive unavailable" in data["error"]


def test_api_upload_and_validate_flow(client):
    # Upload without file
    res = client.post("/api/upload", data={})
    assert res.status_code == 400

    # Upload valid CSV
    csv_data = b"record_date,ward,dry_waste,wet_waste,total\n2026-03-01,W01,10.5,20.5,31.0\n2026-03-02,W02,12.0,22.0,34.0\n"
    res = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(csv_data), "bengaluru_waste_sample.csv")},
        content_type="multipart/form-data"
    )
    assert res.status_code == 200
    upload_res = res.get_json()
    token = upload_res["token"]
    assert "columns" in upload_res
    assert upload_res["row_count"] == 2

    # Validate with missing token
    res = client.post("/api/validate", json={"token": "nonexistent"})
    assert res.status_code == 400

    # Validate with invalid mapping (no total waste or category) -> returns 200 with can_import: False and errors
    res = client.post("/api/validate", json={
        "token": token,
        "mapping": {"ward_id": "ward"},
        "unit": "tonnes",
        "geography_system": "BBMP 2022"
    })
    assert res.status_code == 200
    val_err_data = res.get_json()
    assert val_err_data["can_import"] is False
    assert any("Map total waste or at least one material category" in err for err in val_err_data["errors"])

    # Validate with correct mapping
    res = client.post("/api/validate", json={
        "token": token,
        "mapping": {
            "observation_date": "record_date",
            "ward_id": "ward",
            "total_waste": "total",
            "category:Dry Waste": "dry_waste",
            "category:Wet Waste": "wet_waste"
        },
        "unit": "tonnes",
        "geography_system": "BBMP 2022"
    })
    assert res.status_code == 200
    val_data = res.get_json()
    assert val_data["can_import"] is True
    assert val_data["valid_records"] == 2
    assert val_data["flagged_count"] == 0


def test_api_analytics_endpoints_require_valid_dataset_id(client):
    endpoints = [
        "/api/overview",
        "/api/analytics/areas",
        "/api/analytics/categories",
        "/api/analytics/trends",
        "/api/analytics/clustering/features",
    ]
    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 400, f"Expected 400 for {ep} without dataset_id"
        res_invalid = client.get(f"{ep}?dataset_id=invalid")
        assert res_invalid.status_code == 400, f"Expected 400 for {ep} with invalid dataset_id"


def test_api_clustering_run_validation(client):
    # Non-dict json
    res = client.post("/api/analytics/clustering/run", data="not json", content_type="application/json")
    assert res.status_code == 400

    # Missing dataset_id
    res = client.post("/api/analytics/clustering/run", json={"features": ["total_waste"], "clusters": 3})
    assert res.status_code == 400

    # Invalid clusters
    valid_id = "0" * 32
    res = client.post("/api/analytics/clustering/run", json={"dataset_id": valid_id, "features": ["total_waste"], "clusters": 20})
    assert res.status_code == 400


# ---------------------------------------------------------
# Mocked Hive Database Tests
# ---------------------------------------------------------

def test_mocked_hive_datasets(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "1" * 32
    mock_cur.fetchall.return_value = [
        (valid_id, "waste_records_2026.csv", "tonnes", "BBMP 2022", 0)
    ]

    with patch("app.connect", return_value=mock_conn):
        res = client.get("/api/datasets")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data) == 1
        assert data[0]["id"] == valid_id
        assert data[0]["name"] == "waste_records_2026.csv"
        assert data[0]["unit"] == "tonnes"


def test_mocked_hive_overview(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "a" * 32

    with patch("app.connect", return_value=mock_conn), \
         patch("app.load_dataset_metadata", return_value={"id": valid_id, "name": "waste.csv", "unit": "tonnes", "geography_system": "BBMP 2022", "flagged": 0}), \
         patch("app.quality_snapshot", return_value={
             "records": 10, "period_count": 1, "geography_count": 1, "missing_periods": 0, "missing_total": 0,
             "date_from": "2026-03-01", "date_to": "2026-03-10", "date_count": 10, "area_count": 3,
             "area_kind_count": 1, "duplicates": False, "missing_keys": False, "incompatible": False, "mixed_area_kinds": False
         }), \
         patch("app.category_results", return_value={
             "categories": [{"name": "Dry Waste", "total": 200.0, "unit": "tonnes", "records": 10, "missing_records": 0, "zero_records": 0, "share_percent": 40.0}],
             "total_waste": 500.0, "category_count": 1, "categories_truncated": False,
             "comparison": {"available": True, "discrepant_records": 0, "tolerance": "1%"},
             "limitations": []
         }):
        mock_cur.fetchone.return_value = (500.0,)
        res = client.get(f"/api/overview?dataset_id={valid_id}")
        assert res.status_code == 200
        data = res.get_json()
        assert data["dataset"]["id"] == valid_id
        assert data["record_count"] == 10
        assert data["waste_total"] == 500.0


def test_mocked_hive_areas(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "b" * 32

    with patch("app.connect", return_value=mock_conn), \
         patch("app.open_analytics_dataset", return_value=(
             {"id": valid_id, "name": "waste.csv", "unit": "tonnes", "geography_system": "BBMP 2022"},
             {"date_count": 5, "incompatible": False}
         )), \
         patch("app.quality_snapshot", return_value={
             "records": 10, "area_count": 2, "duplicates": False, "incompatible": False, "mixed_area_kinds": False
         }):
        mock_cur.fetchall.return_value = [
            ("BBMP 2022", "ward_id", "Ward 1", 150.0, 5, 5),
            ("BBMP 2022", "ward_id", "Ward 2", 120.0, 5, 5),
        ]
        res = client.get(f"/api/analytics/areas?dataset_id={valid_id}")
        assert res.status_code == 200
        data = res.get_json()
        assert data["supported"] is True
        assert len(data["rows"]) == 2
        assert data["rows"][0]["area"] == "Ward 1"


def test_mocked_hive_trends(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "c" * 32

    with patch("app.connect", return_value=mock_conn), \
         patch("app.open_analytics_dataset", return_value=(
             {"id": valid_id, "name": "waste.csv", "unit": "tonnes", "geography_system": "BBMP 2022"},
             {"date_count": 5, "incompatible": False, "duplicates": False}
         )), \
         patch("app.quality_snapshot", return_value={
             "records": 10, "date_count": 5, "duplicates": False, "incompatible": False,
             "date_from": date(2026, 3, 1), "date_to": date(2026, 3, 5)
         }):
        mock_cur.fetchall.return_value = [
            ("2026-03-01", 50.0, 2, 2),
            ("2026-03-02", 60.0, 2, 2),
        ]
        res = client.get(f"/api/analytics/trends?dataset_id={valid_id}&grain=day")
        assert res.status_code == 200
        data = res.get_json()
        assert data["supported"] is True
        assert "points" in data


def test_mocked_hive_clustering_features(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "d" * 32

    with patch("app.connect", return_value=mock_conn), \
         patch("app.open_analytics_dataset", return_value=(
             {"id": valid_id, "name": "waste.csv", "unit": "tonnes", "geography_system": "BBMP 2022"},
             {"records": 10, "incompatible": False, "duplicates": False}
         )):
        mock_cur.fetchall.return_value = [("Dry Waste",), ("Wet Waste",)]
        res = client.get(f"/api/analytics/clustering/features?dataset_id={valid_id}")
        assert res.status_code == 200
        data = res.get_json()
        assert data["supported"] is True
        assert len(data["features"]) == 3  # total_waste + 2 categories


def test_mocked_hive_clustering_run(client):
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    valid_id = "e" * 32

    with patch("app.connect", return_value=mock_conn), \
         patch("app.open_analytics_dataset", return_value=(
             {"id": valid_id, "name": "waste.csv", "unit": "tonnes", "geography_system": "BBMP 2022"},
             {"records": 6, "incompatible": False, "duplicates": False}
         )):
        # valid categories check fetchall
        # then data rows fetchall: (record_index, area_name, ward_id, date, total_waste)
        mock_cur.fetchall.side_effect = [
            [], # valid_categories
            [
                (1, "Zone 1", "W1", "2026-03-01", 10.0),
                (2, "Zone 2", "W2", "2026-03-01", 20.0),
                (3, "Zone 3", "W3", "2026-03-01", 30.0),
                (4, "Zone 4", "W4", "2026-03-01", 40.0),
                (5, "Zone 5", "W5", "2026-03-01", 50.0),
                (6, "Zone 6", "W6", "2026-03-01", 60.0),
            ]
        ]
        res = client.post("/api/analytics/clustering/run", json={
            "dataset_id": valid_id,
            "features": ["total_waste"],
            "clusters": 2
        })
        assert res.status_code == 200
        data = res.get_json()
        assert data["supported"] is True
        assert len(data["summaries"]) == 2
        assert len(data["assignments"]) == 6


def test_api_sample_route(client):
    res = client.get("/api/sample")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "loaded"
    assert "Bengaluru" in data["name"]
    assert data["dataset_id"] in app.in_memory_datasets


def test_in_memory_analytics_filter(client):
    # Load sample first
    client.get("/api/sample")
    d_id = list(app.in_memory_datasets.keys())[0]

    # Test get general stats
    res_stats = client.get(f"/api/analytics/general_stats?dataset_id={d_id}")
    assert res_stats.status_code == 200
    stats = res_stats.get_json()
    assert stats["row_count"] == 40
    assert stats["unique_wards"] == 8
    assert len(stats["area_breakdown"]) == 8

    # Test filtering by ward
    res_filter = client.post("/api/analytics/filter", json={
        "dataset_id": d_id,
        "ward": "Koramangala"
    })
    assert res_filter.status_code == 200
    filt = res_filter.get_json()
    assert filt["unique_wards"] == 1
    assert filt["top_ward"]["name"] == "Koramangala"


