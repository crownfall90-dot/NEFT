@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
call "%~dp0restart_telegram_bot.bat" _bg
timeout /t 2 /nobreak >nul
call "%~dp0restart_admin.bat" _bg
endlocal
