"""Sensor Logger parsing and the existing AsphaltIQ RMS screening baseline."""
import bisect
import csv
import io
import math
import re
import statistics

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ACCEL_ROWS = 250_000
MAX_GPS_ROWS = 50_000
METHOD = "rms_threshold_v1"


class DataError(ValueError):
    pass


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def number(value):
    if value is None or not str(value).strip():
        raise ValueError("Missing number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite number")
    return result


def timestamp(value):
    value = value.strip()
    if not re.fullmatch(r"[0-9]{18,20}", value):
        raise ValueError("Expected integer nanosecond timestamp")
    return int(value)


def table(raw, required, aliases, limit):
    if len(raw) > MAX_FILE_BYTES:
        raise DataError("Each CSV must be at most 20 MB.")
    try:
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        rows = (row for row in reader if any(cell.strip() for cell in row))
        headers = [cell.strip() for cell in next(rows)]
        names = [normalize(cell) for cell in headers]
        if not all(names) or len(set(names)) != len(names):
            raise DataError("CSV headings must be nonempty and unique.")
        columns = {}
        for field in required:
            options = aliases.get(field, [field])
            columns[field] = next((i for i, name in enumerate(names) if name in options), -1)
            if columns[field] < 0:
                raise DataError(f"Missing column: {field}. Expected Sensor Logger CSV files.")
        columns["accuracy"] = next((i for i, n in enumerate(names)
                                    if n in ("horizontalaccuracy", "accuracy")), -1)
        result = []
        for row in rows:
            if len(result) >= limit:
                raise DataError(f"CSV exceeds the {limit:,} row limit.")
            result.append(row)
        if not result:
            raise DataError("CSV needs a heading row and at least one data row.")
        return headers, columns, result
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise DataError("Invalid or empty CSV. Use comma-separated UTF-8 files.") from exc


def nearest_fix(time, fixes, times):
    if not fixes or time < times[0] or time > times[-1]:
        return None
    index = min(bisect.bisect_left(times, time), len(fixes) - 1)
    if index > 0 and abs(times[index - 1] - time) < abs(times[index] - time):
        index -= 1
    return fixes[index] if abs(times[index] - time) <= 2 else None


def load_recording(acceleration, location, max_error=50):
    if not math.isfinite(max_error) or not 0 < max_error <= 10_000:
        raise DataError("GPS error limit must be greater than 0 and at most 10000 metres.")
    ah, ac, ar = table(acceleration, ["time", "x", "y", "z"], {}, MAX_ACCEL_ROWS)
    gh, gc, gr = table(location, ["time", "latitude", "longitude"], {
        "latitude": ["latitude", "lat"], "longitude": ["longitude", "lon", "lng"]
    }, MAX_GPS_ROWS)
    samples, fixes = [], []
    rejected_accel, rejected_gps = 0, 0
    for row in ar:
        try:
            if len(row) != len(ah):
                raise ValueError()
            ns = timestamp(row[ac["time"]])
            x, y, z = (number(row[ac[k]]) for k in ("x", "y", "z"))
            magnitude = math.hypot(x, y, z)
            # Squared magnitudes must also be representable for RMS accumulation.
            if not math.isfinite(magnitude) or magnitude > 1e100:
                raise ValueError()
            samples.append({"timestamp": str(ns), "x": x, "y": y, "z": z,
                            "magnitude": magnitude})
        except (ValueError, OverflowError):
            rejected_accel += 1
    if not samples:
        raise DataError("No valid acceleration readings with integer nanosecond timestamps.")
    samples.sort(key=lambda s: int(s["timestamp"]))
    base = int(samples[0]["timestamp"])
    for index, sample in enumerate(samples):
        sample.update(t=(int(sample["timestamp"]) - base) / 1e9, index=index)
    end = samples[-1]["t"]
    for row in gr:
        try:
            if len(row) != len(gh):
                raise ValueError()
            ns = timestamp(row[gc["time"]])
            time = (ns - base) / 1e9
            lat, lon = number(row[gc["latitude"]]), number(row[gc["longitude"]])
            # Missing accuracy stays unknown; malformed nonempty accuracy is rejected.
            raw_error = row[gc["accuracy"]].strip() if gc["accuracy"] >= 0 else ""
            error = number(raw_error) if raw_error else None
            if (abs(lat) > 90 or abs(lon) > 180 or time < 0 or time > end
                    or (error is not None and not 0 <= error <= max_error)):
                raise ValueError()
            fixes.append({"timestamp": str(ns), "t": time, "lat": lat,
                          "lon": lon, "accuracy": error})
        except (ValueError, OverflowError):
            rejected_gps += 1
    fixes.sort(key=lambda fix: int(fix["timestamp"]))
    times = [fix["t"] for fix in fixes]
    matched = sum(nearest_fix(s["t"], fixes, times) is not None for s in samples)
    summary = {
        "acceleration_readings": len(samples), "accepted_gps_fixes": len(fixes),
        "matched_readings": matched, "unmatched_readings": len(samples) - matched,
        "gps_match_coverage_percent": 100 * matched / len(samples),
        "rejected_acceleration_rows": rejected_accel, "rejected_gps_rows": rejected_gps,
        "duration_s": end, "max_gps_error_m": max_error,
        "gps_match_tolerance_s": 2, "accuracy_unknown_fixes": sum(f["accuracy"] is None for f in fixes),
    }
    return {"samples": samples, "fixes": fixes, "summary": summary}


def detect(recording, threshold=None):
    if threshold is not None and (not math.isfinite(threshold) or threshold <= 0):
        raise DataError("Threshold must be positive, or blank for automatic selection.")
    windows = {}
    for sample in recording["samples"]:
        wid = math.floor(sample["t"] / 0.5)
        window = windows.setdefault(wid, {"count": 0, "sum": 0, "first": sample["t"],
                                         "last": sample["t"], "peak": sample})
        window["count"] += 1
        window["sum"] += sample["magnitude"] ** 2
        window["last"] = sample["t"]
        if sample["magnitude"] > window["peak"]["magnitude"]:
            window["peak"] = sample
    usable = [w for w in windows.values() if w["count"] >= 5 and w["last"] - w["first"] >= 0.2]
    if len(usable) < 10:
        return {"method": METHOD, "status": "insufficient_data", "threshold": threshold,
                "threshold_mode": "automatic" if threshold is None else "manual",
                "usable_windows": len(usable), "candidates": [],
                "message": "Not enough complete windows. Use a longer recording."}
    for window in usable:
        window["rms"] = math.sqrt(window["sum"] / window["count"])
    scores = [w["rms"] for w in usable]
    baseline = statistics.median(scores)
    mad = statistics.median(abs(score - baseline) for score in scores)
    cutoff = baseline + max(0.5, 6 * 1.4826 * mad) if threshold is None else threshold
    fixes = recording["fixes"]
    times = [f["t"] for f in fixes]
    candidates, cluster = [], None

    def finish():
        if cluster is None:
            return
        peak = cluster["peak"]
        candidates.append({"id": len(candidates) + 1, "time": peak["t"],
                           "start": cluster["first"], "end": cluster["last"],
                           "sampleIndex": peak["index"], "peak": peak["magnitude"],
                           "rms": cluster["rms"], "threshold": cutoff,
                           "fix": nearest_fix(peak["t"], fixes, times),
                           "status": "unverified_vibration_candidate"})

    for window in usable:
        if window["rms"] <= cutoff:
            finish()
            cluster = None
        elif cluster is not None and window["first"] - cluster["last"] <= 0.15:
            cluster["last"] = window["last"]
            cluster["rms"] = max(cluster["rms"], window["rms"])
            if window["peak"]["magnitude"] > cluster["peak"]["magnitude"]:
                cluster["peak"] = window["peak"]
        else:
            finish()
            cluster = dict(window)
    finish()
    return {"method": METHOD, "status": "complete", "threshold": cutoff,
            "threshold_mode": "automatic" if threshold is None else "manual",
            "usable_windows": len(usable), "candidates": candidates,
            "message": "These are unverified vibration events, not confirmed potholes."}
