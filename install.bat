@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Creating virtual environment...
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Please install Python 3.10+ from python.org first.
    pause
    exit /b 1
)
if not exist venv (
    python -m venv venv
)
echo Installing PySide6 and requests...
"%~dp0venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0venv\Scripts\python.exe" -m pip install PySide6 requests pypinyin
if errorlevel 1 (
    echo.
    echo Install failed. Try a mirror:
    echo   venv\Scripts\python.exe -m pip install PySide6 requests pypinyin -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)
echo Done.
pause
