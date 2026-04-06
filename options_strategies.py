"""
Options Strategy Engine — Iron Condors, Credit Spreads, and The Wheel
Designed for defined-risk options trading on Alpaca.

Strategy Selection Logic:
  - High VIX (>20): Iron Condors (sell premium in both directions)
  - Medium VIX (15-20) + Trend: Credit Spreads (directional)
  - Low VIX (<15): Wheel Strategy on cheap equities (sell CSPs)
  - Always defined risk. Always know max loss before entry.
"""
import math
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StrategyType(Enum):
    IRON_CONDOR = "iron_condor"
    BULL_PUT_SPREAD = "bull_put_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"


class MarketRegime(Enum):
    HIGH_VOL = "high_vol"       # VIX > 20
    MEDIUM_VOL = "medium_vol"   # VIX 15-20
    LOW_VOL = "low_vol"         # VIX < 15


class TrendDirection(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class OptionLeg:
    """Single leg of an options trade."""
    symbol: str               # OCC symbol (e.g., "SPY250407C00520000")
    side: str                 # "buy" or "sell"
    option_type: str          # "call" or "put"
    strike: float
    expiration: str           # "2025-04-07"
    quantity: int = 1
    premium: float = 0.0      # per-contract premium (filled after quote)


@dataclass
class OptionsTradeSetup:
    """Complete trade setup with all legs and risk parameters."""
    strategy: StrategyType
    underlying: str           # e.g., "SPY"
    legs: list                # List of OptionLeg
    max_profit: float = 0.0   # per-contract
    max_loss: float = 0.0     # per-contract (always positive number)
    breakeven_low: float = 0.0
    breakeven_high: float = 0.0
    probability_of_profit: float = 0.0  # estimated
    risk_reward_ratio: float = 0.0
    target_dte: int = 0       # days to expiration
    reason: str = ""
    score: float = 0.0        # 0-1, how good this setup is
    contracts: int = 1        # how many contracts to trade


# ═══════════════════════════════════════════════════════════════
# Market Analysis
# ═══════════════════════════════════════════════════════════════

def classify_market_regime(vix: float) -> MarketRegime:
    """Classify current market volatility regime."""
    if vix > 20:
        return MarketRegime.HIGH_VOL
    elif vix > 15:
        return MarketRegime.MEDIUM_VOL
    else:
        return MarketRegime.LOW_VOL


def detect_trend(prices: list, ema_short: float, ema_long: float) -> TrendDirection:
    """
    Detect market trend from price data and EMAs.
    prices: list of recent close prices (newest last)
    """
    if len(prices) < 5:
        return TrendDirection.NEUTRAL

    current = prices[-1]

    # EMA crossover is primary signal
    if ema_short > ema_long * 1.002 and current > ema_short:
        return TrendDirection.BULLISH
    elif ema_short < ema_long * 0.998 and current < ema_short:
        return TrendDirection.BEARISH

    # Check recent price action (last 5 bars)
    recent = prices[-5:]
    if all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)):
        return TrendDirection.BULLISH
    if all(recent[i] >= recent[i + 1] for i in range(len(recent) - 1)):
        return TrendDirection.BEARISH

    return TrendDirection.NEUTRAL


# ═══════════════════════════════════════════════════════════════
# Strike Selection
# ═══════════════════════════════════════════════════════════════

def round_to_strike(price: float, increment: float = 1.0) -> float:
    """Round a price to the nearest valid strike price."""
    return round(price / increment) * increment


def select_iron_condor_strikes(
    current_price: float,
    vix: float,
    dte: int,
    spread_width: float = 2.0,
    strike_increment: float = 1.0,
) -> dict:
    """
    Select iron condor strikes based on current price and volatility.

    Places short strikes at ~1 standard deviation away (delta ~0.16).
    The higher the VIX, the wider the wings — more premium, more room.

    Returns dict with put_buy, put_sell, call_sell, call_buy strikes.
    """
    # Annualized vol → daily vol → move over DTE period
    # For 0DTE, use 0.5 days (half a trading day remaining on average)
    daily_vol = (vix / 100) / math.sqrt(252)
    time_factor = max(dte, 0.5)  # never zero — 0DTE still has intraday move
    expected_move = current_price * daily_vol * math.sqrt(time_factor)

    # Short strikes at ~1 standard deviation (adjustable)
    # Higher VIX = we can go wider and still collect decent premium
    if vix > 25:
        sd_multiplier = 1.2  # wider in high vol
    elif vix > 20:
        sd_multiplier = 1.0  # standard
    else:
        sd_multiplier = 0.8  # tighter in low vol (but we shouldn't be doing IC in low vol)

    offset = expected_move * sd_multiplier

    put_sell = round_to_strike(current_price - offset, strike_increment)
    put_buy = round_to_strike(put_sell - spread_width, strike_increment)
    call_sell = round_to_strike(current_price + offset, strike_increment)
    call_buy = round_to_strike(call_sell + spread_width, strike_increment)

    # Sanity: short strikes must be OTM
    if put_sell >= current_price:
        put_sell = round_to_strike(current_price - strike_increment, strike_increment)
        put_buy = put_sell - spread_width
    if call_sell <= current_price:
        call_sell = round_to_strike(current_price + strike_increment, strike_increment)
        call_buy = call_sell + spread_width

    return {
        "put_buy": put_buy,
        "put_sell": put_sell,
        "call_sell": call_sell,
        "call_buy": call_buy,
    }


def select_credit_spread_strikes(
    current_price: float,
    vix: float,
    dte: int,
    direction: TrendDirection,
    spread_width: float = 2.0,
    strike_increment: float = 1.0,
) -> dict:
    """
    Select credit spread strikes.

    Bullish → Bull Put Spread (sell put, buy lower put)
    Bearish → Bear Call Spread (sell call, buy higher call)
    """
    daily_vol = (vix / 100) / math.sqrt(252)
    time_factor = max(dte, 0.5)
    expected_move = current_price * daily_vol * math.sqrt(time_factor)

    # Place short strike at ~0.7-1.0 SD away from current price
    offset = expected_move * 0.85

    if direction == TrendDirection.BULLISH:
        # Bull put spread — short put below market
        short_strike = round_to_strike(current_price - offset, strike_increment)
        long_strike = round_to_strike(short_strike - spread_width, strike_increment)
        if short_strike >= current_price:
            short_strike = round_to_strike(current_price - spread_width, strike_increment)
            long_strike = short_strike - spread_width
        return {
            "type": "bull_put_spread",
            "short_put": short_strike,
            "long_put": long_strike,
        }
    else:
        # Bear call spread — short call above market
        short_strike = round_to_strike(current_price + offset, strike_increment)
        long_strike = round_to_strike(short_strike + spread_width, strike_increment)
        if short_strike <= current_price:
            short_strike = round_to_strike(current_price + spread_width, strike_increment)
            long_strike = short_strike + spread_width
        return {
            "type": "bear_call_spread",
            "short_call": short_strike,
            "long_call": long_strike,
        }


def select_wheel_strike(
    current_price: float,
    vix: float,
    dte: int,
    strike_increment: float = 0.5,
) -> float:
    """
    Select strike for cash-secured put (Wheel strategy).
    Sell put slightly OTM — want to get assigned at a discount.
    """
    daily_vol = (vix / 100) / math.sqrt(252)
    time_factor = max(dte, 0.5)
    expected_move = current_price * daily_vol * math.sqrt(time_factor)

    # Put strike ~0.5 SD below current price (higher probability of profit)
    target = current_price - (expected_move * 0.5)
    return round_to_strike(target, strike_increment)


# ═══════════════════════════════════════════════════════════════
# Strategy Builders
# ═══════════════════════════════════════════════════════════════

def build_iron_condor(
    underlying: str,
    current_price: float,
    vix: float,
    dte: int,
    expiration: str,
    max_capital: float,
    spread_width: float = 2.0,
    strike_increment: float = 1.0,
) -> Optional[OptionsTradeSetup]:
    """
    Build a complete iron condor trade setup.

    Returns None if the setup doesn't meet minimum criteria.
    """
    strikes = select_iron_condor_strikes(
        current_price, vix, dte, spread_width, strike_increment
    )

    # Max loss per contract = spread_width - net premium (estimated)
    # We estimate premium as ~30-40% of spread width in high vol
    est_premium_pct = 0.35 if vix > 20 else 0.25
    est_net_premium = spread_width * est_premium_pct * 100  # in dollars
    max_loss_per_contract = (spread_width * 100) - est_net_premium

    # How many contracts can we afford?
    contracts = max(1, int(max_capital * 0.25 / max_loss_per_contract))  # risk 25% max

    # Probability estimate (rough — based on strikes being ~1 SD away)
    prob_profit = 0.68 if vix > 20 else 0.60  # ~1 SD = 68% for IC

    setup = OptionsTradeSetup(
        strategy=StrategyType.IRON_CONDOR,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="buy", option_type="put",
                     strike=strikes["put_buy"], expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="put",
                     strike=strikes["put_sell"], expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="call",
                     strike=strikes["call_sell"], expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="call",
                     strike=strikes["call_buy"], expiration=expiration),
        ],
        max_profit=est_net_premium,
        max_loss=max_loss_per_contract,
        breakeven_low=strikes["put_sell"] - (est_net_premium / 100),
        breakeven_high=strikes["call_sell"] + (est_net_premium / 100),
        probability_of_profit=prob_profit,
        risk_reward_ratio=est_net_premium / max_loss_per_contract if max_loss_per_contract > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        reason=f"Iron Condor on {underlying} | VIX={vix:.1f} | "
               f"Range: ${strikes['put_sell']:.0f}-${strikes['call_sell']:.0f} | "
               f"{dte}DTE",
    )

    # Score: higher is better (good premium, high probability, decent R:R)
    setup.score = (
        prob_profit * 0.4 +
        min(setup.risk_reward_ratio, 1.0) * 0.3 +
        (0.3 if vix > 20 else 0.1)  # bonus for high vol environment
    )

    # Minimum credit filter: need at least 20% of spread width as credit
    # Otherwise risk/reward is unacceptable
    min_credit = spread_width * 0.20 * 100  # 20% of wing width
    if setup.max_profit < min_credit:
        return None  # not enough premium to justify the risk

    return setup


def build_credit_spread(
    underlying: str,
    current_price: float,
    vix: float,
    dte: int,
    expiration: str,
    direction: TrendDirection,
    max_capital: float,
    spread_width: float = 2.0,
    strike_increment: float = 1.0,
) -> Optional[OptionsTradeSetup]:
    """Build a credit spread (bull put or bear call)."""
    strikes = select_credit_spread_strikes(
        current_price, vix, dte, direction, spread_width, strike_increment
    )

    est_premium_pct = 0.30 if vix > 18 else 0.20
    est_net_premium = spread_width * est_premium_pct * 100
    max_loss_per_contract = (spread_width * 100) - est_net_premium

    contracts = max(1, int(max_capital * 0.30 / max_loss_per_contract))

    if strikes["type"] == "bull_put_spread":
        strategy = StrategyType.BULL_PUT_SPREAD
        short_strike = strikes["short_put"]
        long_strike = strikes["long_put"]
        legs = [
            OptionLeg(symbol="", side="sell", option_type="put",
                     strike=short_strike, expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="put",
                     strike=long_strike, expiration=expiration),
        ]
        breakeven = short_strike - (est_net_premium / 100)
        reason = f"Bull Put Spread on {underlying} | Bullish bias | {short_strike}/{long_strike}P | {dte}DTE"
    else:
        strategy = StrategyType.BEAR_CALL_SPREAD
        short_strike = strikes["short_call"]
        long_strike = strikes["long_call"]
        legs = [
            OptionLeg(symbol="", side="sell", option_type="call",
                     strike=short_strike, expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="call",
                     strike=long_strike, expiration=expiration),
        ]
        breakeven = short_strike + (est_net_premium / 100)
        reason = f"Bear Call Spread on {underlying} | Bearish bias | {short_strike}/{long_strike}C | {dte}DTE"

    prob_profit = 0.65 if vix > 18 else 0.55

    setup = OptionsTradeSetup(
        strategy=strategy,
        underlying=underlying,
        legs=legs,
        max_profit=est_net_premium,
        max_loss=max_loss_per_contract,
        breakeven_low=breakeven if strategy == StrategyType.BULL_PUT_SPREAD else 0,
        breakeven_high=breakeven if strategy == StrategyType.BEAR_CALL_SPREAD else 0,
        probability_of_profit=prob_profit,
        risk_reward_ratio=est_net_premium / max_loss_per_contract if max_loss_per_contract > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        reason=reason,
    )

    setup.score = (
        prob_profit * 0.35 +
        min(setup.risk_reward_ratio, 1.0) * 0.35 +
        (0.3 if direction != TrendDirection.NEUTRAL else 0.1)
    )

    return setup


def build_wheel_csp(
    underlying: str,
    current_price: float,
    vix: float,
    dte: int,
    expiration: str,
    max_capital: float,
    strike_increment: float = 0.5,
) -> Optional[OptionsTradeSetup]:
    """Build a cash-secured put for the Wheel strategy."""
    strike = select_wheel_strike(current_price, vix, dte, strike_increment)

    # Need enough cash to cover assignment
    collateral = strike * 100  # 100 shares per contract
    if collateral > max_capital:
        return None  # can't afford it

    # Estimate premium (~2-4% of strike in normal vol)
    premium_pct = 0.03 if vix > 15 else 0.02
    est_premium = strike * premium_pct * 100

    contracts = 1  # Wheel is typically 1 contract at a time for small accounts

    setup = OptionsTradeSetup(
        strategy=StrategyType.CASH_SECURED_PUT,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="sell", option_type="put",
                     strike=strike, expiration=expiration),
        ],
        max_profit=est_premium,
        max_loss=collateral - est_premium,  # assigned at strike minus premium
        breakeven_low=strike - (est_premium / 100),
        probability_of_profit=0.70,  # typically high for slightly OTM puts
        risk_reward_ratio=est_premium / (collateral - est_premium) if collateral > est_premium else 0,
        target_dte=dte,
        contracts=contracts,
        reason=f"Wheel CSP on {underlying} | Strike ${strike:.2f} | "
               f"Collateral ${collateral:.0f} | {dte}DTE",
    )

    setup.score = 0.5  # Wheel is always moderate — safe but slow

    return setup


# ═══════════════════════════════════════════════════════════════
# Strategy Selector — The Brain
# ═══════════════════════════════════════════════════════════════

def select_strategy(
    market_data: dict,
    max_capital: float,
    existing_positions: list = None,
) -> list:
    """
    Master strategy selector. Analyzes market conditions and returns
    ranked list of trade setups to consider.

    market_data should contain:
        - spy_price: float (current SPY price)
        - vix: float (current VIX level)
        - ema_20: float (20-period EMA of SPY)
        - ema_50: float (50-period EMA of SPY)
        - recent_prices: list[float] (last 20+ closes)
        - available_expirations: list[str] (available option expiration dates)
        - wheel_candidates: list[dict] (stocks for wheel: {symbol, price})

    Returns list of OptionsTradeSetup, sorted by score (best first).
    """
    existing_positions = existing_positions or []
    setups = []

    spy_price = market_data["spy_price"]
    vix = market_data["vix"]
    regime = classify_market_regime(vix)
    trend = detect_trend(
        market_data["recent_prices"],
        market_data["ema_20"],
        market_data["ema_50"],
    )
    expirations = market_data.get("available_expirations", [])

    # ── High Volatility: Iron Condors are king ──
    if regime == MarketRegime.HIGH_VOL:
        # 0-3 DTE iron condors on SPY — use $5 wide wings for better premium
        # Skip 0DTE after 11 AM ET — not enough theta left to sell
        now_et = datetime.now()
        for exp in expirations:
            dte = _days_to_expiration(exp)
            if dte == 0 and now_et.hour >= 11:
                continue  # too late for 0DTE
            if 0 <= dte <= 3:
                ic = build_iron_condor(
                    "SPY", spy_price, vix, dte, exp,
                    max_capital, spread_width=5.0, strike_increment=1.0,
                )
                if ic:
                    setups.append(ic)

        # Also consider directional credit spreads if there's a trend
        if trend != TrendDirection.NEUTRAL:
            for exp in expirations:
                dte = _days_to_expiration(exp)
                if 5 <= dte <= 14:
                    cs = build_credit_spread(
                        "SPY", spy_price, vix, dte, exp, trend,
                        max_capital, spread_width=5.0, strike_increment=1.0,
                    )
                    if cs:
                        cs.score *= 0.8  # slightly penalize vs IC in high vol
                        setups.append(cs)

    # ── Medium Volatility: Credit Spreads with trend ──
    elif regime == MarketRegime.MEDIUM_VOL:
        # Prefer directional credit spreads
        for exp in expirations:
            dte = _days_to_expiration(exp)
            if 3 <= dte <= 14:
                if trend != TrendDirection.NEUTRAL:
                    cs = build_credit_spread(
                        "SPY", spy_price, vix, dte, exp, trend,
                        max_capital, spread_width=5.0, strike_increment=1.0,
                    )
                    if cs:
                        setups.append(cs)
                else:
                    # No clear trend — small iron condor
                    ic = build_iron_condor(
                        "SPY", spy_price, vix, dte, exp,
                        max_capital, spread_width=3.0, strike_increment=1.0,
                    )
                    if ic:
                        ic.score *= 0.7  # not ideal conditions for IC
                        setups.append(ic)

    # ── Low Volatility: Wheel on cheap stocks ──
    if regime == MarketRegime.LOW_VOL or not setups:
        wheel_stocks = market_data.get("wheel_candidates", [])
        for stock in wheel_stocks:
            # Skip if we already have a position in this stock
            if any(p.get("symbol") == stock["symbol"] for p in existing_positions):
                continue
            for exp in expirations:
                dte = _days_to_expiration(exp)
                if 14 <= dte <= 45:  # Wheel uses longer DTE
                    csp = build_wheel_csp(
                        stock["symbol"], stock["price"], vix, dte, exp,
                        max_capital, strike_increment=0.5,
                    )
                    if csp:
                        setups.append(csp)

    # Sort by score (best first) and return top candidates
    setups.sort(key=lambda s: s.score, reverse=True)
    return setups[:5]  # return top 5 setups


def _days_to_expiration(exp_date_str: str) -> int:
    """Calculate days to expiration from date string."""
    try:
        exp = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        return (exp - today).days
    except ValueError:
        return -1


# ═══════════════════════════════════════════════════════════════
# Position Management Rules
# ═══════════════════════════════════════════════════════════════

def should_close_position(
    position: dict,
    current_pnl_pct: float,
    dte_remaining: int,
    vix_current: float,
) -> tuple:
    """
    Determine if an options position should be closed.

    Returns (should_close: bool, reason: str)

    Rules:
    1. Take profit at 50% of max profit (standard for premium selling)
    2. Cut loss at 2x premium received (max loss management)
    3. Close at 1 DTE to avoid assignment risk (except wheel)
    4. Close if VIX spikes >30% from entry (volatility expansion danger)
    """
    strategy = position.get("strategy", "")

    # Rule 1: Take profit at 50%
    if current_pnl_pct >= 0.50:
        return True, f"Take profit: {current_pnl_pct:.0%} of max profit reached"

    # Rule 2: Cut loss at 200% of premium (lose 2x what you collected)
    if current_pnl_pct <= -2.0:
        return True, f"Stop loss: losing {abs(current_pnl_pct):.0%} of premium collected"

    # Rule 3: Close before expiration (avoid assignment)
    if strategy != StrategyType.CASH_SECURED_PUT.value:
        if dte_remaining <= 1:
            return True, f"Closing: {dte_remaining} DTE remaining, avoiding assignment risk"

    # Rule 4: Volatility spike
    entry_vix = position.get("entry_vix", vix_current)
    if vix_current > entry_vix * 1.30:
        return True, f"Vol spike: VIX rose from {entry_vix:.1f} to {vix_current:.1f} (+{((vix_current/entry_vix)-1)*100:.0f}%)"

    return False, ""


def calculate_position_risk(setup: OptionsTradeSetup, account_equity: float) -> dict:
    """
    Calculate risk metrics for a potential trade.

    Returns dict with risk assessment and whether to proceed.
    """
    total_max_loss = setup.max_loss * setup.contracts
    risk_pct = total_max_loss / account_equity if account_equity > 0 else 1.0

    return {
        "total_max_loss": total_max_loss,
        "risk_pct_of_equity": risk_pct,
        "contracts": setup.contracts,
        "approved": risk_pct <= 0.05,  # never risk more than 5% of equity on one trade
        "reason": (
            f"Risk: ${total_max_loss:.0f} ({risk_pct:.1%} of equity) — "
            f"{'APPROVED' if risk_pct <= 0.05 else 'REJECTED: exceeds 5% max risk'}"
        ),
    }


# ═══════════════════════════════════════════════════════════════
# Formatting Helpers
# ═══════════════════════════════════════════════════════════════

def format_setup_summary(setup: OptionsTradeSetup) -> str:
    """Human-readable summary of a trade setup."""
    lines = [
        f"{'═' * 45}",
        f"📋 {setup.strategy.value.upper().replace('_', ' ')}",
        f"{'═' * 45}",
        f"Underlying: {setup.underlying}",
        f"Contracts: {setup.contracts}",
    ]

    for leg in setup.legs:
        side_emoji = "🔴" if leg.side == "sell" else "🟢"
        lines.append(
            f"  {side_emoji} {leg.side.upper()} {leg.option_type.upper()} "
            f"${leg.strike:.0f} exp {leg.expiration}"
        )

    lines.extend([
        f"Max Profit: ${setup.max_profit:.0f}/contract (${setup.max_profit * setup.contracts:.0f} total)",
        f"Max Loss:   ${setup.max_loss:.0f}/contract (${setup.max_loss * setup.contracts:.0f} total)",
        f"Prob of Profit: ~{setup.probability_of_profit:.0%}",
        f"Risk/Reward: 1:{setup.risk_reward_ratio:.2f}",
        f"Score: {setup.score:.2f}",
        f"Reason: {setup.reason}",
    ])

    return "\n".join(lines)
