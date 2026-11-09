# Verification

Checked on 5 November 2026.

13 automated checks passed: API access control, CORS, nanosecond precision, real CSV
upload, GPS quality filtering and missing accuracy, time gaps, out-of-span readings,
invalid schema and values, file and request limits, pagination, persisted storage
across app instances, rerunning detection, exports, deletion, short recordings, and
events without GPS matches.

An automated Chrome browser check also passed: real-file upload through the new
frontend controls, server detection, graph inspection, map display, page reload and
reopening, persisted threshold changes, export when zero events were found, and
local preview changes leaving saved server results unchanged. No JavaScript errors
were observed. The map library and road tiles loaded in the checked run.

The real phone recording from 12 October reproduced:

| Measure | Result |
| --- | --- |
| Valid acceleration readings | 24,295 |
| Accepted GPS fixes | 252 |
| Acceleration readings matched to GPS | 24,174 |
| Excluded GPS rows | 1 |
| Manual RMS threshold | 0.81 m/s² |
| Unverified candidate events | 24 |
| Candidates with matched GPS | 24 |

These checks establish processing and storage behavior, not pothole classification
accuracy. There are no verified pothole labels or model accuracy results in this package.
Internet deployment and access from another physical device were not tested.
The Windows double-click launcher is provided; its full installation flow on a fresh
PC was not tested. The Python API and frontend were tested with installed dependencies.
