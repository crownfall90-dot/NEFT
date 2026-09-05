# NEFT — установка на новом компьютере. Автозапуск НЕ настраивается:
# сервисы поднимаются только вручную через ЗАПУСК.bat.
# Идемпотентен: запускать можно сколько угодно раз.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Say([string]$t, [string]$c = "Gray") { Write-Host $t -ForegroundColor $c }
function Head([string]$t) {
    Write-Host ""
    Write-Host ("=" * 52) -ForegroundColor Cyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ("=" * 52) -ForegroundColor Cyan
}

Head "NEFT - установка"
Say "Папка: $Root"

# ── 1. Python ────────────────────────────────────────────────────────
Write-Host ""
Say "[1/6] Проверяю Python..." "Yellow"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Say "[ОШИБКА] Python не найден в PATH." "Red"
    Say ""
    Say "Что сделать:" "Yellow"
    Say "  1. Скачать Python 3.11+ с https://python.org/downloads/"
    Say "  2. В установщике ОБЯЗАТЕЛЬНО отметить 'Add python.exe to PATH'"
    Say "  3. Перезапустить этот файл"
    Read-Host "`nНажмите Enter для выхода"
    exit 1
}
$ver = (& python --version 2>&1) -replace "Python ", ""
$parts = $ver.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 10)) {
    Say "[ОШИБКА] Нужен Python 3.10+, установлен $ver" "Red"
    Read-Host "`nНажмите Enter для выхода"
    exit 1
}
Say "[OK] Python $ver" "Green"

# ── 2. Виртуальное окружение ─────────────────────────────────────────
Write-Host ""
Say "[2/6] Виртуальное окружение..." "Yellow"
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    Say "[OK] .venv уже есть" "Green"
} else {
    Say "     создаю .venv (несколько секунд)"
    & python -m venv .venv
    if (-not (Test-Path $venvPy)) {
        Say "[ОШИБКА] Не удалось создать .venv" "Red"
        Read-Host "`nНажмите Enter для выхода"
        exit 1
    }
    Say "[OK] .venv создан" "Green"
}

# ── 3. Зависимости ───────────────────────────────────────────────────
Write-Host ""
Say "[3/6] Ставлю зависимости (2-5 минут, скачивается ~200 МБ)..." "Yellow"
& $venvPy -m pip install --upgrade pip -q 2>&1 | Out-Null
& $venvPy -m pip install -r (Join-Path $Root "requirements.txt") -q
if ($LASTEXITCODE -ne 0) {
    Say "[!] Часть пакетов не встала." "Yellow"
    Say "    metatrader5 и winloop работают только на Windows —" "Yellow"
    Say "    если вы не на Windows, это ожидаемо." "Yellow"
    Say "    Ставлю минимум для стратегии BTC up/down..." "Yellow"
    & $venvPy -m pip install pandas numpy requests -q
    if ($LASTEXITCODE -ne 0) {
        Say "[ОШИБКА] Не удалось поставить даже базовые пакеты." "Red"
        Read-Host "`nНажмите Enter для выхода"
        exit 1
    }
}
$check = & $venvPy -c "import pandas, numpy, requests; print('ok')" 2>&1
if ($check -notmatch "ok") {
    Say "[ОШИБКА] pandas/numpy/requests не импортируются: $check" "Red"
    Read-Host "`nНажмите Enter для выхода"
    exit 1
}
Say "[OK] Зависимости на месте" "Green"

# ── 4. Конфигурация ──────────────────────────────────────────────────
Write-Host ""
Say "[4/6] Конфигурация .env..." "Yellow"
$envPath = Join-Path $Root ".env"
$newEnv = $false
if (Test-Path $envPath) {
    Say "[OK] .env уже есть, не трогаю" "Green"
} else {
    $sample = Join-Path $Root ".env.example"
    if (Test-Path $sample) {
        Copy-Item $sample $envPath
        $newEnv = $true
        Say "[OK] Создан .env из .env.example" "Green"
    } else {
        Say "[!] Нет ни .env, ни .env.example — панель может не запуститься" "Yellow"
    }
}

# ── 5. Папки ─────────────────────────────────────────────────────────
Write-Host ""
Say "[5/6] Рабочие папки..." "Yellow"
foreach ($d in @("logs", "data\crypto", "data\updown")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null
}
Say "[OK] logs/, data/crypto/, data/updown/" "Green"

# ── 6. Автозапуск: убеждаемся, что его НЕТ ───────────────────────────
Write-Host ""
Say "[6/6] Проверяю автозапуск (его быть не должно)..." "Yellow"
$task = Get-ScheduledTask -TaskName "NEFT Services Logon" -ErrorAction SilentlyContinue
if ($task) {
    Say "     нашёл задачу автозапуска от прошлой установки — отключаю"
    cmd /c 'schtasks /Change /TN "NEFT Services Logon" /DISABLE >nul 2>nul'
    Say "[OK] Автозапуск отключён" "Green"
} else {
    Say "[OK] Автозапуска нет — как и задумано" "Green"
}

# ── Итог ─────────────────────────────────────────────────────────────
Head "Готово"
Write-Host ""
Say "Управление (запускать вручную, автостарта нет):" "Cyan"
Write-Host "  ЗАПУСК.bat      - включить бота и панель"
Write-Host "  ОСТАНОВКА.bat   - выключить всё"
Write-Host "  РЕСТАРТ.bat     - перезапустить"
Write-Host ""
Say "Панель после запуска: http://127.0.0.1:8787" "Cyan"
Write-Host ""
Say "Стратегия BTC up/down (данные качаются сами при первом запуске):" "Cyan"
Write-Host "  .venv\Scripts\python.exe scripts\updown_report2.py --days 3"
Write-Host ""

if ($newEnv) {
    Say "ВАЖНО: в .env проверьте пути MT5_PATH и MT5_DEMO_PATH" "Yellow"
    Say "под установку MetaTrader на ЭТОМ компьютере." "Yellow"
    Write-Host ""
    $ans = Read-Host "Открыть .env в блокноте? (y/n)"
    if ($ans -match "^[yYдД]") { Start-Process notepad $envPath }
}
Read-Host "Нажмите Enter для выхода"
