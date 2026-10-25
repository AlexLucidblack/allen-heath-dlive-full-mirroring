@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python allen_heath_dlive_full_mirroring.py %*
) else (
  py -3 allen_heath_dlive_full_mirroring.py %*
)
pause
