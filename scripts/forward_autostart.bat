@echo off
rem Автозапуск форвард-теста для планировщика Windows.
rem Состояние НЕ сбрасывается (без --reset) — статистика копится между
rem перезапусками (перезагрузка ПК, сбой, ежедневный цикл планировщика).
cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\forward.py" >> "logs\forward_console.log" 2>&1
