@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Port Analyzer

rem ---------------------------------------------------------------------
rem  Port Analyzer launcher (Windows)
rem  First run: makes a private Python environment in .venv and installs
rem  the libraries the app needs. Later runs: starts the app and opens
rem  it in your browser.
rem ---------------------------------------------------------------------

rem 1. Find Python 3.9 or newer
set "PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY goto :no_python
:have_python

rem 2. Make the private Python environment (first run only)
if exist ".venv\Scripts\python.exe" goto :have_venv
echo Setting up for first use. This takes a minute or two...
%PY% -m venv .venv
if errorlevel 1 goto :fail
:have_venv

rem 3. Install or update the libraries whenever requirements.txt changes
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if not errorlevel 1 goto :have_libraries
echo Installing the libraries the app needs...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :fail
copy /y requirements.txt ".venv\requirements.installed" >nul
:have_libraries

rem 4. Start the app. It opens your browser by itself.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail
exit /b 0

:no_python
echo.
echo   Python 3.9 or newer isn't installed on this computer.
echo.
echo   1. Install it from https://www.python.org/downloads/
echo      On the first installer screen, tick "Add python.exe to PATH".
echo      Or, in a Command Prompt, run:  winget install Python.Python.3.12
echo   2. Double-click RunAnalyzer.bat again.
echo.
pause
exit /b 1

:fail
echo.
echo   Something went wrong. Read the messages above, or see
echo   "Troubleshooting" in README.md.
echo.
pause
exit /b 1
