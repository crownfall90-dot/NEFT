@echo off
rem Установка NEFT на новом компьютере. Автозапуск НЕ настраивается.
rem Вся логика в install_neft.ps1 — cmd.exe плохо разбирает кириллицу.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_neft.ps1"
