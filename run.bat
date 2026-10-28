@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
where python >nul 2>nul
if errorlevel 1 (
    set "PYTHON=py -3"
)

%PYTHON% -c "import PyQt6, PIL, numpy" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    %PYTHON% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Could not install the requirements.
        pause
        exit /b 1
    )
)

%PYTHON% main.py
if errorlevel 1 pause
endlocal
