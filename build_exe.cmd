@echo off
rem Builds dist\FH2Manager.exe (one file, no console). Needs Python 3.8+ on PATH; makes a local .venv the first time.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || goto :error
    .venv\Scripts\python -m pip install --upgrade pip || goto :error
    .venv\Scripts\python -m pip install customtkinter pyinstaller || goto :error
)
rem an open FH2Manager.exe cannot be overwritten, but it can be renamed: move it aside (deleted on a later build)
del dist\FH2Manager.old.exe 2>nul
if exist dist\FH2Manager.exe (
    del dist\FH2Manager.exe 2>nul
    if exist dist\FH2Manager.exe move /y dist\FH2Manager.exe dist\FH2Manager.old.exe >nul
)
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed --name FH2Manager --icon fh2manager\data\icon.ico ^
    --collect-data customtkinter --add-data "fh2manager\data;fh2manager\data" fh2manager_gui.py || goto :error
rmdir /s /q build 2>nul
del FH2Manager.spec 2>nul
echo.
echo Built dist\FH2Manager.exe
exit /b 0
:error
echo Build failed.
exit /b 1
