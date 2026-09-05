@echo off
rem Остановка NEFT: гасит бота и панель, автозапуск не включает.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_stop.ps1"
