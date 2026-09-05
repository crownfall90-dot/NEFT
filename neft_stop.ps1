# Остановка NEFT. Автозапуск не включает и не выключает — его просто нет.
# -Quiet используется при рестарте, чтобы не ждать Enter между шагами.
param([switch]$Quiet)

$ErrorActionPreference = "SilentlyContinue"
$Root = $PSScriptRoot

function Say([string]$t, [string]$c = "Gray") { Write-Host $t -ForegroundColor $c }

if (-not $Quiet) {
    Write-Host ("=" * 52) -ForegroundColor Cyan
    Write-Host "  NEFT - остановка" -ForegroundColor Cyan
    Write-Host ("=" * 52) -ForegroundColor Cyan
    Write-Host ""
}

$targets = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'telegram_bot\.py|admin_server\.py|neft_watchdog' }

if (-not $targets) {
    Say "Нечего останавливать — ничего не запущено." "Gray"
} else {
    foreach ($p in $targets) {
        $name = if ($p.CommandLine -match 'telegram_bot') { "Telegram-бот" }
                elseif ($p.CommandLine -match 'admin_server') { "Панель" }
                else { "watchdog" }
        Stop-Process -Id $p.ProcessId -Force
        Say "[OK] $name остановлен (PID $($p.ProcessId))" "Green"
    }
    Start-Sleep -Milliseconds 800
}

# Контрольная проверка: не осталось ли живых процессов.
$left = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'telegram_bot\.py|admin_server\.py|neft_watchdog' }
if ($left) {
    Say "[!] Не удалось погасить: $($left.Count) процесс(ов)" "Yellow"
    $left | ForEach-Object { Write-Host "    PID $($_.ProcessId)" }
} elseif ($targets) {
    Write-Host ""
    Say "Всё остановлено." "Green"
}

if (-not $Quiet) {
    Write-Host ""
    Say "Запустить снова: ЗАПУСК.bat" "Gray"
    Write-Host ""
    Read-Host "Нажмите Enter для выхода"
}
