@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dashboard-server.ps1"
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:8787"
