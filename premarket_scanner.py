"""
Pre-Market Scanner — Extended Hours Opportunity Detection

Scans the watchlist every 30 minutes during pre-market (4:00-9:30 ET) for:
  - Gap ups/downs (vs prior close)
  - Pre-market volume spikes
  - Technical setups on daily charts

Proposals are autonomously reviewed by Claude (same as regular equity trades)
and executed automatically. Results are sent to Telegram for visibility.

Run: integrated into bot.py main loop (not standalone)
"""
import json
import logging
import os
import subprocess
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockLatestQuoteRequest,
    StockLatestBarRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame

import config
from strategies import generate_signal, calculate_rsi, calculate_ema
from telegram_alerts import TelegramAlerts

log = logging.getLogger("AlpacaBot")
ET = ZoneInfo("America/New_York")


class PremarketScanner:
    """Scans for pre-market trading opportunities and generates limit order proposals."""

    def __init__(self, data_client: StockHistoricalDataClient, tg: TelegramAlerts, bot=None):
        """
        data_client: Alpaca stock data client (shared with main bot)
        tg: Telegram alerts instance (shared with main bot)
        bot: reference to AlpacaBot instance for executing approved trades
        """
        self.data_client = data_client
        self.tg = tg
        self.bot = bot
        self.proposals_file = Path(config.PREMARKET_PROPOSALS_FILE)
        self._last_scan = None

    def is_premarket_hours(self) -> bool:
        """Check if we're in pre-market trading window (4:00-9:30 ET, weekdays)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        pm_open = now.replace(hour=4, minute=0, second=0, microsecond=0)
        pm_close = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return pm_open <= now < pm_close

    def is_afterhours(self) -> bool:
        """Check if we're in after-hours window (16:00-20:00 ET, weekdays)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        ah_open = now.replace(hour=16, minute=0, second=0, microsecond=0)
        ah_close = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return ah_open <= now < ah_close

    def is_extended_hours(self) -> bool:
        """Check if we're in any extended hours window."""
        return self.is_premarket_hours() or self.is_afterhours()

    def get_quote(self, symbol: str) -> dict:
        """Get latest bid/ask quote for a symbol."""
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            if symbol in quotes:
                q = quotes[symbol]
                bid = float(q.bid_price) if q.bid_price else 0.0
                ask = float(q.ask_price) if q.ask_price else 0.0
                bid_size = int(q.bid_size) if q.bid_size else 0
                ask_size = int(q.ask_size) if q.ask_size else 0
                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
                return {
                    "bid": bid, "ask": ask, "mid": mid,
                    "bid_size": bid_size, "ask_size": ask_size,
                    "spread": ask - bid if bid > 0 and ask > 0 else 0.0,
                    "spread_pct": (ask - bid) / mid * 100 if mid > 0 else 0.0,
                }
        except Exception as e:
            log.debug(f"Quote fetch failed for {symbol}: {e}")
        return {"bid": 0, "ask": 0, "mid": 0, "bid_size": 0, "ask_size": 0, "spread": 0, "spread_pct": 0}

    def get_prior_close(self, symbol: str) -> float:
        """Get the prior trading day's closing price."""
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(ET) - timedelta(days=5),
                limit=2,
            )
            bars = self.data_client.get_stock_bars(request)
            if symbol in bars.data and len(bars[symbol]) > 0:
                return float(bars[symbol][-1].close)
        except Exception as e:
            log.debug(f"Prior close fetch failed for {symbol}: {e}")
        return 0.0

    def get_daily_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        """Fetch daily bars for technical analysis."""
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=datetime.now(ET) - timedelta(days=days),
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
        except Exception as e:
            log.debug(f"Daily bars fetch failed for {symbol}: {e}")
            return pd.DataFrame()

    def scan(self) -> list:
        """
        Full pre-market scan cycle.
        Returns list of proposals sorted by strength.
        """
        now = datetime.now(ET)
        session = "PRE-MKT" if self.is_premarket_hours() else "AFTER-HRS"
        log.info(f"🌅 {session} scan starting at {now.strftime('%H:%M ET')}...")

        opportunities = []
        stats = {"no_quote": 0, "no_close": 0, "wide_spread": 0, "no_bars": 0, "low_score": 0, "passed": 0}

        # Skip symbols already rejected today
        rejected = self.bot.rejected_today if self.bot else set()

        for symbol in config.WATCHLIST:
            if symbol in rejected:
                continue
            try:
                # Get current quote (bid/ask)
                quote = self.get_quote(symbol)
                if quote["mid"] <= 0:
                    stats["no_quote"] += 1
                    continue

                # Get prior close for gap calculation
                prior_close = self.get_prior_close(symbol)
                if prior_close <= 0:
                    stats["no_close"] += 1
                    continue

                # Calculate gap
                gap_pct = (quote["mid"] - prior_close) / prior_close

                # Skip if spread is too wide (illiquid pre-market)
                if quote["spread_pct"] > 5.0:
                    stats["wide_spread"] += 1
                    log.debug(f"  {symbol}: spread too wide ({quote['spread_pct']:.1f}%)")
                    continue

                # Get daily bars for technical analysis
                daily_bars = self.get_daily_bars(symbol)
                if daily_bars.empty or len(daily_bars) < 30:
                    stats["no_bars"] += 1
                    continue

                # Run technical signal on daily chart
                signal = generate_signal(daily_bars, config.RSI_OVERSOLD, config.RSI_OVERBOUGHT)
                rsi = calculate_rsi(daily_bars)
                ema_20 = calculate_ema(daily_bars, 20)
                ema_50 = calculate_ema(daily_bars, 50)

                # Score the opportunity
                score = self._score_opportunity(
                    gap_pct=gap_pct,
                    signal=signal,
                    rsi=rsi,
                    ema_20=ema_20,
                    ema_50=ema_50,
                    price=quote["mid"],
                    spread_pct=quote["spread_pct"],
                )

                if score < 0.15:
                    stats["low_score"] += 1
                    log.info(f"  ⬇️ {symbol}: score {score:.2f} < 0.30 (gap {gap_pct:+.1%}, RSI {rsi:.1f}, spread {quote['spread_pct']:.1f}%)")
                    continue

                stats["passed"] += 1
                log.info(f"  ✅ {symbol}: score {score:.2f}, gap {gap_pct:+.1%}, RSI {rsi:.1f}, spread {quote['spread_pct']:.1f}%")

                # Calculate limit price (slightly above ask for buys)
                limit_buy_price = round(quote["ask"] * (1 + config.PREMARKET_LIMIT_OFFSET_PCT), 2)

                # Position sizing
                if self.bot:
                    qty = self.bot.calculate_position_size(limit_buy_price)
                else:
                    qty = int((config.MAX_CAPITAL * config.MAX_POSITION_PCT) / limit_buy_price)

                if qty <= 0:
                    continue

                opportunities.append({
                    "symbol": symbol,
                    "prior_close": prior_close,
                    "bid": quote["bid"],
                    "ask": quote["ask"],
                    "mid": quote["mid"],
                    "spread_pct": quote["spread_pct"],
                    "gap_pct": gap_pct,
                    "limit_price": limit_buy_price,
                    "qty": qty,
                    "total_cost": round(limit_buy_price * qty, 2),
                    "score": score,
                    "rsi": rsi,
                    "signal": signal["action"],
                    "confidence": signal["confidence"],
                    "reasons": signal["reasons"],
                    "session": session,
                    "scan_time": now.isoformat(),
                })

            except Exception as e:
                log.error(f"Pre-market scan error for {symbol}: {e}")
                continue

        # Sort by score descending, take top N
        opportunities.sort(key=lambda x: x["score"], reverse=True)
        proposals = opportunities[:config.PREMARKET_MAX_PROPOSALS]

        log.info(f"🌅 Scan stats: {len(config.WATCHLIST)} symbols — "
                 f"no_quote={stats['no_quote']}, no_close={stats['no_close']}, "
                 f"wide_spread={stats['wide_spread']}, no_bars={stats['no_bars']}, "
                 f"low_score={stats['low_score']}, passed={stats['passed']}")

        if not proposals:
            log.info(f"🌅 No pre-market opportunities found this scan")
            self.tg.send_trade_alert(
                f"🌅 {session} Scan {now.strftime('%H:%M ET')} — No opportunities found."
            )
            self._last_scan = now
            return proposals

        log.info(f"🌅 Found {len(proposals)} pre-market opportunities — sending to Claude for review...")

        # Autonomous Claude review (same pattern as regular equity trades)
        account = self.bot.get_account_info() if self.bot else {}
        review = self._claude_premarket_review(proposals, account)

        # Send review summary to Telegram
        review_msg = self._format_premarket_review(proposals, review, session)
        self.tg.send_trade_alert(review_msg)

        # Execute approved trades
        executed = []
        for decision in review.get("trades", []):
            trade_id = decision.get("trade_id", 0)
            if decision.get("decision") not in ("approve", "adjust"):
                # Cache rejection — don't re-propose this symbol today
                if 1 <= trade_id <= len(proposals) and self.bot:
                    rejected_sym = proposals[trade_id - 1]["symbol"]
                    self.bot.rejected_today.add(rejected_sym)
                    log.info(f"🌅 Cached rejection for {rejected_sym} — skipping for rest of day")
                log.info(f"🌅 Pre-mkt #{trade_id}: REJECTED — {decision.get('reason', '')}")
                continue

            if trade_id < 1 or trade_id > len(proposals):
                continue

            prop = proposals[trade_id - 1]
            qty = decision.get("adjusted_qty", prop["qty"])
            limit_price = decision.get("adjusted_price", prop["limit_price"])
            reason = (
                f"Pre-mkt | Score: {prop['score']:.0%} | Gap: {prop['gap_pct']:+.1%} | "
                f"{', '.join(prop['reasons'][:2])} | Claude: {decision.get('reason', 'approved')}"
            )

            if self.bot:
                success = self.bot.place_premarket_buy(
                    symbol=prop["symbol"], qty=qty,
                    limit_price=limit_price, reason=reason,
                )
                if success:
                    executed.append(prop["symbol"])

            if self.bot and not self.bot.can_buy():
                log.info("Pre-market: position limit reached. Stopping execution.")
                break

        log.info(f"🌅 Pre-market scan complete: {len(executed)} trades executed")
        self._save_proposals(proposals)  # Save for /premarket status viewing
        self._last_scan = now
        return proposals

    def _score_opportunity(self, gap_pct: float, signal: dict, rsi: float,
                           ema_20: float, ema_50: float, price: float,
                           spread_pct: float) -> float:
        """
        Score a pre-market opportunity 0-1.
        Two playbooks:
          1. Mean reversion — gap-down + oversold RSI + technical support
          2. Momentum continuation — gap-up + trend confirmation + catalyst
        """
        score = 0.0

        # --- Gap analysis (both directions are valid) ---
        abs_gap = abs(gap_pct)
        if gap_pct < -0.03:
            score += 0.25  # Big gap down — mean reversion bounce
        elif gap_pct < -0.015:
            score += 0.15  # Moderate gap down
        elif gap_pct > 0.03:
            score += 0.20  # Big gap up — momentum / catalyst play
        elif gap_pct > 0.015:
            score += 0.12  # Moderate gap up
        elif abs_gap > 0.005:
            score += 0.05  # Small move — mild interest

        # Technical signal from daily chart
        if signal["action"] == "BUY":
            score += 0.25 * signal["confidence"]
        elif signal["action"] == "SELL":
            score -= 0.10  # Penalize sell signals (less harsh)

        # RSI context — depends on gap direction
        if gap_pct < 0:
            # Gap down: oversold RSI is bullish (mean reversion)
            if rsi < 30:
                score += 0.20
            elif rsi < 40:
                score += 0.10
        else:
            # Gap up: mid-range RSI is fine for momentum (not overbought)
            if 40 <= rsi <= 65:
                score += 0.10  # Sweet spot — room to run
            elif rsi > 75:
                score -= 0.10  # Extended — risky to chase

        # EMA trend — price above rising EMAs is bullish
        if ema_20 > ema_50 and price > ema_20:
            score += 0.15  # Strong uptrend
        elif ema_20 > ema_50:
            score += 0.08  # Rising trend but price dipped below
        elif ema_20 < ema_50 and price < ema_20:
            score += 0.05  # Downtrend but could be mean reversion if oversold

        # Spread penalty — wider spread = harder to fill profitably
        if spread_pct < 0.5:
            score += 0.10  # Tight spread bonus
        elif spread_pct < 1.0:
            score += 0.05  # Decent spread
        elif spread_pct > 1.5:
            score -= 0.10  # Wide spread penalty

        return max(0.0, min(1.0, score))

    def _claude_premarket_review(self, proposals: list, account: dict) -> dict:
        """Send pre-market proposals to Claude CLI for autonomous review."""
        prompt = f"""Review these PRE-MARKET extended-hours trade proposals from an automated scanner.

ACCOUNT:
- Equity: ${account.get('equity', 0):,.2f}
- Cash: ${account.get('cash', 0):,.2f}
- P&L Today: ${account.get('pnl_today', 0):+,.2f}
- Session: Extended Hours (limit orders only)
- Date: {datetime.now(ET).strftime('%A %B %d, %Y %H:%M ET')}

PROPOSED PRE-MARKET TRADES:
"""
        for i, prop in enumerate(proposals, 1):
            prompt += f"""
--- Trade #{i} ---
Action: LIMIT BUY {prop['qty']} shares of {prop['symbol']}
Limit Price: ${prop['limit_price']:.2f} (total: ${prop['total_cost']:,.2f})
Prior Close: ${prop['prior_close']:.2f} | Gap: {prop['gap_pct']:+.1%}
Bid: ${prop['bid']:.2f} / Ask: ${prop['ask']:.2f} (spread: {prop['spread_pct']:.1f}%)
Score: {prop['score']:.0%} | RSI: {prop['rsi']:.1f} | Signal: {prop['signal']} ({prop['confidence']:.0%})
Reasons: {', '.join(prop['reasons'])}
"""
        prompt += """
Pre-market rules: orders are LIMIT only, liquidity is thin, spreads are wider.
Be decisive. Approve strong setups, reject marginal ones. For each trade: approve, reject, or adjust."""

        review_schema = json.dumps({
            "type": "object",
            "properties": {
                "market_assessment": {"type": "string", "description": "1-2 sentence pre-market view"},
                "trades": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "trade_id": {"type": "integer"},
                            "decision": {"type": "string", "enum": ["approve", "reject", "adjust"]},
                            "adjusted_qty": {"type": "integer", "description": "Share quantity"},
                            "adjusted_price": {"type": "number", "description": "Limit price (same or adjusted)"},
                            "reason": {"type": "string"},
                            "risk_notes": {"type": "string"}
                        },
                        "required": ["trade_id", "decision", "adjusted_qty", "adjusted_price", "reason"]
                    }
                },
                "summary": {"type": "string", "description": "2-3 sentence summary for Telegram"}
            },
            "required": ["market_assessment", "trades", "summary"]
        })

        system_prompt = """You are a senior equity risk manager reviewing pre-market / extended-hours trade proposals.

Rules:
- Pre-market has THIN LIQUIDITY and WIDER SPREADS. Be extra cautious.
- Only approve trades with strong technical confluence AND tight spreads (<1%)
- Gap-down mean reversion setups are ideal IF technical support exists (RSI oversold + EMA support)
- Wide spreads (>1.5%) = hard to fill profitably, reject unless signal is very strong
- Limit orders may not fill — that's OK, it's protective
- Max 5% of equity per position
- Fewer high-conviction trades > many marginal ones
- "No trade" is always valid. Protect capital first.
- You can adjust the limit price if the proposed price is too aggressive

Be direct. No hedging language. You are protecting real money in thin pre-market conditions."""

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
                log.error(f"Claude pre-market review failed (exit {result.returncode}): {result.stderr[:500]}")
                return self._fallback_premarket_review(proposals)

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
                log.error(f"Failed to parse Claude pre-market review: {output[:500]}")
                return self._fallback_premarket_review(proposals)

            if "trades" not in review:
                log.error("Pre-market review missing 'trades' field")
                return self._fallback_premarket_review(proposals)

            log.info(f"Claude pre-market review complete: {review.get('summary', '')}")
            return review

        except subprocess.TimeoutExpired:
            log.error("Claude pre-market review timed out (180s)")
            return self._fallback_premarket_review(proposals)
        except Exception as e:
            log.error(f"Claude pre-market review failed: {e}")
            return self._fallback_premarket_review(proposals)

    def _fallback_premarket_review(self, proposals: list) -> dict:
        """Conservative fallback when Claude is unavailable — only approve highest-scoring trades."""
        log.warning("Using fallback pre-market review (Claude unavailable)")
        trades = []
        for i, prop in enumerate(proposals, 1):
            # Extra conservative for pre-market: score >= 0.65 AND tight spread
            if prop["score"] >= 0.65 and prop["spread_pct"] < 1.0:
                trades.append({
                    "trade_id": i,
                    "decision": "approve",
                    "adjusted_qty": prop["qty"],
                    "adjusted_price": prop["limit_price"],
                    "reason": f"Fallback: score {prop['score']:.0%}, spread {prop['spread_pct']:.1f}%",
                })
            else:
                trades.append({
                    "trade_id": i,
                    "decision": "reject",
                    "adjusted_qty": prop["qty"],
                    "adjusted_price": prop["limit_price"],
                    "reason": f"Fallback: insufficient score ({prop['score']:.0%}) or wide spread ({prop['spread_pct']:.1f}%)",
                })
        return {
            "market_assessment": "Claude unavailable — using conservative fallback rules",
            "trades": trades,
            "summary": f"Fallback review: {sum(1 for t in trades if t['decision'] == 'approve')}/{len(trades)} approved (high-score only)",
        }

    def _format_premarket_review(self, proposals: list, review: dict, session: str) -> str:
        """Format pre-market review results for Telegram."""
        now = datetime.now(ET)
        lines = [
            f"🌅 {session} AUTONOMOUS REVIEW — {now.strftime('%H:%M ET')}",
            f"📊 {review.get('market_assessment', '')}",
            "",
        ]

        for decision in review.get("trades", []):
            tid = decision.get("trade_id", 0)
            if tid < 1 or tid > len(proposals):
                continue
            prop = proposals[tid - 1]
            status = decision.get("decision", "?").upper()
            icon = "✅" if status in ("APPROVE", "ADJUST") else "❌"
            qty = decision.get("adjusted_qty", prop["qty"])
            price = decision.get("adjusted_price", prop["limit_price"])

            lines.append(f"{icon} #{tid} {prop['symbol']} — {status}")
            lines.append(f"  Gap: {prop['gap_pct']:+.1%} | RSI: {prop['rsi']:.1f} | Score: {prop['score']:.0%}")
            if status in ("APPROVE", "ADJUST"):
                lines.append(f"  >>> LIMIT BUY {qty} @ ${price:.2f}")
            lines.append(f"  {decision.get('reason', '')}")
            if decision.get("risk_notes"):
                lines.append(f"  ⚠️ {decision['risk_notes']}")
            lines.append("")

        lines.append(f"📝 {review.get('summary', '')}")
        return "\n".join(lines)

    def _save_proposals(self, proposals: list):
        """Save proposals to JSON file for the approval system."""
        try:
            # Load existing pending proposals (don't overwrite)
            existing = []
            if self.proposals_file.exists():
                with open(self.proposals_file) as f:
                    existing = json.load(f)

            # Add new proposals with unique IDs
            max_id = max((p.get("id", 0) for p in existing), default=0)
            for i, prop in enumerate(proposals, 1):
                prop["id"] = max_id + i
                prop["status"] = "pending"
                existing.append(prop)

            with open(self.proposals_file, "w") as f:
                json.dump(existing, f, indent=2, default=str)

            log.info(f"Saved {len(proposals)} proposals to {self.proposals_file}")

        except Exception as e:
            log.error(f"Failed to save proposals: {e}")

    # _send_proposals_to_telegram removed — replaced by _format_premarket_review (autonomous flow)

    def get_recent_results(self) -> list:
        """Get today's scan results (all proposals with their review status)."""
        return self._load_proposals()

    def clear_stale_proposals(self):
        """Remove proposals older than current session."""
        try:
            if not self.proposals_file.exists():
                return
            proposals = self._load_proposals()
            now = datetime.now(ET)
            today_str = now.strftime("%Y-%m-%d")
            fresh = [p for p in proposals if p.get("scan_time", "").startswith(today_str)]
            with open(self.proposals_file, "w") as f:
                json.dump(fresh, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Failed to clear stale proposals: {e}")

    def _load_proposals(self) -> list:
        """Load proposals from file."""
        try:
            if self.proposals_file.exists():
                with open(self.proposals_file) as f:
                    return json.load(f)
        except Exception as e:
            log.error(f"Failed to load proposals: {e}")
        return []

    def _update_proposal(self, proposals: list, updated: dict):
        """Update a single proposal in the file."""
        try:
            for i, p in enumerate(proposals):
                if p.get("id") == updated.get("id"):
                    proposals[i] = updated
                    break
            with open(self.proposals_file, "w") as f:
                json.dump(proposals, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Failed to update proposal: {e}")
