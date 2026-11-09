# AsphaltIQ backend and connected prototype

This package adds a Python FastAPI server and a SQLite database to the existing
AsphaltIQ HTML prototype. It includes a copy of the frontend with backend controls.
Your earlier HTML files and deployed Netlify website have not been replaced.

## What now works

- Upload `Accelerometer.csv` and `Location.csv` from the same Sensor Logger recording.
- Validate columns, nanosecond timestamps, finite acceleration, and GPS coordinates.
- Filter GPS by reported horizontal error; retain missing error as unknown.
- Match the nearest accepted GPS fix within 2 seconds, without extrapolation.
- Screen half-second RMS windows using a manual or automatic threshold.
- Save accepted samples, GPS fixes, candidate events, settings, and trip summaries in SQLite.
- List and reopen recordings, rerun detection, export candidate CSV and GeoJSON, and delete recordings.
- Protect stored recordings with one shared team access key.

The detector is the existing **RMS statistical baseline**, not a trained machine
learning classifier. Events remain `unverified_vibration_candidate`. The package
does not yet implement live phone streaming, a trained Random Forest model,
verified damage labels, or clustering repeated reports across riders.

## Start on Windows

If Python is already installed with its `py` launcher, extract the ZIP and double-click
**Start_AsphaltIQ.cmd**. It creates an environment, installs the packages, and starts
the server. The first installation needs internet. You can also use the commands below.

1. Extract the ZIP into a normal folder, for example `Documents\AsphaltIQ`.
   Do not run files from inside the ZIP preview.
2. Open that folder in VS Code. Choose **Terminal → New Terminal**.
3. Use Python 3.11 or newer. Check `py --version`. If Windows cannot find Python,
   install Python from https://www.python.org/downloads/ and reopen the terminal.
4. Run these commands in the folder that contains `start.py`:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe start.py
```

5. Keep the terminal open. Open **http://127.0.0.1:8000** in the browser.
6. The first startup creates `data\team.key`. Open it in Notepad, copy its contents,
   and paste into the page's **Team access key** field. Keep this key within your team.
7. Leave **Backend address** as `http://127.0.0.1:8000`.
8. Choose your two CSV files in section 1 and enter a recording name at the top.
9. In the Shared backend panel, leave the RMS threshold empty for automatic selection,
   or enter a positive manual threshold. These settings are read when you save.
10. Click **Save recording to backend**. The server processes and saves the recording;
    the graph, route, candidate table, and saved recording list then appear.
11. Refresh the browser, re-enter the team key, click **Connect and refresh**, select
    a saved trip, and click **Open saved recording**. You do not need to upload it again.

`Load locally` and `Find candidates locally` still run the original browser code.
They do not save changes. For an opened server recording, change the threshold and
click **Run and save on backend** to update its stored candidate results. Changing
the GPS error filter requires selecting the original CSVs and saving a new recording.

## What to explain in your review

“The frontend lets users inspect recordings. The new backend receives the CSVs,
matches acceleration to GPS, applies our RMS screening rule, and stores the results
in a database. We can reopen recorded trips without uploading the data again.
The marked events are suspected vibration events. Training and evaluating a
pothole classification model is future work.”

## Share with your teammate

Sending this code ZIP lets your teammate run their own copy. Separate copies have
separate databases. A Netlify frontend link by itself does not share your local database.

For a demonstration on the **same trusted Wi-Fi**, run:

```powershell
$env:HOST = "0.0.0.0"
.\.venv\Scripts\python.exe start.py
```

Find your PC's Wi-Fi IPv4 address using `ipconfig`. If it is `192.168.1.20`, your
teammate opens `http://192.168.1.20:8000` on the same network and uses the same team
key. Their backend address should also be `http://192.168.1.20:8000`. The PC must
remain on, and Windows Firewall may require permission on the private network.
Use this HTTP setup only for a trusted local demonstration; use HTTPS for internet sharing.

For access from different places, deploy this Python service on a Python-capable
host with HTTPS and durable disk storage. Configure `ASPHALTIQ_DATA_DIR` to the
persistent directory and set `ASPHALTIQ_TEAM_KEY` to a long random secret of at least
16 characters. All teammates then use the same hosted backend.
**Netlify Drop uploads the static frontend; it does not run this Python process.**
No cloud backend deployment is included in this package.

If you keep the frontend separately on Netlify, upload both files inside `public/`,
enter your hosted backend HTTPS address on the page, and configure the backend's
`ASPHALTIQ_CORS_ORIGINS` to the exact Netlify origin, for example:

```powershell
$env:ASPHALTIQ_CORS_ORIGINS = "https://your-site.netlify.app"
```

Restart the backend after changing environment variables. Comma-separated explicit
origins are supported. Leaving this variable unset permits the frontend served by
the backend itself; it does not enable cross-origin access.

## API

Interactive API documentation: **http://127.0.0.1:8000/docs**.
Protected requests require header `X-Team-Key: YOUR_KEY`. In the docs, click
**Authorize**, enter the team key, then try the endpoints. The frontend already adds this header.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Basic public health check |
| POST | `/api/recordings` | Multipart upload, detect, and save |
| GET | `/api/recordings?limit=50&offset=0` | Paginated recording summaries |
| GET | `/api/recordings/{id}` | One summary |
| GET | `/api/recordings/{id}/data` | Stored samples, GPS, and candidate data |
| POST | `/api/recordings/{id}/detect` | Rerun with JSON `{"threshold": null}` or a positive number |
| GET | `/api/recordings/{id}/events.geojson` | Located candidates, longitude first |
| GET | `/api/recordings/{id}/events.csv` | All candidates, including those without GPS |
| DELETE | `/api/recordings/{id}` | Delete recording and results |

Upload form fields: `acceleration` (file), `location` (file), `name` (text),
`max_gps_error_m` (default 50), and optional `threshold` (omit for automatic).
Server-generated IDs and parameterized SQL avoid trusting uploaded file names as paths.
The original CSV files are not retained; their accepted contents and summaries are stored.

## Dataset contract and screening

- Comma-separated UTF-8, optionally with a BOM. Quoted fields are supported.
- Acceleration fields: `time`, `x`, `y`, `z` in any column order, units m/s².
- Location fields: `time`, `latitude` (or `lat`), `longitude` (or `lon`/`lng`).
  Optional error: `horizontalAccuracy` or `accuracy`, in metres.
- `time`: 18–20 decimal digits representing integer nanoseconds on a common clock.
  Never convert these timestamps to floating-point numbers before subtracting the base.
- Missing GPS error is retained as unknown. Nonempty malformed error is rejected
  on the server, which is stricter than the old local page.
- Each file at most 20 MB; at most 250,000 acceleration rows and 50,000 GPS rows.
  Longer recordings must be split into aligned sections or these limits reviewed.
- A usable 0.5-second window needs at least 5 samples spanning 0.2 seconds.
  At least 10 usable windows are needed for screening.
- `magnitude = sqrt(x*x + y*y + z*z)`; no extra gravity correction is applied.
- `window_RMS = sqrt(mean(magnitude*magnitude))`.
- Automatic threshold:
  `median(RMS) + max(0.5, 6 * 1.4826 * median(abs(RMS - median(RMS))))`.
- Flag windows strictly above threshold. Merge adjacent flagged windows when the
  last-to-first sample gap is at most 0.15 seconds. Use the peak sample's GPS match.
- GPS match coverage is not pothole detection accuracy.

Other CSV formats need adapters for column names, units, timestamps, and labels.
The Mendeley `AccelerationSignal.csv` is not directly interchangeable with this input.

## Files and storage

```text
main.py           API, access control, SQLite storage, exports
core.py           Parsing, GPS matching, RMS detection
start.py          Local server launcher
public/index.html Existing prototype with server controls
public/backend.js Frontend API integration
test_backend.py   API and signal-processing regression checks
data/             Created on startup; database and team key; excluded from source ZIP
```

All key holders share the same team workspace and can delete its recordings.
This is a small team prototype, not a user account system. GPS records are private
team data. Back up the database before deployment changes. For a consistent SQLite
backup while running, use SQLite's backup API; do not copy only the database file
while WAL writes are active. Deleting a record does not remove existing backups.
Do not put the key or database in Git or in a public static folder.

## Run checks

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest -v test_backend.py
```

The small generated signals in tests are test fixtures, not the project's road dataset.
One optional test reproduces the original 12 October phone run if its directory is provided:

```powershell
$env:ASPHALTIQ_REAL_DATA = "C:\path\to\8GCM_VX6-2026-10-12_14-38-00"
.\.venv\Scripts\python.exe -m unittest -v test_backend.py
```

## Technical references

- FastAPI file uploads: https://fastapi.tiangolo.com/tutorial/request-files/
- FastAPI CORS: https://fastapi.tiangolo.com/tutorial/cors/
- FastAPI API key header: https://fastapi.tiangolo.com/reference/security/#fastapi.security.APIKeyHeader
- SQLite in Python: https://docs.python.org/3/library/sqlite3.html
