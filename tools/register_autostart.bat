@echo off
REM Windows 작업 스케줄러에 봇 자동 기동 등록 (OnLogon 트리거).
REM 관리자 권한 불필요 — 사용자 컨텍스트로 등록.
REM
REM 등록: tools\register_autostart.bat
REM 해제: schtasks /delete /tn "DiscordClaudeBot" /f
REM 즉시 실행 테스트: schtasks /run  /tn "DiscordClaudeBot"

setlocal
set TASK_NAME=DiscordClaudeBot
set BOT_DIR=%~dp0..
set LAUNCHER=%BOT_DIR%\tools\start_bot.bat

REM 경로에 공백 포함 가능 → 큰따옴표로 감쌈
schtasks /create ^
    /tn "%TASK_NAME%" ^
    /tr "\"%LAUNCHER%\"" ^
    /sc onlogon ^
    /rl limited ^
    /f

if %errorlevel% neq 0 (
    echo.
    echo [FAIL] 작업 등록 실패. 위 메시지 확인 바랍니다.
    exit /b 1
)

echo.
echo [OK] '%TASK_NAME%' 등록 완료. 다음 로그인부터 봇 자동 기동됩니다.
echo      즉시 실행 테스트: schtasks /run /tn "%TASK_NAME%"
echo      등록 확인:        schtasks /query /tn "%TASK_NAME%"
endlocal
