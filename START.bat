@echo off
rem Дубль ЗАПУСК.bat латиницей — на случай проблем с кириллицей в именах.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_start.ps1"
