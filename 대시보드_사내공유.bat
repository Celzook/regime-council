@echo off
chcp 65001 > nul
title AI 운용관리 서버 - 사내 공유(읽기 전용) - 이 창을 닫으면 꺼집니다
cd /d "%~dp0"
echo.
echo   사내 공유 모드로 켭니다 (같은 서브넷 PC · 접속 키 · 읽기 전용)
echo   아래에 나오는 링크(…/?key=…)를 동료에게 전달하세요. 이 창을 닫으면 서버가 꺼집니다.
echo.
set REGIME_LAN=1
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" web.py
echo.
echo   서버가 멈췄습니다. 위의 오류 메시지를 확인하세요.
pause
