"""Вставляет данные scripts/crypto_yearly.py в шаблон дашборда.

    python scripts/crypto_yearly.py --days 365 --risk 0.5
    python scripts/build_crypto_dashboard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "crypto_template.html"
DATA = ROOT / "logs" / "crypto_yearly.json"
OUT = ROOT / "dashboard" / "crypto_index.html"

if not DATA.exists():
    raise SystemExit(f"Нет {DATA} — сначала запустите scripts/crypto_yearly.py")

html = TEMPLATE.read_text(encoding="utf-8")
data = DATA.read_text(encoding="utf-8")
data = data.replace("</", "<\\/")
OUT.write_text(html.replace("/*__DATA__*/", data), encoding="utf-8")
print(f"{OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")
