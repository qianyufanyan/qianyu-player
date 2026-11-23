@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
    echo First run: installing dependencies, please wait...
    call "%~dp0install.bat"
    if not exist "venv\Scripts\pythonw.exe" (
        echo Install failed. Run install.bat manually.
        pause
        exit /b 1
    )
)
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0player.py"
exit /b 0
