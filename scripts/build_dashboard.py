"""Вставляет данные бэктеста в шаблон панели.

    python scripts/sweep.py && python scripts/build_dashboard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "dashboard" / "template.html"
DATA = ROOT / "logs" / "dashboard.json"
OUT = ROOT / "dashboard" / "index.html"

if not DATA.exists():
    raise SystemExit(f"Нет {DATA} — сначала запустите scripts/sweep.py")

html = TEMPLATE.read_text(encoding="utf-8")
data = DATA.read_text(encoding="utf-8")
# </script> внутри JSON закрыл бы тег раньше времени.
data = data.replace("</", "<\\/")
OUT.write_text(html.replace("/*__DATA__*/", data), encoding="utf-8")
print(f"{OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")
