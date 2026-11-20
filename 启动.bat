@echo off
cd /d "%~dp0mydrive"
set PORT=8765
set STORAGE_ROOT=.\storage_data
if "%PYTHON%"=="" set PYTHON=python

echo ============================================================
echo   MyDrive - IPFS 内容寻址网盘
echo ============================================================
echo   Web/API : http://127.0.0.1:%PORT%
echo   正在启动...
echo.

"%PYTHON%" api_server.py

pause
