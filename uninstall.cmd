@echo off
rem Remove every integration installed by install.cmd. Message data is kept unless --purge-data.
setlocal
set "ROOT=%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")
%PY% -X utf8 -I "%ROOT%scripts\installer.py" uninstall %*
