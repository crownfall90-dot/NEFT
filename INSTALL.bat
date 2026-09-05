@echo off
rem Дубль УСТАНОВКА.bat латиницей — на случай проблем с кириллицей в именах.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_neft.ps1"
