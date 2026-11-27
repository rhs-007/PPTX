@echo off
rem  PPT Template Migrator - double-click to start the web app.
cd /d "%~dp0"
title PPT Template Migrator

set "PY=py"
where py >nul 2>nul || set "PY=python"

%PY% -c "import lxml, PIL" >nul 2>nul
if errorlevel 1 (
  echo First run: installing the two helper packages ^(lxml, Pillow^) ...
  %PY% -m pip install --user -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Could not install the packages. Check the internet connection and try again.
    pause
    exit /b 1
  )
)

%PY% -m ppt_migrator %*
if errorlevel 1 pause
