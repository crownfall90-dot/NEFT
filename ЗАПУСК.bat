@echo off
rem Ручной запуск NEFT. Автозапуска нет — стартует только этим файлом.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0neft_start.ps1"
