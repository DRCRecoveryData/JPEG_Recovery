@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 set "PYTHON=py -3"

%PYTHON% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    %PYTHON% -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo Could not install PyInstaller.
        pause
        exit /b 1
    )
)

echo Building standalone application...
%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed ^     --name "JPEG-Recovery" main.py

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Built dist\JPEG-Recovery.exe
pause
endlocal
