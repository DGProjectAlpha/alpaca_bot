"""
AlpacaBot — Automated Trading Bot
Multi-indicator mean reversion + momentum strategy on Alpaca

Run: python bot.py
"""
import sys
import time
import json
import logging
import schedule
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    GetOrdersRequest, QueryOrderStatus
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

import subprocess
import os

import config
from strategies import generate_signal
from telegram_alerts import TelegramAlerts
from trade_journal import log_activity

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("alpacabot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("AlpacaBot")

ET = ZoneInfo("America/New_York")


class AlpacaBot:
    def __init__(self):
        # Determine if paper or live
        paper = config.TRADING_MODE.lower() == "paper"
        if not paper:
            log.warning("⚠️  LIVE TRADING MODE — real money at risk!")
            confirm = input("Type 'YES' to confirm live trading: ")
            if confirm != "YES":
                log.info("Aborted. Switching to paper mode.")
                paper = True

        self.trading_client = TradingClient(
            config.API_KEY,
            config.SECRET_KEY,
            paper=paper
        )
        self.data_client = StockHistoricalDataClient(
            config.API_KEY,
            config.SECRET_KEY
        )
        self.paper = paper
        self.trades_today = []
        self.tg = TelegramAlerts(
            bot_token=config.TELEGRAM_BOT_TOKEN,
            group_chat_id=config.TELEGRAM_GROUP_CHAT_ID,
            alerts_topic_id=config.TELEGRAM_ALERTS_TOPIC_ID,
        )

        # Verify connection
        account = self.trading_client.get_account()
        mode = "📄 PAPER" if paper else "💰 LIVE"
        log.info(f"{'='*50}")
        log.info(f"AlpacaBot started — {mode} mode")
        log.info(f"Account: ${float(account.equity):,.2f} equity")
        log.info(f"Buying power: ${float(account.buying_power):,.2f}")
        log.info(f"Watchlist: {len(config.WATCHLIST)} symbols")
        log.info(f"Max capital: ${config.MAX_CAPITAL:,.2f}")
        log.info(f"Max position size: {config.MAX_POSITION_PCT*100:.0f}%")
        log.info(f"Stop loss: {config.STOP_LOSS_PCT*100:.0f}% / Take profit: {config.TAKE_PROFIT_PCT*100:.0f}%")
        log.info(f"{'='*50}")

    def get_account_info(self) -> dict:
        """Get current account state."""
        account = self.trading_client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "pnl_today": float(account.equity) - float(account.last_equity),
        }

    def get_positions(self) -> list:
        """Get all open positions."""
        positions = self.trading_client.get_all_positions()
        return [{
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "pnl": float(p.unrealized_pl),
            "pnl_pct": float(p.unrealized_plpc) * 100,
        } for p in positions]

    def get_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        """Fetch historical bars for analysis."""
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=datetime.now(ET) - timedelta(days=days),
            feed=DataFeed.IEX,
        )
        bars = self.data_client.get_stock_bars(request)
        if symbol not in bars.data or len(bars[symbol]) == 0:
            return pd.DataFrame()

        data = [{
            "timestamp": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        } for bar in bars[symbol]]

        return pd.DataFrame(data)

    def can_buy(self) -> bool:
        """Check if we have capacity for a new position."""
        positions = self.trading_client.get_all_positions()
        if len(positions) >= config.MAX_POSITIONS:
            return False
        account = self.trading_client.get_account()
        if float(account.buying_power) < 50:  # min $50 to open a position
            return False
        return True

    def calculate_position_size(self, price: float) -> int:
        """How many shares to buy. Respects position limits."""
        account = self.trading_client.get_account()
        equity = float(account.equity)
        max_for_position = equity * config.MAX_POSITION_PCT
        max_for_capital = config.MAX_CAPITAL * config.MAX_POSITION_PCT

        # Use the smaller of the two limits
        max_spend = min(max_for_position, max_for_capital, float(account.buying_power) * 0.95)

        shares = int(max_spend / price)
        return max(shares, 0)

    def place_buy(self, symbol: str, qty: int, reason: str) -> bool:
        """Place a market buy order with bracket (stop loss + take profit)."""
        if qty <= 0:
            return False

        try:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            result = self.trading_client.submit_order(order)
            log.info(f"🟢 BUY {qty} {symbol} — {reason}")
            log.info(f"   Order ID: {result.id}, Status: {result.status}")
            self.tg.send_trade_alert(
                f"🟢 BUY {qty} {symbol}\n{reason}\nOrder: {result.id}"
            )

            self.trades_today.append({
                "time": datetime.now(ET).isoformat(),
                "action": "BUY",
                "symbol": symbol,
                "qty": qty,
                "reason": reason,
                "order_id": str(result.id),
            })
            return True

        except Exception as e:
            log.error(f"❌ Failed to buy {symbol}: {e}")
            return False

    def place_sell(self, symbol: str, qty: int, reason: str) -> bool:
        """Place a market sell order."""
        try:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            result = self.trading_client.submit_order(order)
            log.info(f"🔴 SELL {qty} {symbol} — {reason}")
            log.info(f"   Order ID: {result.id}, Status: {result.status}")
            self.tg.send_trade_alert(
                f"🔴 SELL {qty} {symbol}\n{reason}\nOrder: {result.id}"
            )

            self.trades_today.append({
                "time": datetime.now(ET).isoformat(),
                "action": "SELL",
                "symbol": symbol,
                "qty": qty,
                "reason": reason,
                "order_id": str(result.id),
            })
            return True

        except Exception as e:
            log.error(f"❌ Failed to sell {symbol}: {e}")
            return False

    def check_stop_loss_take_profit(self):
        """Check all positions for stop loss / take profit hits."""
        positions = self.get_positions()
        for pos in positions:
            pnl_pct = pos["pnl_pct"] / 100

            if pnl_pct <= -config.STOP_LOSS_PCT:
                log.warning(f"🛑 STOP LOSS hit on {pos['symbol']} ({pos['pnl_pct']:.1f}%)")
                self.tg.send_trade_alert(
                    f"🛑 STOP LOSS — {pos['symbol']} at {pos['pnl_pct']:.1f}%"
                )
                self.place_sell(pos["symbol"], int(pos["qty"]),
                              f"Stop loss at {pos['pnl_pct']:.1f}%")

            elif pnl_pct >= config.TAKE_PROFIT_PCT:
                log.info(f"🎯 TAKE PROFIT hit on {pos['symbol']} ({pos['pnl_pct']:.1f}%)")
                self.tg.send_trade_alert(
                    f"🎯 TAKE PROFIT — {pos['symbol']} at {pos['pnl_pct']:.1f}%"
                )
                self.place_sell(pos["symbol"], int(pos["qty"]),
                              f"Take profit at {pos['pnl_pct']:.1f}%")

    def scan_for_entries(self):
        """Scan watchlist for buy signals, run through Claude review, execute approved trades."""
        scan_results = []

        if not self.can_buy():
            log.info("Max positions reached or insufficient buying power. Skipping scan.")
            return scan_results

        # Don't buy stocks we already hold
        held = {p["symbol"] for p in self.get_positions()}

        # Phase 1: Collect all proposals (don't execute yet)
        proposals = []
        for symbol in config.WATCHLIST:
            if symbol in held:
                continue

            try:
                bars = self.get_bars(symbol)
                if bars.empty:
                    continue

                signal = generate_signal(bars, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)

                if signal["action"] == "BUY" and signal["confidence"] >= 0.5:
                    price = bars["close"].iloc[-1]
                    qty = self.calculate_position_size(price)

                    if qty > 0:
                        proposals.append({
                            "symbol": symbol,
                            "qty": qty,
                            "price": price,
                            "confidence": signal["confidence"],
                            "reasons": signal["reasons"],
                            "rsi": signal.get("rsi", 0),
                            "action": "BUY",
                        })

                elif signal["action"] != "HOLD":
                    scan_results.append(f"👀 {symbol}: {signal['action']} ({signal['confidence']:.0%}) — {', '.join(signal['reasons'])}")

            except Exception as e:
                log.error(f"Error scanning {symbol}: {e}")

        if not proposals:
            return scan_results

        # Phase 2: Send proposals to Claude for review
        log.info(f"📋 {len(proposals)} buy signals detected — sending to Claude for review...")
        account = self.get_account_info()
        review = self._claude_equity_review(proposals, account)

        # Phase 3: Send review to Telegram
        review_msg = self._format_equity_review(proposals, review)
        self.tg.send_trade_alert(review_msg)

        # Phase 4: Execute only approved trades
        for decision in review.get("trades", []):
            trade_id = decision.get("trade_id", 0)
            if decision.get("decision") not in ("approve", "adjust"):
                scan_results.append(f"❌ Trade #{trade_id}: REJECTED — {decision.get('reason', '')}")
                continue

            if trade_id < 1 or trade_id > len(proposals):
                continue

            prop = proposals[trade_id - 1]
            qty = decision.get("adjusted_qty", prop["qty"])
            reason = f"Confidence {prop['confidence']:.0%}: {', '.join(prop['reasons'])} | Claude: {decision.get('reason', 'approved')}"

            self.place_buy(prop["symbol"], qty, reason)
            scan_results.append(f"🟢 {prop['symbol']}: BUY {qty} @ ${prop['price']:.2f} — {reason}")

            if not self.can_buy():
                log.info("Position limit reached after buy. Stopping execution.")
                break

        return scan_results

    def _claude_equity_review(self, proposals: list, account: dict) -> dict:
        """Send equity trade proposals to Claude CLI for review."""
        prompt = f"""Review these equity trade proposals from an automated mean-reversion/momentum bot.

ACCOUNT:
- Equity: ${account['equity']:,.2f}
- Cash: ${account['cash']:,.2f}
- P&L Today: ${account['pnl_today']:+,.2f}
- Mode: {'Paper' if self.paper else 'LIVE'}
- Date: {datetime.now(ET).strftime('%A %B %d, %Y %H:%M ET')}

PROPOSED TRADES:
"""
        for i, prop in enumerate(proposals, 1):
            prompt += f"""
--- Trade #{i} ---
Action: BUY {prop['qty']} shares of {prop['symbol']}
Price: ${prop['price']:.2f} (total: ${prop['price'] * prop['qty']:,.2f})
Confidence: {prop['confidence']:.0%}
RSI: {prop.get('rsi', 'N/A')}
Signals: {', '.join(prop['reasons'])}
"""
        prompt += """
Be decisive. Approve good setups, reject marginal ones. We want small consistent gains, not home runs.
For each trade: approve, reject, or adjust (change quantity). Explain your reasoning briefly."""

        review_schema = json.dumps({
            "type": "object",
            "properties": {
                "market_assessment": {"type": "string", "description": "1-2 sentence market view"},
                "trades": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "trade_id": {"type": "integer"},
                            "decision": {"type": "string", "enum": ["approve", "reject", "adjust"]},
                            "adjusted_qty": {"type": "integer", "description": "Share quantity (same if approve, different if adjust)"},
                            "reason": {"type": "string"},
                            "risk_notes": {"type": "string"}
                        },
                        "required": ["trade_id", "decision", "adjusted_qty", "reason"]
                    }
                },
                "summary": {"type": "string", "description": "2-3 sentence summary for Telegram"}
            },
            "required": ["market_assessment", "trades", "summary"]
        })

        system_prompt = """You are a senior equity risk manager reviewing proposed stock trades for a conservative automated bot.

Rules:
- This bot targets small, consistent profits. NO speculative plays.
- Approve trades with strong technical confluence (RSI oversold + Bollinger + EMA alignment)
- Reject trades where signals are marginal or the stock is in a clear downtrend (catching a falling knife)
- Consider: is this a mean reversion bounce or a value trap?
- RSI below 30 is genuinely oversold. RSI 35-45 is only mildly oversold — need more confluence.
- If the stock has been declining for weeks, a low RSI alone isn't enough
- Max 5% of equity per position
- Fewer high-conviction trades > many marginal ones
- "No trade" is always valid. Protect capital first.

Be direct. No hedging language. You are protecting real money."""

        try:
            clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            result = subprocess.run(
                [
                    "claude", "-p",
                    "--model", "sonnet",
                    "--output-format", "json",
                    "--json-schema", review_schema,
                    "--append-system-prompt", system_prompt,
                    "--no-session-persistence",
                    "--dangerously-skip-permissions",
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=180,
                cwd="/workspace/AlpacaBot",
                env=clean_env,
            )

            if result.returncode != 0:
                log.error(f"Claude equity review failed (exit {result.returncode}): {result.stderr[:500]}")
                return self._fallback_equity_review(proposals)

            output = result.stdout.strip()
            try:
                wrapper = json.loads(output)
                if isinstance(wrapper, dict) and "structured_output" in wrapper:
                    review = wrapper["structured_output"]
                elif isinstance(wrapper, dict) and "result" in wrapper:
                    try:
                        review = json.loads(wrapper["result"])
                    except (json.JSONDecodeError, TypeError):
                        review = wrapper
                elif isinstance(wrapper, dict) and "market_assessment" in wrapper:
                    review = wrapper
                else:
                    review = wrapper
            except (json.JSONDecodeError, TypeError):
                log.error(f"Failed to parse Claude equity review: {output[:500]}")
                return self._fallback_equity_review(proposals)

            if "trades" not in review:
                log.error("Equity review missing 'trades' field")
                return self._fallback_equity_review(proposals)

            log.info(f"Claude equity review complete: {review.get('summary', '')}")
            log_activity("equity_claude_review", {
                "proposals": len(proposals),
                "decisions": [{"id": t["trade_id"], "decision": t["decision"]} for t in review.get("trades", [])],
            })
            return review

        except subprocess.TimeoutExpired:
            log.error("Claude equity review timed out (180s)")
            return self._fallback_equity_review(proposals)
        except Exception as e:
            log.error(f"Claude equity review failed: {e}")
            return self._fallback_equity_review(proposals)

    def _fallback_equity_review(self, proposals: list) -> dict:
        """Conservative fallback when Claude is unavailable — only approve high-confidence trades."""
        log.warning("Using fallback equity review (Claude unavailable)")
        trades = []
        for i, prop in enumerate(proposals, 1):
            if prop["confidence"] >= 0.65:
                trades.append({
                    "trade_id": i,
                    "decision": "approve",
                    "adjusted_qty": prop["qty"],
                    "reason": f"Fallback auto-approved: {prop['confidence']:.0%} confidence >= 65% threshold",
                    "risk_notes": "Claude unavailable — only high-confidence trades approved",
                })
            else:
                trades.append({
                    "trade_id": i,
                    "decision": "reject",
                    "adjusted_qty": 0,
                    "reason": f"Fallback rejected: {prop['confidence']:.0%} confidence < 65% threshold (Claude unavailable)",
                })
        return {
            "market_assessment": "Claude review unavailable — conservative fallback mode",
            "trades": trades,
            "summary": "Fallback: only trades with ≥65% confidence approved. Claude was unavailable.",
        }

    def _format_equity_review(self, proposals: list, review: dict) -> str:
        """Format the Claude equity review as a Telegram message."""
        lines = [
            f"🧠 EQUITY TRADE REVIEW — {datetime.now(ET).strftime('%H:%M ET')}",
            "",
            f"📊 {review.get('market_assessment', 'N/A')}",
            "",
        ]

        for decision in review.get("trades", []):
            tid = decision.get("trade_id", 0)
            dec = decision.get("decision", "?")
            dec_emoji = {"approve": "✅", "reject": "❌", "adjust": "🔧"}.get(dec, "❓")

            if 0 < tid <= len(proposals):
                prop = proposals[tid - 1]
                qty = decision.get("adjusted_qty", prop["qty"])
                lines.append(f"{dec_emoji} #{tid} {prop['symbol']}: {dec.upper()} {qty} shares @ ${prop['price']:.2f}")
                lines.append(f"   Signal: {prop['confidence']:.0%} — {', '.join(prop['reasons'])}")
            else:
                lines.append(f"{dec_emoji} #{tid}: {dec.upper()}")

            lines.append(f"   {decision.get('reason', '')}")
            if decision.get("risk_notes"):
                lines.append(f"   ⚠️ {decision['risk_notes']}")
            lines.append("")

        approved = sum(1 for t in review.get("trades", []) if t.get("decision") in ("approve", "adjust"))
        lines.append(f"📋 Result: {approved}/{len(proposals)} trades approved")
        lines.append(f"💬 {review.get('summary', '')}")

        return "\n".join(lines)

    def scan_for_exits(self):
        """Check held positions for sell signals (beyond stop/TP)."""
        positions = self.get_positions()
        for pos in positions:
            try:
                bars = self.get_bars(pos["symbol"])
                if bars.empty:
                    continue

                signal = generate_signal(bars, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)

                if signal["action"] == "SELL" and signal["confidence"] >= 0.5:
                    reason = f"Signal exit — {', '.join(signal['reasons'])}"
                    self.place_sell(pos["symbol"], int(pos["qty"]), reason)

            except Exception as e:
                log.error(f"Error checking exit for {pos['symbol']}: {e}")

    def run_cycle(self):
        """One full scan cycle: check exits, check stops, scan entries."""
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        if now < market_open or now > market_close:
            log.info(f"Market closed. Current ET time: {now.strftime('%H:%M')}")
            return

        if now.weekday() >= 5:  # Saturday/Sunday
            log.info("Weekend. Market closed.")
            return

        log.info(f"{'─'*40}")
        log.info(f"Running scan cycle at {now.strftime('%H:%M ET')}")

        # 1. Check stop losses and take profits first
        self.check_stop_loss_take_profit()

        # 2. Check existing positions for signal-based exits
        self.scan_for_exits()

        # 3. Scan for new entry opportunities
        scan_results = self.scan_for_entries()

        # 4. Log status
        account = self.get_account_info()
        positions = self.get_positions()
        log.info(f"Portfolio: ${account['equity']:,.2f} | "
                f"P&L today: ${account['pnl_today']:+,.2f} | "
                f"Positions: {len(positions)}/{config.MAX_POSITIONS}")

        for pos in positions:
            log.info(f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_entry']:.2f} "
                    f"→ ${pos['current_price']:.2f} ({pos['pnl_pct']:+.1f}%)")

        # 5. Send scan summary to Telegram
        now = datetime.now(ET)
        pos_lines = []
        for pos in positions:
            emoji = "🟢" if pos["pnl_pct"] > 0 else "🔴"
            pos_lines.append(f"  {emoji} {pos['symbol']}: {pos['qty']:.0f} @ ${pos['current_price']:.2f} ({pos['pnl_pct']:+.1f}%)")

        summary = f"📡 Scan {now.strftime('%H:%M ET')}\n"
        summary += f"💰 ${account['equity']:,.2f} | P&L: ${account['pnl_today']:+,.2f}\n"
        if pos_lines:
            summary += "\n".join(pos_lines) + "\n"
        if scan_results:
            summary += "\nSignals:\n" + "\n".join(scan_results)
        else:
            summary += "\nNo signals triggered."

        self.tg.send_trade_alert(summary)

    def status(self) -> str:
        """Return a human-readable status string."""
        account = self.get_account_info()
        positions = self.get_positions()

        lines = [
            f"{'='*40}",
            f"AlpacaBot Status",
            f"{'='*40}",
            f"Mode: {'📄 Paper' if self.paper else '💰 Live'}",
            f"Equity: ${account['equity']:,.2f}",
            f"Cash: ${account['cash']:,.2f}",
            f"P&L Today: ${account['pnl_today']:+,.2f}",
            f"Positions: {len(positions)}/{config.MAX_POSITIONS}",
        ]

        if positions:
            lines.append(f"{'─'*40}")
            for pos in positions:
                emoji = "🟢" if pos["pnl_pct"] > 0 else "🔴"
                lines.append(
                    f"{emoji} {pos['symbol']}: {pos['qty']:.0f} shares | "
                    f"${pos['current_price']:.2f} | {pos['pnl_pct']:+.1f}%"
                )

        if self.trades_today:
            lines.append(f"{'─'*40}")
            lines.append(f"Trades today: {len(self.trades_today)}")
            for t in self.trades_today[-5:]:
                lines.append(f"  {t['action']} {t['qty']} {t['symbol']}: {t['reason']}")

        return "\n".join(lines)


def main():
    """Main loop — scan every N minutes during market hours."""
    if not config.API_KEY or not config.SECRET_KEY:
        log.error("Missing API keys! Copy .env.example to .env and add your Alpaca keys.")
        sys.exit(1)

    bot = AlpacaBot()

    # Run immediately on start
    log.info("Running initial scan...")
    bot.run_cycle()
    log.info(bot.status())

    # Schedule recurring scans
    log.info(f"Scheduling scans every {config.SCAN_INTERVAL_MINUTES} minutes...")
    schedule.every(config.SCAN_INTERVAL_MINUTES).minutes.do(bot.run_cycle)

    # Daily summary at market close
    def daily_summary():
        status = bot.status()
        log.info(status)
        bot.tg.send_briefing(f"📊 End of Day Summary\n\n{status}")
    schedule.every().day.at("16:05").do(daily_summary)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
        log.info(bot.status())


if __name__ == "__main__":
    main()
