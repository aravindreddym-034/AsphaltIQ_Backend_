"""AsphaltIQ API: upload, screen, persist, reload, and export recordings."""
import csv
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, ConfigDict

from core import DataError, MAX_FILE_BYTES, detect, load_recording

ROOT = Path(os.path.abspath(__file__)).parent


def dumps(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


class Guard:
    """Authenticate before multipart parsing and enforce a bounded request body."""
    def __init__(self, app, key):
        self.app, self.key = app, key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        protected = scope["path"].startswith("/api/") and scope["path"] != "/api/health"
        if protected and scope["method"] != "OPTIONS":
            supplied = headers.get(b"x-team-key", b"")
            if not hmac.compare_digest(supplied, self.key.encode("utf-8")):
                return await JSONResponse({"detail": "Enter the team access key."}, 401)(scope, receive, send)
        if scope["method"] in ("POST", "PUT", "PATCH"):
            # 40 MB of file content plus bounded multipart overhead.
            limit = 42 * 1024 * 1024 if scope["path"] == "/api/recordings" else 16_384
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    return await JSONResponse({"detail": "Request exceeds the upload limit."}, 413)(scope, receive, send)
                if not message.get("more_body"):
                    break
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)


class DetectionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    threshold: float | None = Field(default=None, gt=0)


def create_app(data_dir=None, team_key=None):
    directory = Path(data_dir or os.environ.get("ASPHALTIQ_DATA_DIR", ROOT / "data"))
    directory.mkdir(parents=True, exist_ok=True)
    key = team_key or os.environ.get("ASPHALTIQ_TEAM_KEY")
    if not key:
        key_file = directory / "team.key"
        try:
            with key_file.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        key = key_file.read_text(encoding="utf-8").strip()
    if len(key) < 16:
        raise RuntimeError("ASPHALTIQ_TEAM_KEY must contain at least 16 characters.")
    database = directory / "asphaltiq.sqlite3"
    # Bound expensive processing per process; SQLite writes are atomic transactions.
    processing = threading.Semaphore(1)

    @contextmanager
    def connection():
        db = sqlite3.connect(database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @asynccontextmanager
    async def lifespan(app):
        with connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY, summary TEXT NOT NULL, payload TEXT NOT NULL)""")
        yield

    app = FastAPI(title="AsphaltIQ Backend", version="1.0.0", lifespan=lifespan,
                  description="Shared recording storage and RMS screening. No trained pothole model.")
    router = APIRouter(prefix="/api", dependencies=[Security(APIKeyHeader(name="X-Team-Key", auto_error=False))])
    app.add_middleware(Guard, key=key)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    origins = [origin.strip() for origin in os.environ.get("ASPHALTIQ_CORS_ORIGINS", "").split(",") if origin.strip()]
    if "*" in origins:
        raise RuntimeError("Use explicit frontend origins instead of a wildcard.")
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["X-Team-Key", "Content-Type"])

    @app.exception_handler(DataError)
    async def bad_data(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def get_record(recording_id):
        with connection() as db:
            row = db.execute("SELECT payload FROM recordings WHERE id=?", (recording_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Recording not found.")
        return json.loads(row["payload"])

    def summarize(record):
        detection = record["detection"]
        return {**record["summary"], "id": record["id"], "name": record["name"],
                "created_at": record["created_at"], "detection_status": detection["status"],
                "threshold": detection["threshold"], "threshold_mode": detection["threshold_mode"],
                "candidate_events": len(detection["candidates"]),
                "located_candidates": sum(e["fix"] is not None for e in detection["candidates"]),
                "method": detection["method"]}

    def save_record(record, insert=False):
        with connection() as db:
            if insert:
                db.execute("INSERT INTO recordings VALUES (?,?,?)",
                           (record["id"], dumps(summarize(record)), dumps(record)))
            else:
                cursor = db.execute("UPDATE recordings SET summary=?,payload=? WHERE id=?",
                                    (dumps(summarize(record)), dumps(record), record["id"]))
                if cursor.rowcount == 0:
                    raise HTTPException(404, "Recording was deleted.")

    @app.get("/", include_in_schema=False)
    def homepage():
        return FileResponse(ROOT / "public" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/backend.js", include_in_schema=False)
    def integration():
        return FileResponse(ROOT / "public" / "backend.js", media_type="text/javascript")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "method": "rms_threshold_v1", "trained_ml_model": False}

    @router.post("/recordings", status_code=201)
    def upload_recording(
        acceleration: UploadFile = File(...), location: UploadFile = File(...),
        name: str = Form("Phone recording", min_length=1, max_length=120),
        max_gps_error_m: float = Form(50, gt=0, le=10_000),
        threshold: float | None = Form(None, gt=0),
    ):
        if not name.strip():
            raise HTTPException(422, "Recording name must not be blank.")
        try:
            raw_a = acceleration.file.read(MAX_FILE_BYTES + 1)
            raw_g = location.file.read(MAX_FILE_BYTES + 1)
        finally:
            acceleration.file.close()
            location.file.close()
        if len(raw_a) > MAX_FILE_BYTES or len(raw_g) > MAX_FILE_BYTES:
            raise HTTPException(413, "Each CSV must be at most 20 MB.")
        with processing:
            record = load_recording(raw_a, raw_g, max_gps_error_m)
            record.update(id=str(uuid4()), name=name.strip(),
                          created_at=datetime.now(timezone.utc).isoformat())
            record["detection"] = detect(record, threshold)
            save_record(record, insert=True)
        return summarize(record)

    @router.get("/recordings")
    def list_recordings(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        with connection() as db:
            total = db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
            rows = db.execute("SELECT summary FROM recordings ORDER BY rowid DESC LIMIT ? OFFSET ?",
                              (limit, offset)).fetchall()
        return {"total": total, "items": [json.loads(row["summary"]) for row in rows]}

    @router.get("/recordings/{recording_id}")
    def recording_summary(recording_id: str):
        return summarize(get_record(recording_id))

    @router.get("/recordings/{recording_id}/data")
    def recording_data(recording_id: str):
        """Browser-compatible samples, GPS fixes, and detection results. Timestamps are strings."""
        return get_record(recording_id)

    @router.post("/recordings/{recording_id}/detect")
    def screen(recording_id: str, options: DetectionOptions):
        with processing:
            record = get_record(recording_id)
            record["detection"] = detect(record, options.threshold)
            save_record(record)
        return record["detection"]

    @router.get("/recordings/{recording_id}/events.geojson")
    def geojson(recording_id: str):
        record = get_record(recording_id)
        features = []
        for event in record["detection"]["candidates"]:
            fix = event["fix"]
            if fix is not None:
                features.append({"type": "Feature", "geometry": {
                    "type": "Point", "coordinates": [fix["lon"], fix["lat"]]},
                    "properties": {"recording_id": recording_id, "event_id": event["id"],
                                   "status": event["status"], "time_s": event["time"],
                                   "peak_m_s2": event["peak"], "rms_m_s2": event["rms"],
                                   "gps_error_m": fix["accuracy"],
                                   "gps_time_difference_s": abs(event["time"] - fix["t"])}})
        return {"type": "FeatureCollection", "features": features,
                "unlocated_events": len(record["detection"]["candidates"]) - len(features)}

    @router.get("/recordings/{recording_id}/events.csv")
    def export_events(recording_id: str):
        record = get_record(recording_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["event_id", "peak_time_s", "start_s", "end_s", "peak_m_s2",
                         "max_window_rms_m_s2", "threshold_m_s2", "latitude", "longitude",
                         "gps_time_difference_s", "horizontal_accuracy_m", "status"])
        for e in record["detection"]["candidates"]:
            f = e["fix"]
            writer.writerow([e["id"], e["time"], e["start"], e["end"], e["peak"], e["rms"],
                             e["threshold"], f["lat"] if f else "", f["lon"] if f else "",
                             abs(e["time"] - f["t"]) if f else "", f["accuracy"] if f else "", e["status"]])
        return Response(output.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="AsphaltIQ_{record["id"]}_events.csv"'})

    @router.delete("/recordings/{recording_id}", status_code=204)
    def delete_recording(recording_id: str):
        with connection() as db:
            cursor = db.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
            if cursor.rowcount == 0:
                raise HTTPException(404, "Recording not found.")
        return Response(status_code=204)

    app.include_router(router)
    return app
