@echo off
setlocal
cd /d "%~dp0\..\.."

echo Checking for PyInstaller...
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller is not installed. Installing it for the current Python environment...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo Failed to install PyInstaller.
        exit /b 1
    )
)

echo Building AI Tool Session Tracker...
python -m PyInstaller --clean --noconfirm --distpath "dist\windows" "packer\windows\AI-Tool-Session-Tracker.spec"
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete:
echo dist\windows\AI-Tool-Session-Tracker.exe
endlocal