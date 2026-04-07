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

import config
from strategies import generate_signal
from telegram_alerts import TelegramAlerts

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
        """Scan watchlist for buy signals."""
        if not self.can_buy():
            log.info("Max positions reached or insufficient buying power. Skipping scan.")
            return

        # Don't buy stocks we already hold
        held = {p["symbol"] for p in self.get_positions()}

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
                        reason = f"Confidence {signal['confidence']:.0%}: {', '.join(signal['reasons'])}"
                        self.place_buy(symbol, qty, reason)

                        if not self.can_buy():
                            log.info("Position limit reached after buy. Stopping scan.")
                            return

                elif signal["action"] != "HOLD":
                    log.debug(f"{symbol}: {signal['action']} (conf: {signal['confidence']:.0%}) — {', '.join(signal['reasons'])}")

            except Exception as e:
                log.error(f"Error scanning {symbol}: {e}")

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
        self.scan_for_entries()

        # 4. Log status
        account = self.get_account_info()
        positions = self.get_positions()
        log.info(f"Portfolio: ${account['equity']:,.2f} | "
                f"P&L today: ${account['pnl_today']:+,.2f} | "
                f"Positions: {len(positions)}/{config.MAX_POSITIONS}")

        for pos in positions:
            log.info(f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_entry']:.2f} "
                    f"→ ${pos['current_price']:.2f} ({pos['pnl_pct']:+.1f}%)")

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
