@echo off
rem Starts TaskWrite on this computer and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo TaskWrite is not set up yet. Follow "Quick start" in README.md first.
  pause
  exit /b 1
)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/app"
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000
