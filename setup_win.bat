@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo      AXL Flashtool Windows Environment Setup
echo ============================================================

:: Check Python installation
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python was not found in your PATH.
    echo Please install Python 3.8+ from https://www.python.org/
    pause
    exit /b 1
)

echo [1/3] Installing Python package dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARNING] Failed to install requirements via requirements.txt, trying setup.py...
    python -m pip install -e .
)

echo.
echo [2/3] Registering axlbin command line tools...
python -m pip install -e .

echo.
echo [3/3] Locating and configuring STM32_Programmer_CLI PATH...
python setup.py configure

echo.
echo ============================================================
echo  Setup Completed Successfully!
echo  Commands available: axlbin-flash, axlbin-update, axlbin-packet
echo ============================================================
pause
