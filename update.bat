@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Update Port Analyzer

rem Pulls the newest version from GitHub. Your data folder (switch list,
rem settings, reports) is never touched by updates.

where git >nul 2>&1
if errorlevel 1 goto :no_git
if not exist ".git" goto :not_cloned

git pull --ff-only
if errorlevel 1 goto :fail
echo.
echo   Up to date. Start the app with RunAnalyzer.bat.
echo   New libraries, if any, install the next time it starts.
echo.
pause
exit /b 0

:no_git
echo.
echo   Git isn't installed. Either install it from https://git-scm.com/download/win
echo   or download the newest ZIP from the GitHub page and copy your "data" folder
echo   into it.
echo.
pause
exit /b 1

:not_cloned
echo.
echo   This copy was downloaded as a ZIP, so it can't update itself.
echo   Download the newest ZIP from the GitHub page and copy your "data" folder
echo   into it.
echo.
pause
exit /b 1

:fail
echo.
echo   The update didn't apply. If you edited any app files, git won't overwrite
echo   them. Ask whoever maintains the repo, or download a fresh copy.
echo.
pause
exit /b 1
