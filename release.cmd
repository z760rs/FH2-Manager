@echo off
rem Builds release\FH2Manager-<version>.zip: FH2Manager.exe, README.md and LICENSE - nothing else (no settings, mod
rem library, logs, or files of Azimuth / Extra Options / FH2Patch). tools\release_check.py checks the source and the
rem exe for machine-specific strings and stops the release if it finds any.
setlocal
cd /d "%~dp0"
if not exist LICENSE (
    echo LICENSE is missing - add one before making a release.
    exit /b 1
)
call "%~dp0build_exe.cmd" || goto :error
"%~dp0.venv\Scripts\python.exe" -I "%~dp0tools\release_check.py" || goto :error
exit /b 0
:error
echo Release failed.
exit /b 1
