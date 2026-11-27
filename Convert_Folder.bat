@echo off
rem  Convert every deck in a folder without the web page.
rem  Usage: drag a folder onto this file, or:  Convert_Folder.bat "C:\path\to\old decks"
cd /d "%~dp0"
set "PY=py"
where py >nul 2>nul || set "PY=python"
if "%~1"=="" (
  echo Drag a folder of old decks onto Convert_Folder.bat
  pause
  exit /b 1
)
%PY% -m ppt_migrator batch "%~1"
pause
