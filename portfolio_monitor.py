"""
Portfolio Monitor — Claude's real-time control interface for AlpacaBot.

This module gives Claude (the Telegram bot) full visibility into:
  1. Live account state (equity, P&L, buying power)
  2. All open positions from Alpaca API (not just our tracked file)
  3. All open/pending orders
  4. Position-level P&L with real-time quotes
  5. Force-liquidation of any or all positions

Claude reads this via trade_reviewer or directly to make decisions about
whether to hold, close, or liquidate positions — independent of the bot's
own exit rules.

Usage from Claude (subprocess):
    python portfolio_monitor.py status          → full portfolio snapshot
    python portfolio_monitor.py positions       → open positions + live P&L
    python portfolio_monitor.py orders           → pending orders
    python portfolio_monitor.py liquidate <symbol>  → close specific position
    python portfolio_monitor.py liquidate_all    → emergency: close everything
    python portfolio_monitor.py cancel <order_id> → cancel a pending order
    python portfolio_monitor.py cancel_all       → cancel all pending orders
    python portfolio_monitor.py history [N]      → last N trades from journal
"""
import sys
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ClosePositionRequest, GetOrdersRequest, QueryOrderStatus,
)
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest

import config
from trade_journal import (
    log_activity, record_trade, load_trade_history,
    get_today_trade_count, get_today_pnl, get_stats_summary,
)

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("PortfolioMonitor")

ET = ZoneInfo("America/New_York")
POSITIONS_FILE = Path("/workspace/AlpacaBot/positions.json")
CONTROL_FILE = Path("/workspace/AlpacaBot/control.json")


class PortfolioMonitor:
    def __init__(self):
        paper = config.TRADING_MODE.lower() == "paper"
        self.trading_client = TradingClient(
            config.API_KEY, config.SECRET_KEY, paper=paper
        )
        self.option_data = OptionHistoricalDataClient(
            config.API_KEY, config.SECRET_KEY
        )
        self.paper = paper

    # ═══════════════════════════════════════════════════════════
    # Account Status
    # ═══════════════════════════════════════════════════════════

    def get_account_status(self) -> dict:
        """Full account snapshot."""
        acct = self.trading_client.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "pnl_today": float(acct.equity) - float(acct.last_equity),
            "pnl_today_pct": (float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity) * 100 if float(acct.last_equity) > 0 else 0,
            "last_equity": float(acct.last_equity),
            "status": acct.status.value if hasattr(acct.status, 'value') else str(acct.status),
            "trading_blocked": acct.trading_blocked,
            "account_blocked": acct.account_blocked,
            "mode": "paper" if self.paper else "LIVE",
            "timestamp": datetime.now(ET).isoformat(),
        }

    # ═══════════════════════════════════════════════════════════
    # Live Positions (from Alpaca API, not our file)
    # ═══════════════════════════════════════════════════════════

    def get_live_positions(self) -> list:
        """Get all open positions directly from Alpaca API."""
        positions = self.trading_client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                "symbol": pos.symbol,
                "asset_class": str(pos.asset_class),
                "qty": float(pos.qty),
                "side": pos.side.value if hasattr(pos.side, 'value') else str(pos.side),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "cost_basis": float(pos.cost_basis),
                "unrealized_pnl": float(pos.unrealized_pl),
                "unrealized_pnl_pct": float(pos.unrealized_plpc) * 100,
                "change_today_pct": float(pos.change_today) * 100 if pos.change_today else 0,
            })
        return result

    def get_tracked_positions(self) -> list:
        """Get positions from our local tracking file (bot's view)."""
        if POSITIONS_FILE.exists():
            try:
                positions = json.loads(POSITIONS_FILE.read_text())
                return [p for p in positions if p.get("status") == "open"]
            except Exception:
                return []
        return []

    # ═══════════════════════════════════════════════════════════
    # Orders
    # ═══════════════════════════════════════════════════════════

    def get_open_orders(self) -> list:
        """Get all open/pending orders from Alpaca."""
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self.trading_client.get_orders(request)
        result = []
        for order in orders:
            result.append({
                "id": str(order.id),
                "symbol": order.symbol,
                "side": order.side.value if hasattr(order.side, 'value') else str(order.side),
                "qty": float(order.qty) if order.qty else 0,
                "type": order.type.value if hasattr(order.type, 'value') else str(order.type),
                "limit_price": float(order.limit_price) if order.limit_price else None,
                "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
                "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
                "filled_qty": float(order.filled_qty) if order.filled_qty else 0,
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            })
        return result

    def get_recent_orders(self, limit: int = 20) -> list:
        """Get recent filled/cancelled orders."""
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=limit,
        )
        orders = self.trading_client.get_orders(request)
        result = []
        for order in orders:
            result.append({
                "id": str(order.id),
                "symbol": order.symbol,
                "side": order.side.value if hasattr(order.side, 'value') else str(order.side),
                "qty": float(order.qty) if order.qty else 0,
                "type": order.type.value if hasattr(order.type, 'value') else str(order.type),
                "limit_price": float(order.limit_price) if order.limit_price else None,
                "status": order.status.value if hasattr(order.status, 'value') else str(order.status),
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
                "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else None,
            })
        return result

    # ═══════════════════════════════════════════════════════════
    # Liquidation Controls
    # ═══════════════════════════════════════════════════════════

    def liquidate_position(self, symbol: str) -> dict:
        """Force-close a specific position."""
        try:
            self.trading_client.close_position(symbol)
            result = {
                "action": "liquidated",
                "symbol": symbol,
                "timestamp": datetime.now(ET).isoformat(),
                "status": "success",
            }
            log_activity("force_liquidate", result)

            # Update local tracking file too
            self._mark_position_closed(symbol, "force_liquidated_by_claude")

            return result
        except Exception as e:
            return {
                "action": "liquidate_failed",
                "symbol": symbol,
                "error": str(e),
                "timestamp": datetime.now(ET).isoformat(),
            }

    def liquidate_all(self) -> dict:
        """Emergency: close ALL positions."""
        try:
            self.trading_client.close_all_positions(cancel_orders=True)
            result = {
                "action": "liquidated_all",
                "timestamp": datetime.now(ET).isoformat(),
                "status": "success",
            }
            log_activity("emergency_liquidate_all", result)

            # Mark all local positions closed
            self._mark_all_closed("emergency_liquidate_all")

            return result
        except Exception as e:
            return {
                "action": "liquidate_all_failed",
                "error": str(e),
                "timestamp": datetime.now(ET).isoformat(),
            }

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a specific pending order."""
        try:
            self.trading_client.cancel_order_by_id(order_id)
            result = {
                "action": "cancelled_order",
                "order_id": order_id,
                "timestamp": datetime.now(ET).isoformat(),
                "status": "success",
            }
            log_activity("cancel_order", result)
            return result
        except Exception as e:
            return {
                "action": "cancel_failed",
                "order_id": order_id,
                "error": str(e),
            }

    def cancel_all_orders(self) -> dict:
        """Cancel all pending orders."""
        try:
            self.trading_client.cancel_orders()
            result = {
                "action": "cancelled_all_orders",
                "timestamp": datetime.now(ET).isoformat(),
                "status": "success",
            }
            log_activity("cancel_all_orders", result)
            return result
        except Exception as e:
            return {
                "action": "cancel_all_failed",
                "error": str(e),
            }

    # ═══════════════════════════════════════════════════════════
    # Full Status Report (what Claude reads)
    # ═══════════════════════════════════════════════════════════

    def full_status(self) -> dict:
        """
        Complete portfolio snapshot for Claude's decision-making.
        Returns everything needed to evaluate the current state.
        """
        account = self.get_account_status()
        live_positions = self.get_live_positions()
        tracked_positions = self.get_tracked_positions()
        open_orders = self.get_open_orders()
        recent_orders = self.get_recent_orders(10)

        # Load control state
        control = {}
        if CONTROL_FILE.exists():
            try:
                control = json.loads(CONTROL_FILE.read_text())
            except Exception:
                pass

        # Journal stats
        history = load_trade_history()
        stats = history.get("stats", {})

        return {
            "account": account,
            "live_positions": live_positions,
            "live_position_count": len(live_positions),
            "tracked_positions": tracked_positions,
            "tracked_position_count": len(tracked_positions),
            "open_orders": open_orders,
            "open_order_count": len(open_orders),
            "recent_orders": recent_orders,
            "control": control,
            "journal_stats": stats,
            "today_trades": get_today_trade_count(),
            "today_pnl": get_today_pnl(),
            "timestamp": datetime.now(ET).isoformat(),
        }

    # ═══════════════════════════════════════════════════════════
    # Helper — sync local tracking file with API reality
    # ═══════════════════════════════════════════════════════════

    def _mark_position_closed(self, symbol: str, reason: str):
        """Mark a position as closed in our local tracking file."""
        if not POSITIONS_FILE.exists():
            return
        try:
            positions = json.loads(POSITIONS_FILE.read_text())
            for pos in positions:
                if pos.get("status") == "open":
                    # Match by underlying or any leg symbol
                    if pos.get("underlying", "").upper() == symbol.upper():
                        pos["status"] = "closed"
                        pos["close_time"] = datetime.now(ET).isoformat()
                        pos["close_reason"] = reason
                    elif any(l.get("symbol", "").startswith(symbol.upper()) for l in pos.get("legs", [])):
                        pos["status"] = "closed"
                        pos["close_time"] = datetime.now(ET).isoformat()
                        pos["close_reason"] = reason
            POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
        except Exception:
            pass

    def _mark_all_closed(self, reason: str):
        """Mark all positions as closed in local tracking file."""
        if not POSITIONS_FILE.exists():
            return
        try:
            positions = json.loads(POSITIONS_FILE.read_text())
            for pos in positions:
                if pos.get("status") == "open":
                    pos["status"] = "closed"
                    pos["close_time"] = datetime.now(ET).isoformat()
                    pos["close_reason"] = reason
            POSITIONS_FILE.write_text(json.dumps(positions, indent=2))
        except Exception:
            pass

    def reconcile_positions(self) -> dict:
        """
        Compare Alpaca API positions vs local tracking file.
        Finds orphans (API has it, we don't track it) and ghosts
        (we track it, API doesn't have it).
        """
        live = {p["symbol"]: p for p in self.get_live_positions()}
        tracked = self.get_tracked_positions()

        tracked_symbols = set()
        for pos in tracked:
            for leg in pos.get("legs", []):
                tracked_symbols.add(leg.get("symbol", ""))

        live_symbols = set(live.keys())

        orphans = live_symbols - tracked_symbols  # API has, we don't track
        ghosts = tracked_symbols - live_symbols    # We track, API doesn't have

        result = {
            "in_sync": len(orphans) == 0 and len(ghosts) == 0,
            "orphan_positions": [live[s] for s in orphans if s in live],
            "ghost_positions": list(ghosts),
            "live_count": len(live_symbols),
            "tracked_count": len(tracked_symbols),
        }

        if not result["in_sync"]:
            log_activity("reconciliation_mismatch", result)

        return result


# ═══════════════════════════════════════════════════════════
# CLI Interface (for subprocess calls from Claude)
# ═══════════════════════════════════════════════════════════

def _format_account(account: dict) -> str:
    pnl_emoji = "📈" if account["pnl_today"] >= 0 else "📉"
    return (
        f"{'═' * 45}\n"
        f"  ALPACABOT ACCOUNT STATUS ({account['mode'].upper()})\n"
        f"{'═' * 45}\n"
        f"  Equity:       ${account['equity']:>12,.2f}\n"
        f"  Cash:         ${account['cash']:>12,.2f}\n"
        f"  Buying Power: ${account['buying_power']:>12,.2f}\n"
        f"  {pnl_emoji} Day P&L:    ${account['pnl_today']:>+12,.2f} ({account['pnl_today_pct']:+.2f}%)\n"
        f"  Status:       {account['status']}\n"
        f"{'═' * 45}"
    )


def _format_positions(positions: list) -> str:
    if not positions:
        return "  No open positions."
    lines = []
    total_pnl = 0
    for p in positions:
        emoji = "🟢" if p["unrealized_pnl"] >= 0 else "🔴"
        lines.append(
            f"  {emoji} {p['symbol']:<30s} | "
            f"Qty: {p['qty']:>5.0f} | "
            f"Entry: ${p['avg_entry_price']:>8.2f} | "
            f"Now: ${p['current_price']:>8.2f} | "
            f"P&L: ${p['unrealized_pnl']:>+8.2f} ({p['unrealized_pnl_pct']:>+.1f}%)"
        )
        total_pnl += p["unrealized_pnl"]
    lines.append(f"  {'─' * 60}")
    lines.append(f"  Total Unrealized P&L: ${total_pnl:+,.2f}")
    return "\n".join(lines)


def _format_orders(orders: list) -> str:
    if not orders:
        return "  No open orders."
    lines = []
    for o in orders:
        price_str = f"@ ${o['limit_price']:.2f}" if o['limit_price'] else "MKT"
        lines.append(
            f"  {o['side'].upper():>4s} {o['symbol']:<30s} | "
            f"Qty: {o['qty']:>5.0f} | "
            f"{o['type']:>6s} {price_str} | "
            f"Status: {o['status']} | "
            f"ID: {o['id'][:8]}..."
        )
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python portfolio_monitor.py <command> [args]")
        print("Commands: status, positions, orders, recent, liquidate <symbol>,")
        print("          liquidate_all, cancel <order_id>, cancel_all,")
        print("          history [N], reconcile, stats")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    monitor = PortfolioMonitor()

    if cmd == "status":
        status = monitor.full_status()
        print(_format_account(status["account"]))
        print(f"\n📊 LIVE POSITIONS ({status['live_position_count']}):")
        print(_format_positions(status["live_positions"]))
        print(f"\n📋 OPEN ORDERS ({status['open_order_count']}):")
        print(_format_orders(status["open_orders"]))
        print(f"\n📓 Journal: {status['journal_stats'].get('total_trades', 0)} trades | "
              f"Win rate: {status['journal_stats'].get('win_rate', 0):.0%} | "
              f"Total P&L: ${status['journal_stats'].get('total_pnl', 0):+,.2f}")
        print(f"   Today: {status['today_trades']} trades | P&L: ${status['today_pnl']:+,.2f}")
        # Also dump JSON for programmatic use
        print(f"\n---JSON---\n{json.dumps(status, indent=2, default=str)}")

    elif cmd == "positions":
        positions = monitor.get_live_positions()
        print(f"📊 LIVE POSITIONS ({len(positions)}):")
        print(_format_positions(positions))
        tracked = monitor.get_tracked_positions()
        print(f"\n📁 TRACKED POSITIONS ({len(tracked)}):")
        for t in tracked:
            print(f"  {t.get('strategy', '?')} {t.get('underlying', '?')} | "
                  f"{t.get('contracts', 0)} contracts | "
                  f"Entered: {t.get('entry_time', '?')}")
        print(f"\n---JSON---\n{json.dumps({'live': positions, 'tracked': tracked}, indent=2, default=str)}")

    elif cmd == "orders":
        orders = monitor.get_open_orders()
        print(f"📋 OPEN ORDERS ({len(orders)}):")
        print(_format_orders(orders))
        print(f"\n---JSON---\n{json.dumps(orders, indent=2, default=str)}")

    elif cmd == "recent":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        orders = monitor.get_recent_orders(limit)
        print(f"📜 RECENT ORDERS (last {limit}):")
        for o in orders:
            price_str = f"@ ${o['filled_avg_price']:.2f}" if o['filled_avg_price'] else "unfilled"
            print(f"  {o['status']:>10s} | {o['side'].upper():>4s} {o['symbol']:<30s} | "
                  f"Qty: {o['qty']:>5.0f} {price_str}")
        print(f"\n---JSON---\n{json.dumps(orders, indent=2, default=str)}")

    elif cmd == "liquidate":
        if len(sys.argv) < 3:
            print("Usage: python portfolio_monitor.py liquidate <symbol>")
            sys.exit(1)
        symbol = sys.argv[2]
        result = monitor.liquidate_position(symbol)
        print(json.dumps(result, indent=2))

    elif cmd == "liquidate_all":
        result = monitor.liquidate_all()
        print(json.dumps(result, indent=2))

    elif cmd == "cancel":
        if len(sys.argv) < 3:
            print("Usage: python portfolio_monitor.py cancel <order_id>")
            sys.exit(1)
        order_id = sys.argv[2]
        result = monitor.cancel_order(order_id)
        print(json.dumps(result, indent=2))

    elif cmd == "cancel_all":
        result = monitor.cancel_all_orders()
        print(json.dumps(result, indent=2))

    elif cmd == "history":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        history = load_trade_history()
        trades = history.get("trades", [])[-n:]
        print(f"📓 LAST {len(trades)} TRADES:")
        for t in trades:
            emoji = "💰" if t.get("realized_pnl", 0) > 0 else "📉"
            print(f"  {emoji} #{t.get('trade_number', '?')} {t.get('strategy', '?')} {t.get('underlying', '?')} | "
                  f"P&L: ${t.get('realized_pnl', 0):+,.2f} | {t.get('close_reason', '?')}")
        print(f"\n{get_stats_summary()}")
        print(f"\n---JSON---\n{json.dumps(trades, indent=2, default=str)}")

    elif cmd == "reconcile":
        result = monitor.reconcile_positions()
        if result["in_sync"]:
            print("✅ Positions in sync (API matches local tracking)")
        else:
            print("⚠️ MISMATCH DETECTED:")
            if result["orphan_positions"]:
                print(f"  Orphans (API has, we don't track): {len(result['orphan_positions'])}")
                for o in result["orphan_positions"]:
                    print(f"    {o['symbol']} | {o['qty']} shares | P&L: ${o['unrealized_pnl']:+,.2f}")
            if result["ghost_positions"]:
                print(f"  Ghosts (we track, API doesn't have): {len(result['ghost_positions'])}")
                for g in result["ghost_positions"]:
                    print(f"    {g}")
        print(f"\n---JSON---\n{json.dumps(result, indent=2, default=str)}")

    elif cmd == "stats":
        print(get_stats_summary())

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
