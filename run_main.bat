@echo off
cd /d "%~dp0"
chcp 65001 >nul
title ADS Inflow Automation
python main.py
if errorlevel 1 (
  echo.
  echo Script finished with error code: %errorlevel%
)
echo.
pause
