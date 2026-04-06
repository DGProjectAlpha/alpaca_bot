"""
Trade Journal — Persistent activity log and P&L tracker for AlpacaBot.

Three systems:
1. Activity Log (activity_log.jsonl) — append-only log of every bot action
2. Trade History (trade_history.json) — cumulative record of all trades ever taken
3. Daily Summary (daily_summaries/) — end-of-day snapshots for trend analysis

Claude reads these files to have full context on what the bot has done,
what worked, what didn't, and make informed decisions going forward.
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("AlpacaBotOptions")
ET = ZoneInfo("America/New_York")

JOURNAL_DIR = Path("/workspace/AlpacaBot/journal")
ACTIVITY_LOG = JOURNAL_DIR / "activity_log.jsonl"
TRADE_HISTORY = JOURNAL_DIR / "trade_history.json"
DAILY_DIR = JOURNAL_DIR / "daily_summaries"


def _ensure_dirs():
    JOURNAL_DIR.mkdir(exist_ok=True)
    DAILY_DIR.mkdir(exist_ok=True)


def log_activity(event_type: str, data: dict):
    """
    Append an event to the activity log.

    event_type: scan, proposal, review, auto_approve, auto_reject,
                claude_review, execute, close, pause, resume, error,
                morning_briefing, afternoon_briefing, control_update
    """
    _ensure_dirs()
    entry = {
        "timestamp": datetime.now(ET).isoformat(),
        "event": event_type,
        **data,
    }
    with open(ACTIVITY_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def record_trade(trade: dict):
    """
    Add a completed trade to the cumulative trade history.
    Tracks: entry, exit, P&L, strategy, duration, win/loss.
    """
    _ensure_dirs()
    history = load_trade_history()

    trade["trade_number"] = len(history["trades"]) + 1
    history["trades"].append(trade)

    # Update running stats
    pnl = trade.get("realized_pnl", 0)
    history["stats"]["total_trades"] += 1
    history["stats"]["total_pnl"] += pnl
    if pnl > 0:
        history["stats"]["wins"] += 1
        history["stats"]["total_profit"] += pnl
    elif pnl < 0:
        history["stats"]["losses"] += 1
        history["stats"]["total_loss"] += pnl
    else:
        history["stats"]["breakeven"] += 1

    total = history["stats"]["total_trades"]
    wins = history["stats"]["wins"]
    history["stats"]["win_rate"] = wins / total if total > 0 else 0
    history["stats"]["avg_pnl"] = history["stats"]["total_pnl"] / total if total > 0 else 0

    if wins > 0:
        history["stats"]["avg_win"] = history["stats"]["total_profit"] / wins
    if history["stats"]["losses"] > 0:
        history["stats"]["avg_loss"] = history["stats"]["total_loss"] / history["stats"]["losses"]

    history["last_updated"] = datetime.now(ET).isoformat()
    TRADE_HISTORY.write_text(json.dumps(history, indent=2, default=str))

    log.info(f"Trade #{trade['trade_number']} recorded: {trade.get('strategy', '?')} {trade.get('underlying', '?')} → P&L: ${pnl:+,.0f}")


def load_trade_history() -> dict:
    """Load cumulative trade history."""
    if TRADE_HISTORY.exists():
        try:
            return json.loads(TRADE_HISTORY.read_text())
        except Exception:
            pass
    return {
        "trades": [],
        "stats": {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "total_pnl": 0.0,
            "total_profit": 0.0,
            "total_loss": 0.0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        },
        "last_updated": None,
    }


def save_daily_summary(date_str: str, summary: dict):
    """Save end-of-day summary for historical analysis."""
    _ensure_dirs()
    filepath = DAILY_DIR / f"{date_str}.json"
    filepath.write_text(json.dumps(summary, indent=2, default=str))
    log.info(f"Daily summary saved: {filepath}")


def get_today_activity(event_type: str = None) -> list:
    """Read today's activity log entries, optionally filtered by event type."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    entries = []
    if not ACTIVITY_LOG.exists():
        return entries
    with open(ACTIVITY_LOG, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("timestamp", "").startswith(today):
                    if event_type is None or entry.get("event") == event_type:
                        entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def get_today_trade_count() -> int:
    """Count trades executed today."""
    return len(get_today_activity("execute"))


def get_today_pnl() -> float:
    """Calculate realized P&L from today's closed trades."""
    closures = get_today_activity("close")
    return sum(c.get("realized_pnl", 0) for c in closures)


def get_stats_summary() -> str:
    """Generate a human-readable stats summary for Claude/Telegram."""
    history = load_trade_history()
    s = history["stats"]
    today_trades = get_today_trade_count()
    today_pnl = get_today_pnl()

    lines = [
        "📊 ALPACABOT LIFETIME STATS",
        f"Total trades: {s['total_trades']}",
        f"Win rate: {s['win_rate']:.0%} ({s['wins']}W / {s['losses']}L / {s['breakeven']}B)",
        f"Total P&L: ${s['total_pnl']:+,.2f}",
        f"Avg P&L: ${s['avg_pnl']:+,.2f}",
        f"Avg Win: ${s['avg_win']:+,.2f} | Avg Loss: ${s['avg_loss']:+,.2f}",
        "",
        f"Today: {today_trades} trades | P&L: ${today_pnl:+,.2f}",
    ]
    return "\n".join(lines)
