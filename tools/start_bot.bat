@echo off
REM Discord Claude Bot launcher.
REM Windows 작업 스케줄러에서 OnLogon 트리거로 호출됨.
REM 로그는 logs\bot.log 에 append. (날짜별 분리는 향후 logging 설정에서)

cd /d "%~dp0\.."
if not exist logs mkdir logs

REM .env 파일은 봇 루트에 있어야 함
".venv\Scripts\python.exe" bot.py >> "logs\bot.log" 2>&1
