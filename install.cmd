@echo off
rem Install Agent Sync Bridge for the current Windows user (reversible; see uninstall.cmd).
rem   install.cmd
rem   install.cmd --link "C:\path\to\project=my-id"
setlocal
set "ROOT=%~dp0"
where py >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")
%PY% -X utf8 -I "%ROOT%scripts\installer.py" install %*
if errorlevel 1 exit /b %ERRORLEVEL%
echo.
call "%ROOT%bin\asb.cmd" doctor
echo.
echo Next: trust the hook once in Codex (VS Code Codex settings - Hooks), then reload VS Code.
echo Cross-computer sync: asb remote setup ^<git url^> --device win --pair-code ^<code^>
