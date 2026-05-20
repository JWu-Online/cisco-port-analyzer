@echo off
title Cisco Port Analyzer
echo Starting Cisco Port Analyzer...
cd /d "%~dp0"
call venv\Scripts\activate.bat
echo Opening browser...
start http://localhost:5000
echo Running server...
python app.py
pause
