@echo off
chcp 65001 > nul
title AI 운용관리 서버 (이 창을 닫으면 서버가 꺼집니다)
cd /d "%~dp0"
rem 이미 떠 있으면 브라우저만 연다
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:5101/ | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  start "" http://127.0.0.1:5101
  exit /b 0
)
echo.
echo   AI 운용관리 서버를 켭니다 - http://127.0.0.1:5101
echo   이 창을 닫으면 서버가 꺼집니다. 최소화해 두세요.
echo.
start "" /b cmd /c "timeout /t 3 /nobreak > nul & start "" http://127.0.0.1:5101"
python web.py
echo.
echo   서버가 멈췄습니다. 위의 오류 메시지를 확인하세요.
pause
