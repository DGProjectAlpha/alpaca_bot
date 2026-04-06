"""
AlpacaBot Options — Multi-Strategy Event-Driven Options Trading Bot

Strategies: Iron Condors, Credit Spreads, Wheel, Momentum, Calendar,
Butterfly, Earnings Strangles — across ETFs, stocks, and cheap wheel names.

Scanning: Event-driven, not blind timers.
  - Morning deep scan at 09:20 ET
  - Condition-change rescans (SPY >0.5%, VIX >1pt, hourly)
  - Lightweight condition checks every 2 minutes
  - Afternoon briefing at 16:05 ET

Run: python options_bot.py
"""
import sys
import time
import json
import logging
import schedule
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest,
    GetOrdersRequest, QueryOrderStatus,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import (
    StockHistoricalDataClient,
    OptionHistoricalDataClient,
)
from alpaca.data.requests import (
    StockBarsRequest, StockLatestBarRequest,
    OptionChainRequest, OptionBarsRequest,
)
from alpaca.data.timeframe import TimeFrame

import config
from options_strategies import (
    StrategyType, MarketRegime, TrendDirection,
    OptionLeg, OptionsTradeSetup, TickerAnalysis,
    classify_market_regime, detect_trend, compute_rsi,
    is_near_round_number, select_strategy,
    should_close_position, calculate_position_risk, format_setup_summary,
)
from telegram_alerts import TelegramAlerts
from trade_reviewer import (
    save_proposals, review_trades, save_approvals, load_approvals,
    format_proposals_for_telegram, format_review_for_telegram,
)

# ─── Logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/workspace/AlpacaBot/alpacabot_options.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("AlpacaBotOptions")

ET = ZoneInfo("America/New_York")
POSITIONS_FILE = Path("/workspace/AlpacaBot/positions.json")
BRIEFINGS_DIR = Path("/workspace/AlpacaBot/briefings")


class AlpacaBotOptions:
    def __init__(self, dry_run: bool = True):
        """
        Initialize the options trading bot.
        dry_run=True means analyze and log but don't execute trades.
        """
        self.dry_run = dry_run
        paper = config.TRADING_MODE.lower() == "paper"

        self.trading_client = TradingClient(
            config.API_KEY, config.SECRET_KEY, paper=paper
        )
        self.stock_data = StockHistoricalDataClient(
            config.API_KEY, config.SECRET_KEY
        )
        self.option_data = OptionHistoricalDataClient(
            config.API_KEY, config.SECRET_KEY
        )
        self.paper = paper
        self.trades_today = []
        self.pending_setups = []
        self.active_positions = self._load_positions()
        self.rejected_signatures = set()

        # ── Scan state for event-driven triggers ──
        self._last_spy_price = 0.0
        self._last_vix = 0.0
        self._last_scan_time = None
        self._last_scan_hour = -1
        self._last_condition_check = None

        # Telegram alerts
        self.telegram = TelegramAlerts(
            bot_token=config.TELEGRAM_BOT_TOKEN,
            group_chat_id=config.TELEGRAM_GROUP_CHAT_ID,
            alerts_topic_id=config.TELEGRAM_ALERTS_TOPIC_ID,
        )

        # Verify connection
        account = self.trading_client.get_account()
        mode_str = "📄 PAPER" if paper else "💰 LIVE"
        dry_str = " [DRY RUN]" if dry_run else ""
        log.info(f"{'=' * 55}")
        log.info(f"AlpacaBot Options started — {mode_str}{dry_str}")
        log.info(f"Equity: ${float(account.equity):,.2f}")
        log.info(f"Buying Power: ${float(account.buying_power):,.2f}")
        log.info(f"Options Capital: ${config.OPTIONS_MAX_CAPITAL:,.2f}")
        log.info(f"Universe: {len(config.ETF_UNIVERSE)} ETFs, {len(config.STOCK_UNIVERSE)} stocks, {len(config.WHEEL_STOCKS)} wheel names")
        log.info(f"Scanning: event-driven (check every {config.CONDITION_CHECK_INTERVAL}s)")
        log.info(f"{'=' * 55}")

    # ═══════════════════════════════════════════════════════════
    # Market Data — Prices and Indicators
    # ═══════════════════════════════════════════════════════════

    def get_account_info(self) -> dict:
        account = self.trading_client.get_account()
        return {
            "equity": float(account.equity),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "pnl_today": float(account.equity) - float(account.last_equity),
        }

    def get_stock_price(self, symbol: str) -> float:
        """Get current price for any stock/ETF."""
        try:
            request = StockLatestBarRequest(symbol_or_symbols=symbol)
            bars = self.stock_data.get_stock_latest_bar(request)
            if symbol in bars:
                return float(bars[symbol].close)
        except Exception as e:
            log.error(f"Failed to get price for {symbol}: {e}")
        return 0.0

    def get_spy_price(self) -> float:
        """Get current SPY price."""
        return self.get_stock_price("SPY")

    def get_vix(self) -> float:
        """
        Estimate VIX from VIXY ETF price.
        VIXY tracks VIX short-term futures. Piecewise linear approximation.
        """
        try:
            request = StockLatestBarRequest(symbol_or_symbols="VIXY")
            bars = self.stock_data.get_stock_latest_bar(request)
            if "VIXY" in bars:
                vixy = float(bars["VIXY"].close)
                if vixy < 20:
                    vix_est = 10 + (vixy - 15) * 0.6
                elif vixy < 35:
                    vix_est = 15 + (vixy - 20) * 0.67
                elif vixy < 55:
                    vix_est = 25 + (vixy - 35) * 0.5
                else:
                    vix_est = 35 + (vixy - 55) * 0.4
                vix_est = max(10.0, min(80.0, vix_est))
                log.info(f"VIXY=${vixy:.2f} → VIX estimate={vix_est:.1f}")
                return vix_est
        except Exception:
            pass
        log.warning("Could not fetch VIX proxy. Using default VIX=18")
        return 18.0

    def get_ticker_analysis(self, symbol: str) -> TickerAnalysis:
        """
        Get full technical analysis for a single ticker:
        price, EMA-20, EMA-50, RSI, trend, round-number proximity.
        """
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Hour,
                start=datetime.now(ET) - timedelta(days=30),
            )
            bars = self.stock_data.get_stock_bars(request)
            if symbol not in bars.data or len(bars[symbol]) < 20:
                # Fallback: just get current price
                price = self.get_stock_price(symbol)
                return TickerAnalysis(
                    symbol=symbol, price=price,
                    near_round_number=is_near_round_number(price),
                )

            closes = [float(b.close) for b in bars[symbol]]
            df = pd.Series(closes)
            ema_20 = float(df.ewm(span=20, adjust=False).mean().iloc[-1])
            ema_50 = float(df.ewm(span=50, adjust=False).mean().iloc[-1]) if len(closes) >= 50 else ema_20
            price = closes[-1]
            rsi = compute_rsi(closes)
            trend = detect_trend(closes[-30:], ema_20, ema_50)

            return TickerAnalysis(
                symbol=symbol,
                price=price,
                ema_20=ema_20,
                ema_50=ema_50,
                rsi=rsi,
                recent_prices=closes[-30:],
                trend=trend,
                near_round_number=is_near_round_number(price),
            )
        except Exception as e:
            log.error(f"Failed to analyze {symbol}: {e}")
            price = self.get_stock_price(symbol)
            return TickerAnalysis(
                symbol=symbol, price=price,
                near_round_number=is_near_round_number(price) if price > 0 else False,
            )

    def get_all_ticker_data(self) -> dict:
        """
        Analyze all tickers in the universe. Returns dict[symbol -> TickerAnalysis].
        Fetches prices in bulk where possible, then individual analysis.
        """
        ticker_data = {}
        all_tickers = config.ALL_TICKERS

        # Batch fetch latest prices first
        try:
            request = StockLatestBarRequest(symbol_or_symbols=all_tickers)
            bars = self.stock_data.get_stock_latest_bar(request)
            for sym in all_tickers:
                if sym in bars:
                    price = float(bars[sym].close)
                    ticker_data[sym] = TickerAnalysis(
                        symbol=sym, price=price,
                        near_round_number=is_near_round_number(price),
                    )
        except Exception as e:
            log.error(f"Batch price fetch failed: {e}")

        # Full analysis for ETFs and high-vol stocks (the ones we trade most)
        priority_tickers = config.ETF_UNIVERSE + config.STOCK_UNIVERSE
        for sym in priority_tickers:
            try:
                analysis = self.get_ticker_analysis(sym)
                ticker_data[sym] = analysis
            except Exception as e:
                log.error(f"Analysis failed for {sym}: {e}")

        # For wheel stocks, just need price (already batch-fetched)
        # But get proper analysis if time permits
        for sym in config.WHEEL_STOCKS:
            if sym not in ticker_data or ticker_data[sym].ema_20 == 0:
                try:
                    analysis = self.get_ticker_analysis(sym)
                    ticker_data[sym] = analysis
                except Exception:
                    pass  # batch price is sufficient for wheel

        log.info(f"Analyzed {len(ticker_data)} tickers ({sum(1 for t in ticker_data.values() if t.ema_20 > 0)} with full technicals)")
        return ticker_data

    def get_option_expirations(self, symbol: str) -> list:
        """Get available option expiration dates for a symbol."""
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            request = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                expiration_date_gte=datetime.now().strftime("%Y-%m-%d"),
                expiration_date_lte=(datetime.now() + timedelta(days=45)).strftime("%Y-%m-%d"),
                limit=1000,
            )
            contracts = self.trading_client.get_option_contracts(request)

            expirations = set()
            if contracts and hasattr(contracts, 'option_contracts'):
                for c in contracts.option_contracts:
                    exp_str = c.expiration_date.strftime("%Y-%m-%d") if hasattr(c.expiration_date, 'strftime') else str(c.expiration_date)
                    expirations.add(exp_str)

            result = sorted(list(expirations))
            if result:
                return result
        except Exception as e:
            log.error(f"Failed to get expirations for {symbol}: {e}")

        # Fallback: generate expected daily expirations
        dates = []
        for i in range(0, 45):
            d = datetime.now() + timedelta(days=i)
            if d.weekday() < 5:
                dates.append(d.strftime("%Y-%m-%d"))
        return dates[:20]

    def get_all_expirations(self) -> dict:
        """
        Get option expirations for all tickers in the universe.
        Returns dict[symbol -> list[str]].
        """
        expirations_map = {}
        # ETFs have the most liquid options — fetch individually
        for sym in config.ETF_UNIVERSE:
            expirations_map[sym] = self.get_option_expirations(sym)

        # For stocks, use SPY expirations as proxy (most share the same dates)
        spy_exps = expirations_map.get("SPY", [])
        for sym in config.STOCK_UNIVERSE + config.WHEEL_STOCKS:
            try:
                exps = self.get_option_expirations(sym)
                expirations_map[sym] = exps if exps else spy_exps
            except Exception:
                expirations_map[sym] = spy_exps

        return expirations_map

    def get_option_chain(self, symbol: str, expiration: str) -> dict:
        """Get option chain for a symbol and expiration."""
        try:
            from alpaca.trading.requests import GetOptionContractsRequest
            request = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                expiration_date=expiration,
                limit=1000,
            )
            contracts = self.trading_client.get_option_contracts(request)

            calls = []
            puts = []
            if contracts and hasattr(contracts, 'option_contracts'):
                for c in contracts.option_contracts:
                    ctype = c.type.value if hasattr(c.type, 'value') else str(c.type)
                    entry = {
                        "symbol": c.symbol,
                        "strike": float(c.strike_price),
                        "expiration": str(c.expiration_date),
                        "type": ctype,
                    }
                    if "call" in ctype.lower():
                        calls.append(entry)
                    elif "put" in ctype.lower():
                        puts.append(entry)

            calls.sort(key=lambda x: x["strike"])
            puts.sort(key=lambda x: x["strike"])
            return {"calls": calls, "puts": puts}

        except Exception as e:
            log.error(f"Failed to get option chain for {symbol} {expiration}: {e}")
            return {"calls": [], "puts": []}

    def get_option_quote(self, option_symbol: str) -> dict:
        """Get latest quote for an option contract."""
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest
            request = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol)
            quotes = self.option_data.get_option_latest_quote(request)
            if option_symbol in quotes:
                q = quotes[option_symbol]
                return {
                    "bid": float(q.bid_price),
                    "ask": float(q.ask_price),
                    "mid": (float(q.bid_price) + float(q.ask_price)) / 2,
                }
        except Exception as e:
            log.error(f"Failed to get quote for {option_symbol}: {e}")
        return {"bid": 0, "ask": 0, "mid": 0}

    def get_earnings_upcoming(self) -> list:
        """
        Return list of tickers with earnings in the next 14 days.
        Placeholder — requires an earnings calendar API or manual list.
        For now returns empty; can be populated from external source.
        """
        # TODO: integrate earnings calendar API (e.g., Alpha Vantage, FMP)
        # For now, this can be manually set or read from a file
        earnings_file = Path("/workspace/AlpacaBot/earnings_calendar.json")
        if earnings_file.exists():
            try:
                data = json.loads(earnings_file.read_text())
                today = datetime.now().strftime("%Y-%m-%d")
                cutoff = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
                return [
                    e["symbol"] for e in data
                    if today <= e.get("date", "") <= cutoff
                    and e["symbol"] in (config.STOCK_UNIVERSE + config.ETF_UNIVERSE)
                ]
            except Exception:
                pass
        return []

    # ═══════════════════════════════════════════════════════════
    # Option Symbol Resolution
    # ═══════════════════════════════════════════════════════════

    def resolve_option_symbols(self, setup: OptionsTradeSetup) -> OptionsTradeSetup:
        """
        Resolve OCC option symbols for each leg of a trade setup.
        Finds the closest matching contract in the chain.
        """
        for leg in setup.legs:
            chain = self.get_option_chain(setup.underlying, leg.expiration)
            contracts = chain["calls"] if leg.option_type == "call" else chain["puts"]

            best = None
            best_diff = float("inf")
            for c in contracts:
                diff = abs(c["strike"] - leg.strike)
                if diff < best_diff:
                    best_diff = diff
                    best = c

            if best:
                leg.symbol = best["symbol"]
                leg.strike = best["strike"]
                quote = self.get_option_quote(best["symbol"])
                leg.premium = quote["mid"]

        return setup

    # ═══════════════════════════════════════════════════════════
    # Trade Execution
    # ═══════════════════════════════════════════════════════════

    def execute_setup(self, setup: OptionsTradeSetup) -> bool:
        """
        Execute an options trade setup. Places orders for each leg.
        """
        if self.dry_run:
            log.info(f"🏜️ DRY RUN — would execute:")
            log.info(format_setup_summary(setup))
            self.telegram.send_trade_alert(
                f"🏜️ DRY RUN SIGNAL\n\n{format_setup_summary(setup)}"
            )
            return True

        account = self.get_account_info()
        risk = calculate_position_risk(setup, account["equity"])
        if not risk["approved"]:
            log.warning(f"⚠️ Trade rejected: {risk['reason']}")
            self.telegram.send_trade_alert(f"⚠️ REJECTED: {risk['reason']}")
            return False

        log.info(f"Executing: {setup.strategy.value} on {setup.underlying}")
        all_success = True

        for leg in setup.legs:
            if not leg.symbol:
                log.error(f"Missing symbol for leg: {leg}")
                all_success = False
                continue

            try:
                side = OrderSide.BUY if leg.side == "buy" else OrderSide.SELL
                order = LimitOrderRequest(
                    symbol=leg.symbol,
                    qty=setup.contracts * leg.quantity,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(leg.premium, 2),
                )
                result = self.trading_client.submit_order(order)

                emoji = "🟢" if leg.side == "buy" else "🔴"
                log.info(
                    f"{emoji} {leg.side.upper()} {setup.contracts * leg.quantity}x "
                    f"{leg.option_type.upper()} ${leg.strike:.0f} "
                    f"@ ${leg.premium:.2f} — {result.status}"
                )

                self.trades_today.append({
                    "time": datetime.now(ET).isoformat(),
                    "strategy": setup.strategy.value,
                    "action": leg.side.upper(),
                    "symbol": leg.symbol,
                    "strike": leg.strike,
                    "type": leg.option_type,
                    "qty": setup.contracts * leg.quantity,
                    "premium": leg.premium,
                    "order_id": str(result.id),
                    "status": str(result.status),
                })

            except Exception as e:
                log.error(f"❌ Failed to execute leg: {leg.symbol} — {e}")
                all_success = False

        if all_success:
            self._save_position(setup)
            alert_msg = (
                f"✅ TRADE EXECUTED\n\n"
                f"{format_setup_summary(setup)}\n\n"
                f"Contracts: {setup.contracts}"
            )
            self.telegram.send_trade_alert(alert_msg)
        else:
            self.telegram.send_trade_alert(
                f"⚠️ PARTIAL FILL on {setup.strategy.value} — check logs"
            )

        return all_success

    # ═══════════════════════════════════════════════════════════
    # Position Management
    # ═══════════════════════════════════════════════════════════

    def _load_positions(self) -> list:
        """Load tracked positions from file."""
        if POSITIONS_FILE.exists():
            try:
                return json.loads(POSITIONS_FILE.read_text())
            except Exception:
                return []
        return []

    def _save_position(self, setup: OptionsTradeSetup):
        """Save a new position to tracking file."""
        position = {
            "strategy": setup.strategy.value,
            "underlying": setup.underlying,
            "legs": [
                {
                    "symbol": l.symbol,
                    "side": l.side,
                    "type": l.option_type,
                    "strike": l.strike,
                    "expiration": l.expiration,
                    "premium": l.premium,
                    "quantity": l.quantity,
                }
                for l in setup.legs
            ],
            "contracts": setup.contracts,
            "max_profit": setup.max_profit,
            "max_loss": setup.max_loss,
            "entry_time": datetime.now(ET).isoformat(),
            "entry_vix": self._last_vix,
            "risk_budget": setup.risk_budget,
            "status": "open",
        }
        self.active_positions.append(position)
        POSITIONS_FILE.write_text(json.dumps(self.active_positions, indent=2))

    def _update_positions_file(self):
        """Write current positions state to file."""
        POSITIONS_FILE.write_text(json.dumps(self.active_positions, indent=2))

    def check_positions(self):
        """Check all open positions for exit conditions."""
        vix = self._last_vix if self._last_vix > 0 else self.get_vix()
        closed_count = 0

        for pos in self.active_positions:
            if pos.get("status") != "open":
                continue

            total_pnl = 0
            for leg in pos["legs"]:
                quote = self.get_option_quote(leg["symbol"])
                current_mid = quote["mid"]
                entry_premium = leg["premium"]
                qty = leg.get("quantity", 1)

                if leg["side"] == "sell":
                    leg_pnl = (entry_premium - current_mid) * 100 * pos["contracts"] * qty
                else:
                    leg_pnl = (current_mid - entry_premium) * 100 * pos["contracts"] * qty
                total_pnl += leg_pnl

            max_profit = pos["max_profit"] * pos["contracts"]
            pnl_pct = total_pnl / max_profit if max_profit > 0 else 0

            exp_dates = [l["expiration"] for l in pos["legs"]]
            min_exp = min(exp_dates) if exp_dates else datetime.now().strftime("%Y-%m-%d")
            dte = (datetime.strptime(min_exp, "%Y-%m-%d").date() - datetime.now().date()).days

            should_close, reason = should_close_position(pos, pnl_pct, dte, vix)

            log.info(
                f"  Position: {pos['strategy']} {pos['underlying']} | "
                f"P&L: ${total_pnl:+.0f} ({pnl_pct:+.0%} of max) | "
                f"DTE: {dte} | {'⚠️ CLOSE: ' + reason if should_close else '✅ HOLD'}"
            )

            if should_close:
                self._close_position(pos, reason, total_pnl)
                closed_count += 1

        return closed_count

    def _close_position(self, position: dict, reason: str, pnl: float):
        """Close an open position by placing opposing orders."""
        if self.dry_run:
            log.info(f"🏜️ DRY RUN — would close: {position['strategy']} {position['underlying']} — {reason}")
            position["status"] = "closed_dry"
            self._update_positions_file()
            self.telegram.send_trade_alert(
                f"🏜️ DRY RUN CLOSE\n"
                f"{position['strategy']} {position['underlying']}\n"
                f"P&L: ${pnl:+.0f}\n"
                f"Reason: {reason}"
            )
            return

        for leg in position["legs"]:
            try:
                close_side = OrderSide.BUY if leg["side"] == "sell" else OrderSide.SELL
                quote = self.get_option_quote(leg["symbol"])
                price = quote["mid"]
                qty = leg.get("quantity", 1)

                order = LimitOrderRequest(
                    symbol=leg["symbol"],
                    qty=position["contracts"] * qty,
                    side=close_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(price, 2),
                )
                self.trading_client.submit_order(order)
                log.info(f"Closed leg: {close_side.value} {leg['symbol']} @ ${price:.2f}")

            except Exception as e:
                log.error(f"Failed to close leg {leg['symbol']}: {e}")

        position["status"] = "closed"
        position["close_time"] = datetime.now(ET).isoformat()
        position["close_reason"] = reason
        position["realized_pnl"] = pnl
        self._update_positions_file()

        emoji = "💰" if pnl > 0 else "📉"
        self.telegram.send_trade_alert(
            f"{emoji} POSITION CLOSED\n"
            f"{position['strategy']} {position['underlying']}\n"
            f"P&L: ${pnl:+.0f}\n"
            f"Reason: {reason}"
        )

    # ═══════════════════════════════════════════════════════════
    # Event-Driven Scanning
    # ═══════════════════════════════════════════════════════════

    def check_conditions(self):
        """
        Lightweight condition check — runs every 2 minutes.
        Only fetches SPY price and VIX. If a trigger fires, runs full scan.
        Otherwise just checks open positions for exits.
        """
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        if now < market_open or now > market_close:
            return
        if now.weekday() >= 5:
            return

        spy_price = self.get_spy_price()
        vix = self.get_vix()

        if spy_price == 0:
            return

        trigger_reason = None

        # Trigger 1: SPY moved >0.5% since last scan
        if self._last_spy_price > 0:
            spy_change = abs(spy_price - self._last_spy_price) / self._last_spy_price
            if spy_change >= config.SPY_MOVE_THRESHOLD:
                trigger_reason = f"SPY moved {spy_change:.2%} (${self._last_spy_price:.2f} → ${spy_price:.2f})"

        # Trigger 2: VIX changed >1.0 point since last scan
        if self._last_vix > 0 and trigger_reason is None:
            vix_change = abs(vix - self._last_vix)
            if vix_change >= config.VIX_CHANGE_THRESHOLD:
                trigger_reason = f"VIX changed {vix_change:+.1f} ({self._last_vix:.1f} → {vix:.1f})"

        # Trigger 3: New hour started (hourly light scan)
        current_hour = now.hour
        if current_hour != self._last_scan_hour and trigger_reason is None:
            if self._last_scan_hour >= 0:  # not first check
                trigger_reason = f"Hourly scan ({now.strftime('%H:00')} ET)"

        # Always update VIX for position checks
        self._last_vix = vix

        if trigger_reason:
            log.info(f"🔔 Scan trigger: {trigger_reason}")
            self.run_full_scan(spy_price, vix, trigger_reason)
        else:
            # No trigger — just monitor existing positions
            open_count = sum(1 for p in self.active_positions if p.get("status") == "open")
            if open_count > 0:
                log.info(f"Condition check: SPY=${spy_price:.2f} VIX={vix:.1f} — no trigger, checking {open_count} positions")
                self.check_positions()
            else:
                log.info(f"Condition check: SPY=${spy_price:.2f} VIX={vix:.1f} — no trigger, no positions")

    def run_full_scan(self, spy_price: float = None, vix: float = None, trigger: str = "manual"):
        """
        Full analysis cycle: scan all tickers, generate proposals,
        send to Claude for review, execute approved trades.
        """
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        if now < market_open or now > market_close:
            log.info(f"Market closed. ET time: {now.strftime('%H:%M')}")
            return
        if now.weekday() >= 5:
            log.info("Weekend. Market closed.")
            return

        log.info(f"{'─' * 55}")
        log.info(f"Full scan at {now.strftime('%H:%M ET')} — trigger: {trigger}")

        # 1. Check existing positions first
        if self.active_positions:
            log.info("Checking open positions...")
            self.check_positions()

        # 2. Capacity check
        open_count = sum(1 for p in self.active_positions if p.get("status") == "open")
        if open_count >= config.OPTIONS_MAX_POSITIONS:
            log.info(f"Max positions ({config.OPTIONS_MAX_POSITIONS}) reached. Monitoring only.")
            return

        # 3. Gather market data
        if spy_price is None:
            spy_price = self.get_spy_price()
        if vix is None:
            vix = self.get_vix()
        self._last_vix = vix

        if spy_price == 0:
            log.error("Could not get SPY price. Skipping scan.")
            return

        regime = classify_market_regime(vix)
        log.info(f"SPY: ${spy_price:.2f} | VIX: {vix:.1f} ({regime.value}) | Trigger: {trigger}")

        # 4. Analyze all tickers
        log.info("Analyzing ticker universe...")
        ticker_data = self.get_all_ticker_data()

        # 5. Get expirations for all tickers
        log.info("Fetching option expirations...")
        expirations_map = self.get_all_expirations()

        # 6. Check for upcoming earnings
        earnings_upcoming = self.get_earnings_upcoming()
        if earnings_upcoming:
            log.info(f"Earnings upcoming: {', '.join(earnings_upcoming)}")

        # 7. Build market data dict for strategy selector
        market_data = {
            "vix": vix,
            "ticker_data": ticker_data,
            "available_expirations": expirations_map,
            "earnings_upcoming": earnings_upcoming,
        }

        # 8. Get account equity for position sizing
        account = self.get_account_info()
        equity = account["equity"]

        # 9. Run strategy selector across all tickers and strategies
        setups = select_strategy(market_data, equity, self.active_positions)

        if not setups:
            log.info("No viable setups found this scan.")
            self._update_scan_state(spy_price, now)
            return

        # 10. Filter previously rejected signatures
        filtered = []
        for setup in setups:
            sig = self._setup_signature(setup)
            if sig not in self.rejected_signatures:
                filtered.append(setup)
            else:
                log.info(f"Skipping previously rejected: {sig}")
        setups = filtered

        if not setups:
            log.info("All setups were previously rejected. Waiting for new conditions.")
            self._update_scan_state(spy_price, now)
            return

        log.info(f"Found {len(setups)} viable setups:")
        for i, s in enumerate(setups, 1):
            log.info(f"  #{i}: {s.strategy.value} on {s.underlying} — score={s.score:.2f}")

        # 11. Resolve option symbols and get real quotes
        resolved_setups = []
        for setup in setups[:3]:
            setup = self.resolve_option_symbols(setup)
            if setup.legs:
                self._recalculate_with_real_quotes(setup)
            resolved_setups.append(setup)

        # 12. Build proposals for Claude review
        proposals = self._build_proposals(resolved_setups)

        # Include extended market context for Claude
        spy_td = ticker_data.get("SPY")
        market_context = {
            "spy_price": spy_price,
            "vix": vix,
            "regime": regime.value,
            "trend": spy_td.trend.value if spy_td else "unknown",
            "ema_20": spy_td.ema_20 if spy_td else 0,
            "ema_50": spy_td.ema_50 if spy_td else 0,
            "rsi": spy_td.rsi if spy_td else 0,
            "trigger": trigger,
            "open_positions": open_count,
            "tickers_analyzed": len(ticker_data),
        }

        # 13. Post proposals to Telegram
        proposals_msg = format_proposals_for_telegram(proposals, market_context)
        self.telegram.send_trade_alert(proposals_msg)

        # 14. Claude review pipeline
        pending = save_proposals(proposals, market_context)
        review = review_trades(pending, account)
        approvals = save_approvals(review, proposals)
        review_msg = format_review_for_telegram(review, approvals)
        self.telegram.send_trade_alert(review_msg)

        # 15. Track rejections
        for trade_decision in review.get("trades", []):
            if trade_decision.get("decision") == "reject":
                tid = trade_decision.get("trade_id", 0)
                if 0 < tid <= len(resolved_setups):
                    setup = resolved_setups[tid - 1]
                    sig = self._setup_signature(setup)
                    self.rejected_signatures.add(sig)
                    log.info(f"Marked as rejected: {sig}")

        # 16. Execute approved trades
        approved_trades = approvals.get("approved_trades", [])
        if not approved_trades:
            log.info("No trades approved by Claude. Standing by.")
            self._update_scan_state(spy_price, now)
            return

        for approved in approved_trades:
            for setup in resolved_setups:
                if (setup.strategy.value == approved["strategy"]
                        and setup.underlying == approved["underlying"]):
                    setup.contracts = approved.get("contracts", setup.contracts)
                    log.info(f"Executing approved trade: {setup.strategy.value} on {setup.underlying} ({setup.contracts} contracts)")
                    self.execute_setup(setup)
                    break

        self._update_scan_state(spy_price, now)
        self._log_status()

    def _update_scan_state(self, spy_price: float, now: datetime):
        """Update scan tracking state after a full scan."""
        self._last_spy_price = spy_price
        self._last_scan_time = now
        self._last_scan_hour = now.hour

    def _setup_signature(self, setup: OptionsTradeSetup) -> str:
        """Generate a unique signature for a trade setup to track rejections."""
        sig = f"{setup.strategy.value}_{setup.underlying}_{setup.target_dte}"
        for leg in setup.legs:
            sig += f"_{leg.strike}"
        return sig

    def _recalculate_with_real_quotes(self, setup: OptionsTradeSetup):
        """Recalculate P&L metrics using real option quotes."""
        sell_premium = sum(l.premium * l.quantity for l in setup.legs if l.side == "sell")
        buy_premium = sum(l.premium * l.quantity for l in setup.legs if l.side == "buy")
        real_premium = sell_premium - buy_premium

        if real_premium > 0 and setup.strategy.value in (
            "iron_condor", "bull_put_spread", "bear_call_spread", "earnings_strangle"
        ):
            setup.max_profit = real_premium * 100

            if setup.strategy == StrategyType.IRON_CONDOR and len(setup.legs) >= 4:
                put_legs = sorted([l for l in setup.legs if l.option_type == "put"], key=lambda l: l.strike)
                call_legs = sorted([l for l in setup.legs if l.option_type == "call"], key=lambda l: l.strike)
                put_width = abs(put_legs[-1].strike - put_legs[0].strike) if len(put_legs) == 2 else 5
                call_width = abs(call_legs[-1].strike - call_legs[0].strike) if len(call_legs) == 2 else 5
                spread_width = max(put_width, call_width)
                setup.max_loss = (spread_width * 100) - setup.max_profit
            elif setup.strategy in (StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD):
                spread_width = abs(setup.legs[0].strike - setup.legs[1].strike)
                setup.max_loss = (spread_width * 100) - setup.max_profit

            setup.risk_reward_ratio = setup.max_profit / setup.max_loss if setup.max_loss > 0 else 0

        elif real_premium < 0 and setup.strategy.value in (
            "long_call", "long_put", "calendar_spread", "butterfly"
        ):
            # Debit trades: cost is what we pay
            setup.max_loss = abs(real_premium) * 100
            setup.risk_reward_ratio = setup.max_profit / setup.max_loss if setup.max_loss > 0 else 0

        # Re-cap position size after real quotes — enforce hard 5% equity limit
        if setup.max_loss > 0:
            account = self.get_account_info()
            max_risk = account["equity"] * setup.risk_budget
            max_contracts = max(1, int(max_risk / setup.max_loss))
            if setup.contracts > max_contracts:
                log.info(f"Resized {setup.underlying} {setup.strategy.value}: {setup.contracts} → {max_contracts} contracts (risk cap)")
                setup.contracts = max_contracts

    def _build_proposals(self, setups: list) -> list:
        """Convert resolved setups to proposal dicts for the review pipeline."""
        proposals = []
        for setup in setups:
            proposals.append({
                "strategy": setup.strategy.value,
                "underlying": setup.underlying,
                "contracts": setup.contracts,
                "max_profit": setup.max_profit,
                "max_loss": setup.max_loss,
                "probability_of_profit": setup.probability_of_profit,
                "risk_reward_ratio": setup.risk_reward_ratio,
                "risk_budget": setup.risk_budget,
                "target_dte": setup.target_dte,
                "score": setup.score,
                "reason": setup.reason,
                "legs": [
                    {
                        "symbol": l.symbol,
                        "side": l.side,
                        "option_type": l.option_type,
                        "strike": l.strike,
                        "expiration": l.expiration,
                        "premium": l.premium,
                        "quantity": l.quantity,
                    }
                    for l in setup.legs
                ],
            })
        return proposals

    def _log_status(self):
        """Log current portfolio status."""
        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]
        log.info(
            f"Portfolio: ${account['equity']:,.2f} | "
            f"P&L today: ${account['pnl_today']:+,.2f} | "
            f"Open positions: {len(open_positions)}/{config.OPTIONS_MAX_POSITIONS}"
        )

    # ═══════════════════════════════════════════════════════════
    # Briefings
    # ═══════════════════════════════════════════════════════════

    def morning_briefing(self):
        """
        Morning briefing at 09:20 ET: full universe analysis, generate plan,
        send proposals to Claude for pre-market review.
        """
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return

        # New day reset
        self.rejected_signatures.clear()
        self.trades_today = []
        self._last_scan_hour = -1
        log.info("Morning reset: cleared rejection memory and trade log")

        spy_price = self.get_spy_price()
        vix = self.get_vix()
        self._last_vix = vix
        self._last_spy_price = spy_price
        regime = classify_market_regime(vix)

        # Quick SPY trend for briefing
        spy_analysis = self.get_ticker_analysis("SPY")
        trend = spy_analysis.trend

        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]

        plan_text = self._generate_plan(spy_price, vix, regime, trend)

        briefing = {
            "timestamp": now.isoformat(),
            "type": "morning",
            "market": {
                "spy_price": spy_price,
                "vix": vix,
                "regime": regime.value,
                "trend": trend.value,
                "ema_20": spy_analysis.ema_20,
                "ema_50": spy_analysis.ema_50,
                "rsi": spy_analysis.rsi,
            },
            "account": account,
            "open_positions": len(open_positions),
            "positions": open_positions,
            "plan": plan_text,
        }

        BRIEFINGS_DIR.mkdir(exist_ok=True)
        filename = f"morning_{now.strftime('%Y-%m-%d')}.json"
        filepath = BRIEFINGS_DIR / filename
        filepath.write_text(json.dumps(briefing, indent=2, default=str))
        log.info(f"Morning briefing saved: {filepath}")

        msg = (
            f"🌅 MORNING BRIEFING — {now.strftime('%A %b %d')}\n\n"
            f"SPY: ${spy_price:.2f} | VIX: {vix:.1f} ({regime.value.replace('_', ' ')})\n"
            f"Trend: {trend.value} | RSI: {spy_analysis.rsi:.0f}\n"
            f"Account: ${account['equity']:,.2f}\n"
            f"Open positions: {len(open_positions)}/{config.OPTIONS_MAX_POSITIONS}\n\n"
            f"📋 TODAY'S PLAN:\n{plan_text}\n\n"
            f"🔍 Running full universe scan..."
        )
        self.telegram.send_briefing(msg)

        # Run the full scan as part of morning briefing
        self.run_full_scan(spy_price, vix, trigger="morning_briefing")

    def afternoon_briefing(self):
        """Generate afternoon briefing — daily wrap-up."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return

        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]
        closed_today = [
            p for p in self.active_positions
            if p.get("status") == "closed"
            and p.get("close_time", "").startswith(now.strftime("%Y-%m-%d"))
        ]

        total_realized = sum(p.get("realized_pnl", 0) for p in closed_today)

        briefing = {
            "timestamp": now.isoformat(),
            "type": "afternoon",
            "account": account,
            "trades_today": self.trades_today,
            "closed_positions": closed_today,
            "open_positions": open_positions,
            "realized_pnl": total_realized,
        }

        BRIEFINGS_DIR.mkdir(exist_ok=True)
        filename = f"afternoon_{now.strftime('%Y-%m-%d')}.json"
        filepath = BRIEFINGS_DIR / filename
        filepath.write_text(json.dumps(briefing, indent=2, default=str))
        log.info(f"Afternoon briefing saved: {filepath}")

        msg = (
            f"🌆 AFTERNOON BRIEFING — {now.strftime('%A %b %d')}\n\n"
            f"Account: ${account['equity']:,.2f}\n"
            f"P&L Today: ${account['pnl_today']:+,.2f}\n"
            f"Trades executed: {len(self.trades_today)}\n"
            f"Positions closed: {len(closed_today)}\n"
            f"Realized P&L: ${total_realized:+,.2f}\n"
            f"Open positions: {len(open_positions)}\n\n"
        )

        if self.trades_today:
            msg += "📊 TRADES:\n"
            for t in self.trades_today:
                msg += f"  {t['action']} {t.get('symbol', '?')} @ ${t.get('premium', 0):.2f}\n"

        self.telegram.send_briefing(msg)

    def _generate_plan(
        self, spy_price: float, vix: float,
        regime: MarketRegime, trend: TrendDirection,
    ) -> str:
        """Generate a human-readable multi-strategy trading plan."""
        lines = []

        # Strategy priorities based on regime
        if regime == MarketRegime.HIGH_VOL:
            lines.append(f"• VIX elevated ({vix:.1f}) — premium selling is attractive")
            lines.append(f"• Priority: Iron condors on ETFs ({', '.join(config.ETF_UNIVERSE)})")
            if trend != TrendDirection.NEUTRAL:
                lines.append(f"• Also scanning: {trend.value} credit spreads on stocks")
            lines.append(f"• Earnings strangles if any high-IV names upcoming")
        elif regime == MarketRegime.MEDIUM_VOL:
            if trend != TrendDirection.NEUTRAL:
                lines.append(f"• Moderate vol + {trend.value} trend → credit spreads")
                lines.append(f"• Scanning ETFs + stocks: {', '.join(config.ETF_UNIVERSE + config.STOCK_UNIVERSE[:4])}")
            else:
                lines.append(f"• Moderate vol, no clear trend — calendar spreads, small condors")
            lines.append(f"• Momentum plays on strong movers (TSLA, NVDA, AMD, META)")
        else:
            lines.append(f"• Low vol ({vix:.1f}) — wheel territory")
            lines.append(f"• Priority: Cash-secured puts on {', '.join(config.WHEEL_STOCKS[:5])}")
            lines.append(f"• Butterflies near round numbers (cheap lotto tickets)")
            lines.append(f"• Calendar spreads if term structure is steep")

        # Capacity
        open_count = sum(1 for p in self.active_positions if p.get("status") == "open")
        remaining = config.OPTIONS_MAX_POSITIONS - open_count
        lines.append(f"• Capacity: {remaining} new positions available (max {config.OPTIONS_MAX_POSITIONS})")
        lines.append(f"• Scanning: event-driven (SPY >0.5%, VIX >1pt, or hourly)")

        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # Status
    # ═══════════════════════════════════════════════════════════

    def status(self) -> str:
        """Return a human-readable status string."""
        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]

        lines = [
            f"{'=' * 50}",
            f"AlpacaBot Options Status (Multi-Strategy)",
            f"{'=' * 50}",
            f"Mode: {'📄 Paper' if self.paper else '💰 Live'} {'[DRY RUN]' if self.dry_run else '[LIVE TRADING]'}",
            f"Equity: ${account['equity']:,.2f}",
            f"Cash: ${account['cash']:,.2f}",
            f"P&L Today: ${account['pnl_today']:+,.2f}",
            f"Open Positions: {len(open_positions)}/{config.OPTIONS_MAX_POSITIONS}",
            f"Last SPY: ${self._last_spy_price:.2f} | Last VIX: {self._last_vix:.1f}",
            f"Last Scan: {self._last_scan_time.strftime('%H:%M ET') if self._last_scan_time else 'never'}",
        ]

        if open_positions:
            lines.append(f"{'─' * 50}")
            for pos in open_positions:
                lines.append(
                    f"  {pos['strategy']} {pos['underlying']} | "
                    f"Max P: ${pos['max_profit']:.0f} | Max L: ${pos['max_loss']:.0f}"
                )

        if self.trades_today:
            lines.append(f"{'─' * 50}")
            lines.append(f"Trades today: {len(self.trades_today)}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Main — Event-Driven Loop
# ═══════════════════════════════════════════════════════════════

def main():
    if not config.API_KEY or not config.SECRET_KEY:
        log.error("Missing API keys! Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        sys.exit(1)

    # Prevent multiple instances
    import os
    pid_file = Path("/workspace/AlpacaBot/bot.pid")
    if pid_file.exists():
        old_pid = pid_file.read_text().strip()
        try:
            old_pid_num = int(old_pid.replace("PID: ", ""))
            os.kill(old_pid_num, 0)
            log.error(f"Another instance already running (PID {old_pid_num}). Exiting.")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    pid_file.write_text(str(os.getpid()))

    bot = AlpacaBotOptions(dry_run=config.DRY_RUN)

    # Initial analysis
    log.info("Generating initial analysis...")
    bot.morning_briefing()
    log.info(bot.status())

    # ── Schedule: briefings only ──
    # Morning briefing at 09:20 (full scan + Claude review)
    # Afternoon briefing at 16:05 (daily wrap-up)
    schedule.every().day.at("09:20").do(bot.morning_briefing)
    schedule.every().day.at("16:05").do(bot.afternoon_briefing)

    log.info(f"Event-driven loop started: condition checks every {config.CONDITION_CHECK_INTERVAL}s")
    log.info(f"Triggers: SPY >{config.SPY_MOVE_THRESHOLD:.1%} move, VIX >{config.VIX_CHANGE_THRESHOLD:.1f}pt change, or new hour")

    try:
        while True:
            schedule.run_pending()

            # Lightweight condition check every CONDITION_CHECK_INTERVAL seconds
            now = datetime.now(ET)
            if bot._last_condition_check is None or (now - bot._last_condition_check).total_seconds() >= config.CONDITION_CHECK_INTERVAL:
                bot.check_conditions()
                bot._last_condition_check = now

            time.sleep(10)  # sleep 10s between loop iterations (responsive but not busy)

    except KeyboardInterrupt:
        log.info("Bot stopped.")
        log.info(bot.status())
    finally:
        if pid_file.exists():
            pid_file.unlink()


if __name__ == "__main__":
    main()
