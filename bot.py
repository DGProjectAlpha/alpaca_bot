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
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
    GetOrdersRequest, QueryOrderStatus,
    TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, OrderClass
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
from premarket_scanner import PremarketScanner

# ─── Trailing Stop High-Water Mark Tracking ───
TRAILING_STOPS_FILE = "/workspace/AlpacaBot/trailing_stops.json"

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
        self.rejected_today = set()  # symbols rejected by Claude — skip for rest of day
        self.monitor_mode = False    # True = only monitor positions, skip entry scanning
        self.last_full_scan_time = None  # track when we last did a full scan
        self.last_spy_price = None       # track SPY for move-triggered rescans
        self.positions_sold_since_scan = 0  # track sells to trigger rescan
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
        log.info(f"Bracket orders: ENABLED (exchange-level SL/TP)")
        log.info(f"Trailing stop: {config.TRAILING_STOP_PCT*100:.0f}% trail (activates after +{config.TRAILING_STOP_ACTIVATE_PCT*100:.1f}%)")
        log.info(f"{'='*50}")

        # Pre-market scanner
        self.premarket = PremarketScanner(
            data_client=self.data_client,
            tg=self.tg,
            bot=self,
        )

        # Protect any existing positions that don't have bracket orders
        self._protect_existing_positions()

    def _protect_existing_positions(self):
        """On startup, place stop + take profit orders for any positions missing bracket protection."""
        positions = self.get_positions()
        if not positions:
            return

        # Check which symbols already have open orders (bracket legs or manual SL/TP)
        bracketed = self._get_symbols_with_bracket_legs()

        # Also check raw open orders — if shares are "held_for_orders", they're already protected
        try:
            open_orders = self.trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            for o in open_orders:
                bracketed.add(o.symbol)
        except Exception:
            pass

        unprotected = [p for p in positions if p["symbol"] not in bracketed]

        if not unprotected:
            log.info("✅ All existing positions have protective orders active.")
            return

        for pos in unprotected:
            entry = pos["avg_entry"]
            stop_price = round(entry * (1 - config.STOP_LOSS_PCT), 2)
            profit_price = round(entry * (1 + config.TAKE_PROFIT_PCT), 2)
            qty = int(pos["qty"])

            try:
                # Two separate orders for existing positions: stop loss + take profit
                stop_order = StopOrderRequest(
                    symbol=pos["symbol"],
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    stop_price=stop_price,
                )
                self.trading_client.submit_order(stop_order)

                tp_order = LimitOrderRequest(
                    symbol=pos["symbol"],
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=profit_price,
                )
                self.trading_client.submit_order(tp_order)

                log.info(f"🛡️ Protected {pos['symbol']}: SL ${stop_price} / TP ${profit_price}")
                self.tg.send_trade_alert(
                    f"🛡️ Bracket added for {pos['symbol']} ({qty} shares)\n"
                    f"Entry: ${entry:.2f} | SL: ${stop_price} | TP: ${profit_price}"
                )
            except Exception as e:
                log.error(f"❌ Failed to protect {pos['symbol']}: {e}")

    # ═══════════════════════════════════════════════════════════
    # Smart Scanning — Monitor Mode + Rescan Triggers
    # ═══════════════════════════════════════════════════════════

    def _get_spy_price(self) -> float:
        """Get current SPY price for market move detection."""
        try:
            bars = self.get_bars("SPY")
            if not bars.empty:
                return bars["close"].iloc[-1]
        except Exception as e:
            log.warning(f"Failed to get SPY price: {e}")
        return 0.0

    def _check_rescan_triggers(self) -> str:
        """Check if any trigger warrants leaving monitor mode for a full rescan.
        Returns trigger reason string, or empty string if no trigger."""
        now = datetime.now(ET)

        # Trigger 1: SPY moved significantly since last scan
        if self.last_spy_price and self.last_spy_price > 0:
            current_spy = self._get_spy_price()
            if current_spy > 0:
                move = abs(current_spy - self.last_spy_price) / self.last_spy_price
                if move >= config.SPY_MOVE_THRESHOLD:
                    return f"SPY moved {move:.1%} since last scan (${self.last_spy_price:.2f} → ${current_spy:.2f})"

        # Trigger 2: Enough time has passed (cooldown expired)
        if self.last_full_scan_time:
            hours_since = (now - self.last_full_scan_time).total_seconds() / 3600
            if hours_since >= config.RESCAN_COOLDOWN_HOURS:
                return f"{hours_since:.1f}h since last scan (cooldown: {config.RESCAN_COOLDOWN_HOURS}h)"

        # Trigger 3: A position was sold (frees capital, worth looking for new entries)
        if self.positions_sold_since_scan > 0:
            count = self.positions_sold_since_scan
            self.positions_sold_since_scan = 0
            return f"{count} position(s) sold — scanning for new entries"

        return ""

    def _enter_monitor_mode(self):
        """Enter monitor mode — stop scanning for entries, only monitor existing positions."""
        if not self.monitor_mode:
            self.monitor_mode = True
            rejected_pct = len(self.rejected_today) / len(config.WATCHLIST) * 100
            msg = (f"💤 Entering MONITOR MODE — {len(self.rejected_today)}/{len(config.WATCHLIST)} "
                   f"symbols rejected ({rejected_pct:.0f}%). Only monitoring positions now.\n"
                   f"Will rescan if: SPY moves >{config.SPY_MOVE_THRESHOLD*100:.0f}%, "
                   f"{config.RESCAN_COOLDOWN_HOURS}h passes, or a position sells.")
            log.info(msg)
            self.tg.send_trade_alert(msg)

    def _exit_monitor_mode(self, reason: str):
        """Exit monitor mode and do a full scan."""
        if self.monitor_mode:
            self.monitor_mode = False
            msg = f"🔄 Exiting MONITOR MODE — {reason}"
            log.info(msg)
            self.tg.send_trade_alert(msg)

    def _should_enter_monitor_mode(self) -> bool:
        """Check if we should enter monitor mode based on rejection ratio."""
        if len(config.WATCHLIST) == 0:
            return False
        rejection_ratio = len(self.rejected_today) / len(config.WATCHLIST)
        return rejection_ratio >= config.MONITOR_MODE_THRESHOLD

    def run_monitor_check(self):
        """Lightweight check during monitor mode — only positions + triggers."""
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        if now < market_open or now > market_close or now.weekday() >= 5:
            return

        if not self.monitor_mode:
            return

        # Check trailing stops and stop loss/take profit (always active)
        self.check_trailing_stops()
        self.check_stop_loss_take_profit()

        # Check if any trigger warrants a full rescan
        trigger = self._check_rescan_triggers()
        if trigger:
            self._exit_monitor_mode(trigger)
            self.run_cycle()

    # ═══════════════════════════════════════════════════════════
    # Trailing Stop Loss System
    # ═══════════════════════════════════════════════════════════

    def _load_trailing_stops(self) -> dict:
        """Load high-water marks from disk."""
        try:
            with open(TRAILING_STOPS_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_trailing_stops(self, data: dict):
        """Persist high-water marks to disk."""
        with open(TRAILING_STOPS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _update_high_water_marks(self, positions: list):
        """Update the high-water mark for each open position."""
        marks = self._load_trailing_stops()
        held_symbols = set()

        for pos in positions:
            sym = pos["symbol"]
            held_symbols.add(sym)
            current = pos["current_price"]
            entry = pos["avg_entry"]

            if sym not in marks:
                # First time tracking — initialize with entry price
                marks[sym] = {
                    "entry_price": entry,
                    "high_water": max(current, entry),
                    "updated": datetime.now(ET).isoformat(),
                }
            else:
                # Update high-water mark if current price is higher
                if current > marks[sym]["high_water"]:
                    marks[sym]["high_water"] = current
                    marks[sym]["updated"] = datetime.now(ET).isoformat()

        # Clean up symbols we no longer hold
        stale = [s for s in marks if s not in held_symbols]
        for s in stale:
            del marks[s]

        self._save_trailing_stops(marks)
        return marks

    def check_trailing_stops(self):
        """Check if any position has dropped enough from its high-water mark to trigger a trailing stop sell."""
        positions = self.get_positions()
        if not positions:
            return

        marks = self._update_high_water_marks(positions)
        bracketed = self._get_symbols_with_bracket_legs()

        for pos in positions:
            sym = pos["symbol"]
            current = pos["current_price"]
            entry = pos["avg_entry"]

            if sym not in marks:
                continue

            hwm = marks[sym]["high_water"]
            gain_from_entry = (hwm - entry) / entry

            # Only activate trailing stop once position has gained enough
            if gain_from_entry < config.TRAILING_STOP_ACTIVATE_PCT:
                continue

            # Check if price has dropped TRAILING_STOP_PCT from the high-water mark
            drop_from_peak = (hwm - current) / hwm

            if drop_from_peak >= config.TRAILING_STOP_PCT:
                pnl_pct = (current - entry) / entry * 100
                log.warning(
                    f"📉 TRAILING STOP triggered for {sym}: "
                    f"Peak ${hwm:.2f} → Now ${current:.2f} "
                    f"(dropped {drop_from_peak*100:.1f}% from peak, P&L: {pnl_pct:+.1f}%)"
                )

                # Cancel any existing bracket/stop/TP orders for this symbol before selling
                if sym in bracketed:
                    self._cancel_orders_for_symbol(sym)
                    time.sleep(1)  # Brief pause for order cancellation to settle

                reason = (
                    f"Trailing stop: dropped {drop_from_peak*100:.1f}% from peak ${hwm:.2f}. "
                    f"P&L: {pnl_pct:+.1f}% (entry ${entry:.2f} → ${current:.2f})"
                )
                self.place_sell(sym, int(pos["qty"]), reason)

                self.tg.send_trade_alert(
                    f"📉 TRAILING STOP — {sym}\n"
                    f"Peak: ${hwm:.2f} → Now: ${current:.2f}\n"
                    f"Drop from peak: {drop_from_peak*100:.1f}%\n"
                    f"P&L: {pnl_pct:+.1f}% (entry ${entry:.2f})\n"
                    f"Selling {int(pos['qty'])} shares to lock in gains"
                )

    def _cancel_orders_for_symbol(self, symbol: str):
        """Cancel all open orders for a specific symbol (bracket legs, stops, etc.)."""
        try:
            open_orders = self.trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            for order in open_orders:
                if order.symbol == symbol:
                    try:
                        self.trading_client.cancel_order_by_id(order.id)
                        log.info(f"Cancelled order {order.id} for {symbol} (trailing stop cleanup)")
                    except Exception as e:
                        log.warning(f"Failed to cancel order {order.id} for {symbol}: {e}")
        except Exception as e:
            log.error(f"Error fetching orders for cancellation: {e}")

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

    def place_buy(self, symbol: str, qty: int, reason: str, entry_price: float = None) -> bool:
        """Place a bracket buy order — exchange-level stop loss + take profit (OCO)."""
        if qty <= 0:
            return False

        try:
            # Calculate stop/profit prices from entry (or latest known price)
            if entry_price and entry_price > 0:
                stop_price = round(entry_price * (1 - config.STOP_LOSS_PCT), 2)
                profit_price = round(entry_price * (1 + config.TAKE_PROFIT_PCT), 2)
            else:
                # Fallback: get current price for bracket calc
                bars = self.get_bars(symbol, days=2)
                if not bars.empty:
                    entry_price = bars["close"].iloc[-1]
                    stop_price = round(entry_price * (1 - config.STOP_LOSS_PCT), 2)
                    profit_price = round(entry_price * (1 + config.TAKE_PROFIT_PCT), 2)
                else:
                    # Can't determine price — place simple order as fallback
                    log.warning(f"Can't get price for {symbol} bracket — placing simple market order")
                    stop_price = None
                    profit_price = None

            if stop_price and profit_price:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.BRACKET,
                    take_profit=TakeProfitRequest(limit_price=profit_price),
                    stop_loss=StopLossRequest(stop_price=stop_price),
                )
                log.info(f"🟢 BRACKET BUY {qty} {symbol} — SL: ${stop_price} / TP: ${profit_price}")
            else:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )

            result = self.trading_client.submit_order(order)
            bracket_info = f" [SL: ${stop_price} | TP: ${profit_price}]" if stop_price else ""
            log.info(f"🟢 BUY {qty} {symbol} — {reason}{bracket_info}")
            log.info(f"   Order ID: {result.id}, Status: {result.status}")
            self.tg.send_trade_alert(
                f"🟢 BUY {qty} {symbol}{bracket_info}\n{reason}\nOrder: {result.id}"
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

    def place_premarket_buy(self, symbol: str, qty: int, limit_price: float, reason: str) -> bool:
        """Place an extended-hours limit buy order (pre-market / after-hours)."""
        if qty <= 0 or limit_price <= 0:
            return False

        try:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                limit_price=round(limit_price, 2),
                time_in_force=TimeInForce.DAY,
                extended_hours=True,
            )
            result = self.trading_client.submit_order(order)
            log.info(f"🌅 PRE-MKT BUY {qty} {symbol} @ ${limit_price:.2f} — {reason}")
            log.info(f"   Order ID: {result.id}, Status: {result.status}")
            self.tg.send_trade_alert(
                f"🌅 PRE-MKT BUY {qty} {symbol} @ ${limit_price:.2f}\n{reason}\nOrder: {result.id}"
            )
            self.trades_today.append({
                "time": datetime.now(ET).isoformat(),
                "action": "PRE-MKT BUY",
                "symbol": symbol,
                "qty": qty,
                "limit_price": limit_price,
                "reason": reason,
                "order_id": str(result.id),
            })
            return True

        except Exception as e:
            log.error(f"❌ Failed pre-market buy {symbol}: {e}")
            return False

    def place_premarket_sell(self, symbol: str, qty: int, limit_price: float, reason: str) -> bool:
        """Place an extended-hours limit sell order (pre-market / after-hours)."""
        if qty <= 0 or limit_price <= 0:
            return False

        try:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                limit_price=round(limit_price, 2),
                time_in_force=TimeInForce.DAY,
                extended_hours=True,
            )
            result = self.trading_client.submit_order(order)
            log.info(f"🌅 PRE-MKT SELL {qty} {symbol} @ ${limit_price:.2f} — {reason}")
            log.info(f"   Order ID: {result.id}, Status: {result.status}")
            self.tg.send_trade_alert(
                f"🌅 PRE-MKT SELL {qty} {symbol} @ ${limit_price:.2f}\n{reason}\nOrder: {result.id}"
            )
            self.trades_today.append({
                "time": datetime.now(ET).isoformat(),
                "action": "PRE-MKT SELL",
                "symbol": symbol,
                "qty": qty,
                "limit_price": limit_price,
                "reason": reason,
                "order_id": str(result.id),
            })
            return True

        except Exception as e:
            log.error(f"❌ Failed pre-market sell {symbol}: {e}")
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
            self.positions_sold_since_scan += 1  # trigger rescan in monitor mode
            return True

        except Exception as e:
            log.error(f"❌ Failed to sell {symbol}: {e}")
            return False

    def _get_symbols_with_bracket_legs(self) -> set:
        """Return symbols that already have active bracket (stop/TP) orders on the exchange."""
        try:
            open_orders = self.trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            bracket_symbols = set()
            for o in open_orders:
                # Bracket legs show up as open stop/limit orders tied to the parent
                if o.order_class and str(o.order_class) in ("bracket", "oco"):
                    bracket_symbols.add(o.symbol)
                # Also catch standalone stop/limit legs with parent_order_id
                if hasattr(o, "legs") and o.legs:
                    bracket_symbols.add(o.symbol)
            return bracket_symbols
        except Exception as e:
            log.error(f"Error checking bracket orders: {e}")
            return set()

    def check_stop_loss_take_profit(self):
        """Safety net: check positions for SL/TP if bracket orders aren't active on exchange."""
        positions = self.get_positions()
        bracketed = self._get_symbols_with_bracket_legs()

        for pos in positions:
            if pos["symbol"] in bracketed:
                # Exchange-level bracket order is handling this position
                continue

            # Fallback software check for positions without bracket orders
            pnl_pct = pos["pnl_pct"] / 100

            if pnl_pct <= -config.STOP_LOSS_PCT:
                log.warning(f"🛑 STOP LOSS (software fallback) on {pos['symbol']} ({pos['pnl_pct']:.1f}%)")
                self.tg.send_trade_alert(
                    f"🛑 STOP LOSS — {pos['symbol']} at {pos['pnl_pct']:.1f}% (no bracket order active)"
                )
                self.place_sell(pos["symbol"], int(pos["qty"]),
                              f"Stop loss at {pos['pnl_pct']:.1f}%")

            elif pnl_pct >= config.TAKE_PROFIT_PCT:
                log.info(f"🎯 TAKE PROFIT (software fallback) on {pos['symbol']} ({pos['pnl_pct']:.1f}%)")
                self.tg.send_trade_alert(
                    f"🎯 TAKE PROFIT — {pos['symbol']} at {pos['pnl_pct']:.1f}% (no bracket order active)"
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
            if symbol in self.rejected_today:
                continue

            try:
                bars = self.get_bars(symbol)
                if bars.empty:
                    continue

                signal = generate_signal(bars, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)

                if signal["action"] == "BUY" and signal["confidence"] >= 0.55:
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
                # Cache rejection — don't re-propose this symbol today
                if 1 <= trade_id <= len(proposals):
                    rejected_sym = proposals[trade_id - 1]["symbol"]
                    self.rejected_today.add(rejected_sym)
                    log.info(f"Cached rejection for {rejected_sym} — skipping for rest of day")
                scan_results.append(f"❌ Trade #{trade_id}: REJECTED — {decision.get('reason', '')}")
                continue

            if trade_id < 1 or trade_id > len(proposals):
                continue

            prop = proposals[trade_id - 1]
            qty = decision.get("adjusted_qty", prop["qty"])
            reason = f"Confidence {prop['confidence']:.0%}: {', '.join(prop['reasons'])} | Claude: {decision.get('reason', 'approved')}"

            self.place_buy(prop["symbol"], qty, reason, entry_price=prop["price"])
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
                    "--model", "haiku",
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

    def run_premarket_scan(self):
        """Pre-market scan cycle — runs during extended hours only."""
        if not self.premarket.is_extended_hours():
            return
        if not self.can_buy():
            log.info("Pre-market: max positions reached or insufficient buying power.")
            self.tg.send_trade_alert("🌅 Pre-market scan skipped — max positions reached or insufficient buying power.")
            return
        now = datetime.now(ET)
        session = "PRE-MKT" if self.premarket.is_premarket_hours() else "AFTER-HRS"
        self.tg.send_trade_alert(f"🌅 {session} scan starting — scanning {len(config.WATCHLIST)} symbols at {now.strftime('%H:%M ET')}...")
        self.premarket.clear_stale_proposals()
        self.premarket.scan()

    def run_cycle(self, force=False):
        """One full scan cycle: check exits, check stops, scan entries.
        If force=True, run even if in monitor mode (used for fixed-time scans)."""
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        # Reset rejection cache + monitor mode at market open each day
        if now.hour == 9 and now.minute < (30 + config.SCAN_INTERVAL_MINUTES):
            if self.rejected_today:
                log.info(f"New trading day — clearing {len(self.rejected_today)} cached rejections")
                self.rejected_today.clear()
            if self.monitor_mode:
                self.monitor_mode = False
                log.info("New trading day — exiting monitor mode")

        if now < market_open or now > market_close:
            log.info(f"Market closed. Current ET time: {now.strftime('%H:%M')}")
            return

        if now.weekday() >= 5:  # Saturday/Sunday
            log.info("Weekend. Market closed.")
            return

        # If in monitor mode and not forced, skip the full scan
        if self.monitor_mode and not force:
            log.info(f"📡 Monitor mode — skipping full scan at {now.strftime('%H:%M ET')}")
            return

        log.info(f"{'─'*40}")
        log.info(f"Running {'FORCED ' if force else ''}scan cycle at {now.strftime('%H:%M ET')}")

        # 1. Check trailing stops (locks in gains from winners)
        self.check_trailing_stops()

        # 2. Check fixed stop losses and take profits
        self.check_stop_loss_take_profit()

        # 3. Check existing positions for signal-based exits
        self.scan_for_exits()

        # 4. Scan for new entry opportunities
        scan_results = self.scan_for_entries()

        # Track scan state for smart scanning
        self.last_full_scan_time = now
        self.last_spy_price = self._get_spy_price()
        self.positions_sold_since_scan = 0

        # 5. Check if we should enter monitor mode
        if self._should_enter_monitor_mode():
            self._enter_monitor_mode()

        # 6. Log status
        account = self.get_account_info()
        positions = self.get_positions()
        mode_tag = " [MONITOR]" if self.monitor_mode else ""
        log.info(f"Portfolio: ${account['equity']:,.2f} | "
                f"P&L today: ${account['pnl_today']:+,.2f} | "
                f"Positions: {len(positions)}/{config.MAX_POSITIONS} | "
                f"Rejected today: {len(self.rejected_today)} symbols cached{mode_tag}")

        for pos in positions:
            log.info(f"  {pos['symbol']}: {pos['qty']} shares @ ${pos['avg_entry']:.2f} "
                    f"→ ${pos['current_price']:.2f} ({pos['pnl_pct']:+.1f}%)")

        # 7. Send scan summary to Telegram ONLY if there are actual signals or trades
        # Suppress the "no signals" spam — only notify when something meaningful happens
        if scan_results or force:
            now = datetime.now(ET)
            pos_lines = []
            for pos in positions:
                emoji = "🟢" if pos["pnl_pct"] > 0 else "🔴"
                pos_lines.append(f"  {emoji} {pos['symbol']}: {pos['qty']:.0f} @ ${pos['current_price']:.2f} ({pos['pnl_pct']:+.1f}%)")

            summary = f"📡 Scan {now.strftime('%H:%M ET')}{mode_tag}\n"
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
    """Main loop — smart scanning with monitor mode."""
    if not config.API_KEY or not config.SECRET_KEY:
        log.error("Missing API keys! Copy .env.example to .env and add your Alpaca keys.")
        sys.exit(1)

    bot = AlpacaBot()

    # ── Catch-up logic: run any fixed scans we missed due to late start ──
    now = datetime.now(ET)
    current_time_str = now.strftime("%H:%M")

    # Check which fixed scans should have fired today but were missed
    fixed_market_scans = [config.FIXED_SCAN_OPEN, config.FIXED_SCAN_PRECLOSE]
    fixed_extended_scans = [config.FIXED_SCAN_PREMARKET, config.FIXED_SCAN_POSTMARKET]

    missed_market = [t for t in fixed_market_scans if t <= current_time_str]
    missed_extended = [t for t in fixed_extended_scans if t <= current_time_str]

    # Run initial scan (always)
    log.info("Running initial scan...")
    bot.run_cycle(force=True)  # force=True so it sends to Telegram on startup
    log.info(bot.status())

    if missed_market:
        log.info(f"Catch-up: missed fixed market scans {missed_market} — initial forced scan covers these")

    # If we're in extended hours, run premarket scan immediately too
    if bot.premarket.is_extended_hours():
        log.info("Extended hours detected — running immediate pre-market scan...")
        bot.tg.send_trade_alert("🌅 AlpacaBot started during extended hours — running pre-market scan now...")
        bot.run_premarket_scan()
    elif missed_extended:
        log.info(f"Catch-up: missed extended-hours scans {missed_extended} — running pre-market scan now...")
        bot.run_premarket_scan()

    # ── Recurring scans (only fire if NOT in monitor mode) ──
    log.info(f"Scheduling scans every {config.SCAN_INTERVAL_MINUTES} minutes (skipped in monitor mode)...")
    schedule.every(config.SCAN_INTERVAL_MINUTES).minutes.do(bot.run_cycle)

    # ── Monitor mode checks (lightweight, every 60s) ──
    log.info(f"Scheduling monitor checks every {config.CONDITION_CHECK_INTERVAL}s...")
    schedule.every(config.CONDITION_CHECK_INTERVAL).seconds.do(bot.run_monitor_check)

    # ── Fixed-time scans (always run regardless of monitor mode) ──
    def forced_market_scan():
        log.info("⏰ Fixed-time market scan triggered")
        bot.run_cycle(force=True)

    schedule.every().day.at(config.FIXED_SCAN_OPEN).do(forced_market_scan)
    schedule.every().day.at(config.FIXED_SCAN_PRECLOSE).do(forced_market_scan)
    log.info(f"Fixed scans: {config.FIXED_SCAN_OPEN}, {config.FIXED_SCAN_PRECLOSE} ET")

    # ── Pre-market + post-market fixed scans ──
    def premarket_fixed_scan():
        log.info("⏰ Fixed pre-market scan (4:01 AM ET)")
        bot.run_premarket_scan()

    def postmarket_fixed_scan():
        log.info("⏰ Fixed post-market scan (7:00 PM ET)")
        bot.run_premarket_scan()

    schedule.every().day.at(config.FIXED_SCAN_PREMARKET).do(premarket_fixed_scan)
    schedule.every().day.at(config.FIXED_SCAN_POSTMARKET).do(postmarket_fixed_scan)
    log.info(f"Extended hours scans: {config.FIXED_SCAN_PREMARKET} (pre), {config.FIXED_SCAN_POSTMARKET} (post)")

    # ── Daily summary at market close ──
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
