' Тихий запуск neft_watchdog.ps1 — Task Scheduler, вызывая powershell.exe
' напрямую даже с -WindowStyle Hidden, на долю секунды всё равно показывает
' окно консоли (особенность Windows, не лечится флагами PowerShell). Этот
' VBS-обёртка — единственный по-настоящему бесшумный способ: WScript.Shell.Run
' с параметром окна 0 создаёт процесс без окна вообще, а не прячет его после.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = scriptDir & "\neft_watchdog.ps1"

Set shell = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & psScript & """"
shell.Run cmd, 0, True
