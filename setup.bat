@echo off
rem Thin launcher — real logic (incl. Russian text) lives in setup.ps1,
rem cmd.exe batch parsing of Cyrillic is unreliable, PowerShell handles it fine.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
