"""ИИ-разбор по методичке Crypto Daily, на данных NEFT.

    python scripts/ai_trade.py
    python scripts/ai_trade.py tldr
    python scripts/ai_trade.py devil "лонг BTC от 68k"
    python scripts/ai_trade.py premortem HYPE
    python scripts/ai_trade.py compare "BTC vs HYPE"
    python scripts/ai_trade.py analyze
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.panel import Panel

from neft.core.ai_trade import COMMANDS, DISPATCH

con = Console()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("command", nargs="?", default="tldr",
                   choices=COMMANDS, help="команда из блока Think & Decide")
    p.add_argument("text", nargs="*", help="тезис / план / монета")
    p.add_argument("--refresh", action="store_true")
    a = p.parse_args()
    if a.refresh:
        from neft.core.crypto_feed import fetch_posts
        fetch_posts(refresh=True)
    arg = " ".join(a.text)
    fn = DISPATCH[a.command]
    text = fn(arg)
    con.print(Panel(text, title=f"NEFT · /{a.command.upper()}",
                    subtitle="не сигнал, ордер не ставится", border_style="cyan"))
