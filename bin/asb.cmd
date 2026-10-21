@echo off
rem Agent Sync Bridge CLI for Windows (cmd / PowerShell). Needs Python 3.9+.
setlocal
set "ASB_ENTRY=%~dp0asb.py"
if defined ASB_PYTHON goto custom
where py >nul 2>nul
if errorlevel 1 goto plain
py -3 -X utf8 -I "%ASB_ENTRY%" %*
exit /b %ERRORLEVEL%
:plain
python -X utf8 -I "%ASB_ENTRY%" %*
exit /b %ERRORLEVEL%
:custom
"%ASB_PYTHON%" -X utf8 -I "%ASB_ENTRY%" %*
exit /b %ERRORLEVEL%
