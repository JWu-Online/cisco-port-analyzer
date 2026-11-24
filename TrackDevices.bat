@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Device tracker scan

rem ---------------------------------------------------------------------
rem  Device tracker: scans every switch on the list over SNMP (read-only)
rem  and saves what moved. Windows Task Scheduler runs this every day
rem  (turn it on in the app: Device tracker, Daily scan). Double-click it
rem  to scan now.
rem ---------------------------------------------------------------------

rem Started by Task Scheduler: reopen minimized so it doesn't pop up over your work
if /i "%~1"=="--scheduled" (
  start "Device tracker scan" /min "%~f0" --scheduled-window
  exit /b 0
)
set "FLAGS="
if /i "%~1"=="--scheduled-window" set "FLAGS=--scheduled"

if not exist ".venv\Scripts\python.exe" goto :no_setup

rem New libraries after an update (the same check RunAnalyzer.bat does)
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if not errorlevel 1 goto :have_libraries
echo Installing the libraries the app needs...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :fail
copy /y requirements.txt ".venv\requirements.installed" >nul
:have_libraries

".venv\Scripts\python.exe" tracker.py %FLAGS%
if errorlevel 1 goto :fail
if not defined FLAGS pause
exit /b 0

:no_setup
echo.
echo   Start the app once with RunAnalyzer.bat first (it sets things up), then try again.
echo.
if not defined FLAGS pause
exit /b 1

:fail
echo.
echo   The scan didn't finish. The Device tracker page shows why
echo   (or read data\tracker\last-scan.log).
echo.
if not defined FLAGS pause
exit /b 1
