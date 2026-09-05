# Ручной запуск NEFT: telegram_bot + admin_server. Автозапуск не трогаем.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Say([string]$t, [string]$c = "Gray") { Write-Host $t -ForegroundColor $c }

Write-Host ("=" * 52) -ForegroundColor Cyan
Write-Host "  NEFT - запуск" -ForegroundColor Cyan
Write-Host ("=" * 52) -ForegroundColor Cyan
Write-Host ""

$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Say "[ОШИБКА] Нет .venv — сначала запустите УСТАНОВКА.bat" "Red"
    Read-Host "`nНажмите Enter для выхода"
    exit 1
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    Say "[!] Нет .env — панель может не запуститься. Запустите УСТАНОВКА.bat" "Yellow"
}

# Гасим то, что уже запущено, чтобы не плодить дубли: два admin_server
# на одном порту дают зависший вход в панель.
$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'telegram_bot\.py|admin_server\.py' }
if ($running) {
    Say "Нашёл уже запущенные процессы — перезапускаю их" "Yellow"
    foreach ($p in $running) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

function Start-Piece([string]$script, [string]$label) {
    $path = Join-Path $Root $script
    if (-not (Test-Path $path)) {
        Say "[!] Не найден $script — пропускаю" "Yellow"
        return $false
    }
    Start-Process -FilePath $venvPy -ArgumentList "`"$path`"" `
        -WorkingDirectory $Root -WindowStyle Hidden
    Say "[OK] $label запущен" "Green"
    return $true
}

Start-Piece "scripts\telegram_bot.py" "Telegram-бот" | Out-Null
Start-Sleep -Seconds 2
Start-Piece "scripts\admin_server.py" "Панель" | Out-Null

# Ждём, пока панель реально поднимет порт.
Write-Host ""
Say "Жду ответа панели..." "Yellow"
$ok = $false
foreach ($i in 1..20) {
    Start-Sleep -Milliseconds 700
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8787/" -TimeoutSec 3 `
             -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -ge 200) { $ok = $true; break }
    } catch {
        # 401/403 тоже значит, что сервер живой и отвечает
        if ($_.Exception.Response) { $ok = $true; break }
    }
}

Write-Host ""
if ($ok) {
    Write-Host ("=" * 52) -ForegroundColor Green
    Say "  Запущено. Панель: http://127.0.0.1:8787" "Green"
    Write-Host ("=" * 52) -ForegroundColor Green
    Write-Host ""
    Say "Вход подтверждается кнопкой в Telegram." "Gray"
    Say "Остановить: ОСТАНОВКА.bat    Перезапустить: РЕСТАРТ.bat" "Gray"
    Write-Host ""
    $ans = Read-Host "Открыть панель в браузере? (y/n)"
    if ($ans -match "^[yYдД]") { Start-Process "http://127.0.0.1:8787" }
} else {
    Say "[!] Панель не ответила за 14 секунд." "Yellow"
    Say "    Процессы запущены, но порт 8787 молчит." "Yellow"
    Say "    Смотрите logs/ или запустите вручную, чтобы увидеть ошибку:" "Yellow"
    Write-Host "      .venv\Scripts\python.exe scripts\admin_server.py"
    Read-Host "`nНажмите Enter для выхода"
}
