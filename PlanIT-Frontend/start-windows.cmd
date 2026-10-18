@echo off
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Install Node.js 22 or newer, then run this file again.
  pause
  exit /b 1
)
if not exist node_modules (
  call npm ci
  if errorlevel 1 goto failed
)
echo Open http://localhost:5173 in your browser.
echo Keep this window open while using PlanIT Control.
call npm run dev
if errorlevel 1 goto failed
exit /b 0
:failed
echo PlanIT could not start. Review the message above.
pause
exit /b 1
