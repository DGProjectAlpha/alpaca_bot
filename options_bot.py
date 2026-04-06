"""
AlpacaBot Options — Automated options trading bot on Alpaca.
Strategies: Iron Condors, Credit Spreads, Wheel.
Integrated with Telegram for alerts and briefings.

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
    GetOrdersRequest, QueryOrderStatus
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
    StrategyType, MarketRegime, TrendDirection, OptionLeg, OptionsTradeSetup,
    classify_market_regime, detect_trend, select_strategy,
    should_close_position, calculate_position_risk, format_setup_summary,
    build_iron_condor, build_credit_spread, build_wheel_csp,
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
        self.pending_setups = []  # setups waiting for execution
        self.active_positions = self._load_positions()
        self.rejected_signatures = set()  # track rejected trade signatures to avoid re-proposing

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
        log.info(f"Max Risk/Trade: {config.OPTIONS_MAX_RISK_PER_TRADE * 100:.0f}%")
        log.info(f"{'=' * 55}")

    # ═══════════════════════════════════════════════════════════
    # Market Data
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

    def get_spy_price(self) -> float:
        """Get current SPY price."""
        try:
            request = StockLatestBarRequest(symbol_or_symbols="SPY")
            bars = self.stock_data.get_stock_latest_bar(request)
            if "SPY" in bars:
                return float(bars["SPY"].close)
        except Exception as e:
            log.error(f"Failed to get SPY price: {e}")
        return 0.0

    def get_vix(self) -> float:
        """
        Estimate VIX from VIXY ETF price.
        VIXY tracks VIX short-term futures. Relationship is nonlinear
        due to contango/backwardation, but we can approximate:
        - VIXY ~$15-20 → VIX ~12-15 (low vol)
        - VIXY ~$25-35 → VIX ~18-25 (moderate)
        - VIXY ~$40-60 → VIX ~28-40 (high)
        - VIXY ~$60+   → VIX ~40+ (panic)
        """
        try:
            request = StockLatestBarRequest(symbol_or_symbols="VIXY")
            bars = self.stock_data.get_stock_latest_bar(request)
            if "VIXY" in bars:
                vixy = float(bars["VIXY"].close)
                # Piecewise linear approximation calibrated to recent data
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

    def get_spy_emas(self) -> dict:
        """Get SPY EMA-20 and EMA-50 for trend detection."""
        try:
            request = StockBarsRequest(
                symbol_or_symbols="SPY",
                timeframe=TimeFrame.Hour,
                start=datetime.now(ET) - timedelta(days=30),
            )
            bars = self.stock_data.get_stock_bars(request)
            if "SPY" not in bars.data or len(bars["SPY"]) < 50:
                return {"ema_20": 0, "ema_50": 0, "prices": []}

            closes = [float(b.close) for b in bars["SPY"]]
            df = pd.Series(closes)
            ema_20 = df.ewm(span=20, adjust=False).mean().iloc[-1]
            ema_50 = df.ewm(span=50, adjust=False).mean().iloc[-1]

            return {
                "ema_20": ema_20,
                "ema_50": ema_50,
                "prices": closes[-30:],
            }
        except Exception as e:
            log.error(f"Failed to get SPY EMAs: {e}")
            return {"ema_20": 0, "ema_50": 0, "prices": []}

    def get_option_expirations(self, symbol: str = "SPY") -> list:
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

        # Fallback: generate expected daily expirations for SPY
        dates = []
        for i in range(0, 45):
            d = datetime.now() + timedelta(days=i)
            if d.weekday() < 5:
                dates.append(d.strftime("%Y-%m-%d"))
        return dates[:20]

    def get_option_chain(self, symbol: str, expiration: str) -> dict:
        """
        Get option chain for a symbol and expiration.
        Returns dict with 'calls' and 'puts' lists.
        """
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

    def get_wheel_candidates(self) -> list:
        """Get current prices for wheel-eligible stocks."""
        candidates = []
        for symbol in config.WHEEL_STOCKS:
            try:
                request = StockLatestBarRequest(symbol_or_symbols=symbol)
                bars = self.stock_data.get_stock_latest_bar(request)
                if symbol in bars:
                    price = float(bars[symbol].close)
                    # Only include if we can afford 100 shares (for assignment)
                    if price * 100 <= config.OPTIONS_MAX_CAPITAL:
                        candidates.append({"symbol": symbol, "price": price})
            except Exception:
                continue
        return candidates

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

            # Find closest strike
            best = None
            best_diff = float("inf")
            for c in contracts:
                diff = abs(c["strike"] - leg.strike)
                if diff < best_diff:
                    best_diff = diff
                    best = c

            if best:
                leg.symbol = best["symbol"]
                leg.strike = best["strike"]  # update to actual available strike

                # Get current quote
                quote = self.get_option_quote(best["symbol"])
                leg.premium = quote["mid"]

        return setup

    # ═══════════════════════════════════════════════════════════
    # Trade Execution
    # ═══════════════════════════════════════════════════════════

    def execute_setup(self, setup: OptionsTradeSetup) -> bool:
        """
        Execute an options trade setup. Places orders for each leg.

        For spreads, places each leg separately (Alpaca may support
        multi-leg but separate legs are more reliable on paper).
        """
        if self.dry_run:
            log.info(f"🏜️ DRY RUN — would execute:")
            log.info(format_setup_summary(setup))
            self.telegram.send_trade_alert(
                f"🏜️ DRY RUN SIGNAL\n\n{format_setup_summary(setup)}"
            )
            return True

        # Risk check
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
                    qty=setup.contracts,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(leg.premium, 2),
                )
                result = self.trading_client.submit_order(order)

                emoji = "🟢" if leg.side == "buy" else "🔴"
                log.info(
                    f"{emoji} {leg.side.upper()} {setup.contracts}x "
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
                    "qty": setup.contracts,
                    "premium": leg.premium,
                    "order_id": str(result.id),
                    "status": str(result.status),
                })

            except Exception as e:
                log.error(f"❌ Failed to execute leg: {leg.symbol} — {e}")
                all_success = False

        if all_success:
            # Save position for tracking
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
                }
                for l in setup.legs
            ],
            "contracts": setup.contracts,
            "max_profit": setup.max_profit,
            "max_loss": setup.max_loss,
            "entry_time": datetime.now(ET).isoformat(),
            "entry_vix": self._last_vix,
            "status": "open",
        }
        self.active_positions.append(position)
        POSITIONS_FILE.write_text(json.dumps(self.active_positions, indent=2))

    def _update_positions_file(self):
        """Write current positions state to file."""
        POSITIONS_FILE.write_text(json.dumps(self.active_positions, indent=2))

    def check_positions(self):
        """Check all open positions for exit conditions."""
        vix = self.get_vix()
        self._last_vix = vix

        for pos in self.active_positions:
            if pos.get("status") != "open":
                continue

            # Calculate current P&L by checking option quotes
            total_pnl = 0
            for leg in pos["legs"]:
                quote = self.get_option_quote(leg["symbol"])
                current_mid = quote["mid"]
                entry_premium = leg["premium"]

                if leg["side"] == "sell":
                    # Sold: profit when price drops
                    leg_pnl = (entry_premium - current_mid) * 100 * pos["contracts"]
                else:
                    # Bought: profit when price rises
                    leg_pnl = (current_mid - entry_premium) * 100 * pos["contracts"]
                total_pnl += leg_pnl

            max_profit = pos["max_profit"] * pos["contracts"]
            pnl_pct = total_pnl / max_profit if max_profit > 0 else 0

            # Calculate DTE remaining
            exp_dates = [l["expiration"] for l in pos["legs"]]
            min_exp = min(exp_dates) if exp_dates else datetime.now().strftime("%Y-%m-%d")
            dte = (datetime.strptime(min_exp, "%Y-%m-%d").date() - datetime.now().date()).days

            should_close, reason = should_close_position(
                pos, pnl_pct, dte, vix
            )

            log.info(
                f"  Position: {pos['strategy']} {pos['underlying']} | "
                f"P&L: ${total_pnl:+.0f} ({pnl_pct:+.0%} of max) | "
                f"DTE: {dte} | {'⚠️ CLOSE: ' + reason if should_close else '✅ HOLD'}"
            )

            if should_close:
                self._close_position(pos, reason, total_pnl)

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
                # Reverse the original side
                close_side = OrderSide.BUY if leg["side"] == "sell" else OrderSide.SELL
                quote = self.get_option_quote(leg["symbol"])
                price = quote["mid"]

                order = LimitOrderRequest(
                    symbol=leg["symbol"],
                    qty=position["contracts"],
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
    # Main Scan Cycle
    # ═══════════════════════════════════════════════════════════

    def run_cycle(self):
        """One full analysis + trade cycle."""
        now = datetime.now(ET)
        market_open = now.replace(hour=9, minute=30, second=0)
        market_close = now.replace(hour=16, minute=0, second=0)

        if now < market_open or now > market_close:
            log.info(f"Market closed. ET time: {now.strftime('%H:%M')}")
            return

        if now.weekday() >= 5:
            log.info("Weekend. Market closed.")
            return

        log.info(f"{'─' * 50}")
        log.info(f"Options scan cycle at {now.strftime('%H:%M ET')}")

        # 1. Check existing positions
        if self.active_positions:
            log.info("Checking open positions...")
            self.check_positions()

        # 2. Count open positions
        open_count = sum(1 for p in self.active_positions if p.get("status") == "open")
        if open_count >= config.OPTIONS_MAX_POSITIONS:
            log.info(f"Max positions ({config.OPTIONS_MAX_POSITIONS}) reached. Holding.")
            return

        # 3. Gather market data
        spy_price = self.get_spy_price()
        vix = self.get_vix()
        self._last_vix = vix
        ema_data = self.get_spy_emas()
        expirations = self.get_option_expirations("SPY")
        wheel_candidates = self.get_wheel_candidates()

        if spy_price == 0:
            log.error("Could not get SPY price. Skipping cycle.")
            return

        regime = classify_market_regime(vix)
        trend = detect_trend(
            ema_data["prices"],
            ema_data["ema_20"],
            ema_data["ema_50"],
        )

        log.info(f"SPY: ${spy_price:.2f} | VIX: {vix:.1f} ({regime.value}) | Trend: {trend.value}")
        log.info(f"EMA20: ${ema_data['ema_20']:.2f} | EMA50: ${ema_data['ema_50']:.2f}")
        log.info(f"Available expirations: {len(expirations)} | Wheel candidates: {len(wheel_candidates)}")

        # 4. Run strategy selector
        market_data = {
            "spy_price": spy_price,
            "vix": vix,
            "regime": regime.value,
            "trend": trend.value,
            "ema_20": ema_data["ema_20"],
            "ema_50": ema_data["ema_50"],
            "recent_prices": ema_data["prices"],
            "available_expirations": expirations,
            "wheel_candidates": wheel_candidates,
        }

        setups = select_strategy(
            market_data, config.OPTIONS_MAX_CAPITAL, self.active_positions
        )

        if not setups:
            log.info("No viable setups found this cycle.")
            return

        # Filter out previously rejected trade signatures
        filtered = []
        for setup in setups:
            sig = f"{setup.strategy.value}_{setup.underlying}_{setup.target_dte}"
            for leg in setup.legs:
                sig += f"_{leg.strike}"
            if sig not in self.rejected_signatures:
                filtered.append(setup)
            else:
                log.info(f"Skipping previously rejected: {sig}")
        setups = filtered

        if not setups:
            log.info("All setups were previously rejected. Waiting for new conditions.")
            return

        # 5. Resolve option symbols and get real quotes for top setups
        resolved_setups = []
        for setup in setups[:3]:  # resolve top 3 for review
            setup = self.resolve_option_symbols(setup)
            if setup.legs:
                real_premium = sum(
                    l.premium for l in setup.legs if l.side == "sell"
                ) - sum(
                    l.premium for l in setup.legs if l.side == "buy"
                )
                if real_premium > 0:
                    setup.max_profit = real_premium * 100
                    # Recalculate max_loss and risk/reward with real numbers
                    # For spreads: max_loss = (spread_width * 100) - net_premium
                    if len(setup.legs) >= 4:  # iron condor
                        sell_strikes = [l.strike for l in setup.legs if l.side == "sell"]
                        buy_strikes = [l.strike for l in setup.legs if l.side == "buy"]
                        if sell_strikes and buy_strikes:
                            spread_width = max(abs(s - b) for s in sell_strikes for b in buy_strikes if abs(s - b) < 20)
                            setup.max_loss = (spread_width * 100) - setup.max_profit
                    elif len(setup.legs) == 2:  # credit spread
                        strikes = [l.strike for l in setup.legs]
                        spread_width = abs(strikes[0] - strikes[1])
                        setup.max_loss = (spread_width * 100) - setup.max_profit
                    # Update risk/reward ratio with real values
                    setup.risk_reward_ratio = setup.max_profit / setup.max_loss if setup.max_loss > 0 else 0
                    # Recalculate score with real R:R
                    setup.score = (
                        setup.probability_of_profit * 0.4 +
                        min(setup.risk_reward_ratio, 1.0) * 0.3 +
                        0.3  # vol bonus (we're already in the right regime)
                    )
            resolved_setups.append(setup)

        # 6. Convert setups to proposal dicts for review
        proposals = []
        for setup in resolved_setups:
            proposals.append({
                "strategy": setup.strategy.value,
                "underlying": setup.underlying,
                "contracts": setup.contracts,
                "max_profit": setup.max_profit,
                "max_loss": setup.max_loss,
                "probability_of_profit": setup.probability_of_profit,
                "risk_reward_ratio": setup.risk_reward_ratio,
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
                    }
                    for l in setup.legs
                ],
            })

        # 7. Post proposals to Telegram
        proposals_msg = format_proposals_for_telegram(proposals, market_data)
        self.telegram.send_trade_alert(proposals_msg)

        # 8. Save proposals and send to Claude for review
        pending = save_proposals(proposals, market_data)
        account_info = self.get_account_info()
        review = review_trades(pending, account_info)

        # 9. Save approvals and post review to Telegram
        approvals = save_approvals(review, proposals)
        review_msg = format_review_for_telegram(review, approvals)
        self.telegram.send_trade_alert(review_msg)

        # 10. Track rejected trades so we don't re-propose them
        for trade_decision in review.get("trades", []):
            if trade_decision.get("decision") == "reject":
                tid = trade_decision.get("trade_id", 0)
                if 0 < tid <= len(resolved_setups):
                    setup = resolved_setups[tid - 1]
                    sig = f"{setup.strategy.value}_{setup.underlying}_{setup.target_dte}"
                    for leg in setup.legs:
                        sig += f"_{leg.strike}"
                    self.rejected_signatures.add(sig)
                    log.info(f"Marked as rejected: {sig}")

        # 11. Execute only approved trades
        approved_trades = approvals.get("approved_trades", [])
        if not approved_trades:
            log.info("No trades approved by Claude. Standing by.")
            return

        for approved in approved_trades:
            # Find the matching resolved setup
            for setup in resolved_setups:
                if (setup.strategy.value == approved["strategy"]
                        and setup.underlying == approved["underlying"]):
                    # Apply adjusted contract count if Claude changed it
                    setup.contracts = approved.get("contracts", setup.contracts)
                    log.info(f"Executing approved trade: {setup.strategy.value} on {setup.underlying} ({setup.contracts} contracts)")
                    self.execute_setup(setup)
                    break

        # 11. Log portfolio status
        self._log_status()

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
    # Briefings (for Telegram bot to read)
    # ═══════════════════════════════════════════════════════════

    def morning_briefing(self):
        """
        Generate morning briefing + run the proposal/review pipeline.
        Flow: scan → propose → Claude reviews → post results → wait for market open to execute.
        """
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return

        # New day, clear rejection memory — market conditions have changed
        self.rejected_signatures.clear()
        self.trades_today = []
        log.info("Morning reset: cleared rejection memory and trade log")

        spy_price = self.get_spy_price()
        vix = self.get_vix()
        self._last_vix = vix
        ema_data = self.get_spy_emas()
        regime = classify_market_regime(vix)
        trend = detect_trend(
            ema_data["prices"], ema_data["ema_20"], ema_data["ema_50"]
        )
        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]

        briefing = {
            "timestamp": now.isoformat(),
            "type": "morning",
            "market": {
                "spy_price": spy_price,
                "vix": vix,
                "regime": regime.value,
                "trend": trend.value,
                "ema_20": ema_data["ema_20"],
                "ema_50": ema_data["ema_50"],
            },
            "account": account,
            "open_positions": len(open_positions),
            "positions": open_positions,
            "plan": self._generate_plan(spy_price, vix, regime, trend),
            "trades_yesterday": len(self.trades_today),
        }

        # Save briefing file
        filename = f"morning_{now.strftime('%Y-%m-%d')}.json"
        filepath = BRIEFINGS_DIR / filename
        filepath.write_text(json.dumps(briefing, indent=2, default=str))
        log.info(f"Morning briefing saved: {filepath}")

        # Send morning overview to Telegram
        plan_text = briefing["plan"]
        msg = (
            f"🌅 MORNING BRIEFING — {now.strftime('%A %b %d')}\n\n"
            f"SPY: ${spy_price:.2f}\n"
            f"VIX: {vix:.1f} ({regime.value.replace('_', ' ')})\n"
            f"Trend: {trend.value}\n"
            f"Account: ${account['equity']:,.2f}\n"
            f"Open positions: {len(open_positions)}\n\n"
            f"📋 TODAY'S PLAN:\n{plan_text}\n\n"
            f"🔍 Running pre-market scan and sending to Claude for review..."
        )
        self.telegram.send_briefing(msg)

        # Run the proposal + review cycle (trades won't execute until market opens via run_cycle)
        log.info("Morning pre-market scan starting...")

    def afternoon_briefing(self):
        """Generate afternoon briefing — what happened today."""
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
        """Generate a human-readable trading plan for the day."""
        lines = []

        if regime == MarketRegime.HIGH_VOL:
            lines.append(f"• VIX is elevated ({vix:.1f}) — prime conditions for selling premium")
            lines.append(f"• Looking for 0-3 DTE iron condors on SPY")
            lines.append(f"• Target: collect premium while SPY stays in range")
            if trend != TrendDirection.NEUTRAL:
                lines.append(f"• Also watching for {trend.value} credit spreads (7-14 DTE)")
        elif regime == MarketRegime.MEDIUM_VOL:
            if trend != TrendDirection.NEUTRAL:
                lines.append(f"• Moderate vol + {trend.value} trend → credit spreads")
                if trend == TrendDirection.BULLISH:
                    lines.append(f"• Selling bull put spreads below market")
                else:
                    lines.append(f"• Selling bear call spreads above market")
            else:
                lines.append(f"• Moderate vol, no clear trend — cautious iron condors")
                lines.append(f"• Using tighter spreads ($1 wide)")
        else:
            lines.append(f"• Low vol ({vix:.1f}) — poor conditions for premium selling")
            lines.append(f"• Shifting to Wheel strategy on cheap stocks")
            lines.append(f"• Selling cash-secured puts on quality names for income")

        open_count = sum(1 for p in self.active_positions if p.get("status") == "open")
        remaining = config.OPTIONS_MAX_POSITIONS - open_count
        lines.append(f"• Capacity: {remaining} new positions available (max {config.OPTIONS_MAX_POSITIONS})")

        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════
    # Human-Readable Status
    # ═══════════════════════════════════════════════════════════

    def status(self) -> str:
        """Return a human-readable status string."""
        account = self.get_account_info()
        open_positions = [p for p in self.active_positions if p.get("status") == "open"]

        lines = [
            f"{'=' * 45}",
            f"AlpacaBot Options Status",
            f"{'=' * 45}",
            f"Mode: {'📄 Paper' if self.paper else '💰 Live'} {'[DRY RUN]' if self.dry_run else '[LIVE TRADING]'}",
            f"Equity: ${account['equity']:,.2f}",
            f"Cash: ${account['cash']:,.2f}",
            f"P&L Today: ${account['pnl_today']:+,.2f}",
            f"Open Positions: {len(open_positions)}/{config.OPTIONS_MAX_POSITIONS}",
        ]

        if open_positions:
            lines.append(f"{'─' * 45}")
            for pos in open_positions:
                lines.append(
                    f"  {pos['strategy']} {pos['underlying']} | "
                    f"Max P: ${pos['max_profit']:.0f} | Max L: ${pos['max_loss']:.0f}"
                )

        if self.trades_today:
            lines.append(f"{'─' * 45}")
            lines.append(f"Trades today: {len(self.trades_today)}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Main
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
        # Check if process is still running
        try:
            old_pid_num = int(old_pid.replace("PID: ", ""))
            os.kill(old_pid_num, 0)  # signal 0 = check if alive
            log.error(f"Another instance already running (PID {old_pid_num}). Exiting.")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # old process is dead, we can proceed
    pid_file.write_text(str(os.getpid()))

    # Start in DRY RUN mode — analyze but don't trade
    bot = AlpacaBotOptions(dry_run=config.DRY_RUN)

    # Run morning briefing (includes pre-market scan)
    log.info("Generating initial analysis...")
    bot.morning_briefing()

    # Run initial cycle (will generate proposals → Claude review → execute approved)
    bot.run_cycle()
    log.info(bot.status())

    # Schedule
    # Morning: briefing at 9:20 (before market open), then first trade cycle at 9:35
    # Intraday: scan every 15 min for position management + new opportunities
    # Afternoon: wrap-up briefing at 16:05
    log.info(f"Scheduling: scans every {config.SCAN_INTERVAL_MINUTES}min, briefings at 9:20 and 16:05 ET")
    schedule.every(config.SCAN_INTERVAL_MINUTES).minutes.do(bot.run_cycle)
    schedule.every().day.at("09:20").do(bot.morning_briefing)
    schedule.every().day.at("16:05").do(bot.afternoon_briefing)

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Bot stopped.")
        log.info(bot.status())


if __name__ == "__main__":
    main()
