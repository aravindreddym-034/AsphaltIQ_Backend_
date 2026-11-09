@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.11 or newer from python.org.
  echo Then close this window and run Start_AsphaltIQ.cmd again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  py -m venv .venv
  if errorlevel 1 goto failed
)
echo Checking and installing the backend packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Open http://127.0.0.1:8000 after the server starts.
echo Your team key is stored in data\team.key after the first startup.
".venv\Scripts\python.exe" start.py
pause
exit /b 0
:failed
echo.
echo Setup did not finish. Read the error above and the README instructions.
pause
exit /b 1
