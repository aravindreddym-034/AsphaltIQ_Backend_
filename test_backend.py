"""Run: python -m unittest -v test_backend.py (requires requirements-dev.txt)."""
import csv
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from core import DataError, MAX_FILE_BYTES, detect, load_recording
from main import create_app

KEY = "test-key-for-automated-checks-only"
BASE = 1_791_815_880_000_000_000


def csv_bytes(rows):
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue().encode()


def fixtures():
    # Explicit test fixtures, never bundled or presented as road measurements.
    acceleration = [["time", "seconds_elapsed", "z", "y", "x"]]
    for i in range(600):
        acceleration.append([str(BASE + i * 10_000_000), i / 100, 10 if 200 <= i < 250 else 0.1, 0, 0])
    gps = [["time", "latitude", "longitude", "horizontalAccuracy"]]
    for i in range(7):
        gps.append([str(BASE + i * 1_000_000_000), 17 + i / 10000, 78, 5])
    return csv_bytes(acceleration), csv_bytes(gps)


class BackendTests(unittest.TestCase):
    def setUp(self):
        # Keep test-only databases outside the deliverable and away from real recordings.
        root = Path(os.environ.get("ASPHALTIQ_TEST_DIR", Path(__file__).parent.parent / "work" / "backend_tests"))
        self.directory = root / str(uuid4())
        self.client = TestClient(create_app(self.directory, KEY))
        self.client.__enter__()
        self.headers = {"X-Team-Key": KEY}
        self.accel, self.gps = fixtures()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def upload(self, **fields):
        return self.client.post("/api/recordings", headers=self.headers,
                                files={"acceleration": ("Accelerometer.csv", self.accel, "text/csv"),
                                       "location": ("Location.csv", self.gps, "text/csv")},
                                data={"name": "Test trip", "threshold": "2", **fields})

    def test_authentication_before_upload(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/recordings").status_code, 401)
        self.assertEqual(self.client.post("/api/recordings", content=b"bad").status_code, 401)
        self.assertEqual(self.client.get("/api/recordings", headers={"X-Team-Key": "wrong"}).status_code, 401)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("APIKeyHeader", schema["components"]["securitySchemes"])

    def test_cors_allows_only_configured_origin(self):
        with patch.dict(os.environ, {"ASPHALTIQ_CORS_ORIGINS": "https://team-example.netlify.app"}):
            with TestClient(create_app(self.directory, KEY)) as client:
                allowed = client.options("/api/recordings", headers={
                    "Origin": "https://team-example.netlify.app",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "x-team-key"})
                self.assertEqual(allowed.status_code, 200)
                self.assertEqual(allowed.headers["access-control-allow-origin"], "https://team-example.netlify.app")
                rejected = client.options("/api/recordings", headers={
                    "Origin": "https://unrelated.example", "Access-Control-Request-Method": "POST"})
                self.assertEqual(rejected.status_code, 400)
                error = client.get("/api/recordings", headers={"Origin": "https://team-example.netlify.app"})
                self.assertEqual(error.status_code, 401)
                self.assertEqual(error.headers["access-control-allow-origin"], "https://team-example.netlify.app")

    def test_upload_persist_reopen_geojson_export_delete(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        summary = response.json()
        self.assertEqual(summary["acceleration_readings"], 600)
        self.assertEqual(summary["accepted_gps_fixes"], 6)
        self.assertEqual(summary["matched_readings"], 501)
        self.assertEqual(summary["candidate_events"], 1)
        rid = summary["id"]
        listed = self.client.get("/api/recordings", headers=self.headers).json()
        self.assertEqual(listed["total"], 1)
        data = self.client.get(f"/api/recordings/{rid}/data", headers=self.headers).json()
        self.assertEqual(data["samples"][1]["timestamp"], str(BASE + 10_000_000))
        self.assertEqual(data["samples"][1]["t"], 0.01)
        events = self.client.get(f"/api/recordings/{rid}/events.geojson", headers=self.headers).json()
        self.assertEqual(len(events["features"]), 1)
        self.assertEqual(events["features"][0]["geometry"]["coordinates"][0], 78)
        self.assertEqual(events["features"][0]["properties"]["status"], "unverified_vibration_candidate")
        exported = self.client.get(f"/api/recordings/{rid}/events.csv", headers=self.headers)
        self.assertEqual(len(list(csv.reader(io.StringIO(exported.text)))), 2)
        # Fresh app instance sees SQLite data, without an in-memory cache.
        with TestClient(create_app(self.directory, KEY)) as second:
            self.assertEqual(second.get(f"/api/recordings/{rid}", headers=self.headers).json()["id"], rid)
        self.assertEqual(self.client.delete(f"/api/recordings/{rid}", headers=self.headers).status_code, 204)
        self.assertEqual(self.client.get(f"/api/recordings/{rid}", headers=self.headers).status_code, 404)

    def test_rerun_and_invalid_parameters(self):
        rid = self.upload().json()["id"]
        url = f"/api/recordings/{rid}/detect"
        self.assertEqual(len(self.client.post(url, headers=self.headers, json={"threshold": 20}).json()["candidates"]), 0)
        automatic = self.client.post(url, headers=self.headers, json={"threshold": None}).json()
        self.assertEqual(automatic["threshold_mode"], "automatic")
        self.assertAlmostEqual(automatic["threshold"], 0.6)
        self.assertEqual(len(automatic["candidates"]), 1)
        for data in ({"threshold": 0}, {"threshold": -1}, {"threshold": "NaN"}, {"typo": 1}):
            self.assertEqual(self.client.post(url, headers=self.headers, json=data).status_code, 422)

    def test_reject_bad_schema_without_saving(self):
        self.accel = b"time,something\n123,4\n"
        self.assertEqual(self.upload().status_code, 422)
        self.assertEqual(self.client.get("/api/recordings", headers=self.headers).json()["total"], 0)

    def test_reject_bad_times_and_nonfinite_values(self):
        for axis, ns in (("nan", str(BASE)), ("1", "1791815880"), ("1e300", str(BASE))):
            self.accel = f"time,x,y,z\n{ns},{axis},0,0\n".encode()
            self.assertEqual(self.upload().status_code, 422)

    def test_gps_quality_unknown_and_out_of_span(self):
        gps = csv_bytes([
            ["time", "latitude", "longitude", "horizontalAccuracy"],
            [str(BASE - 1), 17, 78, 5],
            [str(BASE), 17, 78, ""],
            [str(BASE + 1_000_000_000), 17, 78, 51],
            [str(BASE + 2_000_000_000), 17, 78, 50],
            [str(BASE + 3_000_000_000), 17, 78, -1],
            [str(BASE + 4_000_000_000), 99, 78, 5],
            [str(BASE + 5_000_000_000), 17, 78, "bad"],
        ])
        record = load_recording(self.accel, gps, 50)
        self.assertEqual(len(record["fixes"]), 2)
        self.assertIsNone(record["fixes"][0]["accuracy"])
        self.assertEqual(record["summary"]["rejected_gps_rows"], 5)

    def test_no_gps_match_events_retained(self):
        self.gps = csv_bytes([["time", "latitude", "longitude"], [str(BASE - 1), 17, 78]])
        result = self.upload().json()
        self.assertEqual(result["candidate_events"], 1)
        self.assertEqual(result["located_candidates"], 0)
        url = f'/api/recordings/{result["id"]}/events.geojson'
        self.assertEqual(self.client.get(url, headers=self.headers).json()["unlocated_events"], 1)

    def test_gps_time_gap_is_not_extrapolated(self):
        self.gps = csv_bytes([["time", "latitude", "longitude"],
                              [str(BASE), 17, 78], [str(BASE + 5_000_000_000), 17, 78]])
        record = load_recording(self.accel, self.gps)
        # 2.01 to 2.99 is over 2 seconds from both fixes; after 5s is outside the span.
        self.assertEqual(record["summary"]["matched_readings"], 402)

    def test_short_recording_is_saved_without_invented_events(self):
        self.accel = csv_bytes([["time", "x", "y", "z"], [str(BASE), 0, 0, 1]])
        summary = self.upload().json()
        self.assertEqual(summary["detection_status"], "insufficient_data")
        self.assertEqual(summary["candidate_events"], 0)

    def test_upload_limit_and_request_limit(self):
        self.accel = b"x" * (MAX_FILE_BYTES + 1)
        self.assertEqual(self.upload().status_code, 413)
        self.assertEqual(self.client.post("/api/recordings", headers=self.headers,
                                         content=b"x" * (42 * 1024 * 1024 + 1)).status_code, 413)

    def test_pagination_and_name_safety(self):
        name = "<script>alert('trip')</script>"
        self.assertEqual(self.upload(name=name).json()["name"], name)
        self.assertEqual(self.upload(name="   ").status_code, 422)
        self.assertEqual(self.client.get("/api/recordings?offset=1", headers=self.headers).json()["items"], [])
        self.assertEqual(self.client.get("/api/recordings?limit=1001", headers=self.headers).status_code, 422)

    @unittest.skipUnless(os.environ.get("ASPHALTIQ_REAL_DATA"), "Set ASPHALTIQ_REAL_DATA to a local Sensor Logger recording directory")
    def test_actual_phone_recording(self):
        folder = Path(os.environ["ASPHALTIQ_REAL_DATA"])
        self.accel = (folder / "Accelerometer.csv").read_bytes()
        self.gps = (folder / "Location.csv").read_bytes()
        response = self.upload(threshold="0.81")
        self.assertEqual(response.status_code, 201, response.text)
        summary = response.json()
        for key, expected in {"acceleration_readings": 24295, "accepted_gps_fixes": 252,
                              "matched_readings": 24174, "candidate_events": 24,
                              "located_candidates": 24, "rejected_gps_rows": 1}.items():
            self.assertEqual(summary[key], expected)


if __name__ == "__main__":
    unittest.main()
