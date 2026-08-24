# NEFT — установка и настройка одним запуском.
# Идемпотентен: можно запускать сколько угодно раз, ничего не ломает
# и не перезаписывает уже существующий .env.
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Say([string]$text, [string]$color = "Gray") {
    Write-Host $text -ForegroundColor $color
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  NEFT - установка и настройка" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# 1. Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Say "[ОШИБКА] Python не найден в PATH." "Red"
    Say "Поставьте Python 3.11+ с https://python.org и включите" "Red"
    Say "'Add python.exe to PATH' в установщике, затем запустите заново." "Red"
    Read-Host "Нажмите Enter для выхода"
    exit 1
}
$pyVer = (& python --version) 2>&1
Say "[OK] $pyVer" "Green"

# 2. Виртуальное окружение
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Say "[..] Создаю виртуальное окружение .venv" "Yellow"
    python -m venv .venv
    if (-not (Test-Path $venvPy)) {
        Say "[ОШИБКА] Не удалось создать .venv" "Red"
        Read-Host "Нажмите Enter для выхода"
        exit 1
    }
} else {
    Say "[OK] .venv уже есть" "Green"
}

# 3. Зависимости
Say "[..] Ставлю зависимости из requirements.txt (может занять пару минут)" "Yellow"
& $venvPy -m pip install --upgrade pip -q
& $venvPy -m pip install -r requirements.txt -q
if ($LASTEXITCODE -ne 0) {
    Say "[ОШИБКА] pip install не прошёл — смотрите вывод выше." "Red"
    Read-Host "Нажмите Enter для выхода"
    exit 1
}
Say "[OK] Зависимости установлены" "Green"

# 4. .env
$needEnv = $false
$envPath = Join-Path $Root ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $Root ".env.example") $envPath
    Say "[OK] Создан .env из шаблона — DEMO_ONLY=true, ключи биржи пустые." "Green"
    $needEnv = $true
} else {
    Say "[OK] .env уже есть, не трогаю" "Green"
}

# 5. Папки для логов/рантайма
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\crypto") | Out-Null
Say "[OK] Папки logs/, data/crypto/ на месте" "Green"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Готово" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

if ($needEnv) {
    Write-Host ""
    Say "Перед запуском откройте .env и впишите свои ключи:" "Yellow"
    Say "  - BYBIT_API_KEY / BYBIT_API_SECRET  (крипта)" "Yellow"
    Say "  - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER  (форекс/CFD, если нужно)" "Yellow"
    Say "  - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (уведомления, необязательно)" "Yellow"
    Say "Пока ключей нет — бот всё равно запустится в ТЕСТ-режиме на живых" "Gray"
    Say "котировках без ключей, DEMO_ONLY=true не даст уйти в бой по ошибке." "Gray"
    Write-Host ""
    Start-Process notepad $envPath
}
Write-Host "Запуск панели:  start_neft_services.bat"
Write-Host "Панель:         http://127.0.0.1:8787"
Write-Host ""
Read-Host "Нажмите Enter для выхода"
