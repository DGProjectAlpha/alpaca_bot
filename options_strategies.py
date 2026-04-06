"""
Options Strategy Engine — Multi-Ticker, Multi-Strategy

Seven strategies scored and ranked across a diverse ticker universe:
  1. Iron Condors       — VIX >20, range-bound, ETFs only, 0-7 DTE
  2. Credit Spreads     — trend detected, ETFs + stocks, 5-21 DTE
  3. Wheel (CSP)        — low VIX (<15), cheap stocks, 14-45 DTE
  4. Momentum Calls/Puts— breakout/breakdown via EMA + RSI, 7-30 DTE
  5. Calendar Spreads   — steep IV term structure, any ticker
  6. Butterfly Spreads  — low-vol pinning bets, ETFs + stocks near round numbers
  7. Earnings Strangles — sell high IV rank (>70%) before announcements

The select_strategy() function scans ALL tickers, checks which strategies
apply to each, scores every viable setup, and returns the top 5 for review.
"""
import math
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import config


# ═══════════════════════════════════════════════════════════════
# Enums and Data Classes
# ═══════════════════════════════════════════════════════════════

class StrategyType(Enum):
    IRON_CONDOR = "iron_condor"
    BULL_PUT_SPREAD = "bull_put_spread"
    BEAR_CALL_SPREAD = "bear_call_spread"
    BULL_CALL_SPREAD = "bull_call_spread"      # Debit spread — buy lower call, sell higher
    BEAR_PUT_SPREAD = "bear_put_spread"        # Debit spread — buy higher put, sell lower
    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    CALENDAR_SPREAD = "calendar_spread"
    BUTTERFLY = "butterfly"
    EARNINGS_STRANGLE = "earnings_strangle"


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
    max_profit: float = 0.0   # per-contract (dollars)
    max_loss: float = 0.0     # per-contract (always positive number)
    breakeven_low: float = 0.0
    breakeven_high: float = 0.0
    probability_of_profit: float = 0.0  # estimated
    risk_reward_ratio: float = 0.0
    target_dte: int = 0       # days to expiration
    reason: str = ""          # why this ticker, this strategy, why now
    score: float = 0.0        # 0-1, higher = better setup
    contracts: int = 1        # how many contracts to trade
    risk_budget: float = 0.0  # max equity % allocated to this trade


@dataclass
class TickerAnalysis:
    """Per-ticker technical analysis used for strategy selection."""
    symbol: str
    price: float
    ema_20: float = 0.0
    ema_50: float = 0.0
    rsi: float = 50.0
    recent_prices: list = field(default_factory=list)
    trend: TrendDirection = TrendDirection.NEUTRAL
    near_round_number: bool = False
    iv_rank: float = 0.0       # 0-1, estimated from VIX proxy


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
    if len(prices) < 5 or ema_short == 0 or ema_long == 0:
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


def compute_rsi(prices: list, period: int = 14) -> float:
    """Compute RSI from a list of close prices."""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def is_near_round_number(price: float, tolerance_pct: float = 0.01) -> bool:
    """Check if price is near a round number (multiples of 5 for stocks, 10 for ETFs)."""
    for increment in [5, 10, 25, 50, 100]:
        nearest = round(price / increment) * increment
        if abs(price - nearest) / price <= tolerance_pct:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# Strike Selection Helpers
# ═══════════════════════════════════════════════════════════════

def round_to_strike(price: float, increment: float = 1.0) -> float:
    """Round a price to the nearest valid strike price."""
    return round(price / increment) * increment


def _expected_move(price: float, vix: float, dte: int) -> float:
    """Calculate expected move based on implied vol and time."""
    daily_vol = (vix / 100) / math.sqrt(252)
    time_factor = max(dte, 0.5)
    return price * daily_vol * math.sqrt(time_factor)


def _strike_increment(price: float) -> float:
    """Pick appropriate strike increment based on price level."""
    if price > 200:
        return 5.0
    elif price > 50:
        return 1.0
    elif price > 20:
        return 0.5
    else:
        return 0.5


# ═══════════════════════════════════════════════════════════════
# Position Sizing
# ═══════════════════════════════════════════════════════════════

def size_contracts(max_loss_per_contract: float, equity: float, risk_pct: float) -> int:
    """
    Determine how many contracts to trade given risk budget.
    For small accounts ($2k), this will almost always be 1 contract.
    That's fine — survival matters more than size.
    """
    if max_loss_per_contract <= 0 or equity <= 0:
        return 1
    max_risk_dollars = equity * risk_pct
    contracts = int(max_risk_dollars / max_loss_per_contract)
    # Hard cap: never more than 3 contracts on a $2k account
    return max(1, min(contracts, 3))


# ═══════════════════════════════════════════════════════════════
# Strategy Builders
# ═══════════════════════════════════════════════════════════════

def build_iron_condor(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    equity: float,
    spread_width: float = None,
    trend: TrendDirection = TrendDirection.NEUTRAL,
) -> Optional[OptionsTradeSetup]:
    """
    Iron Condor: sell OTM put spread + OTM call spread.
    Only when VIX >20 and market is range-bound.
    """
    if spread_width is None:
        spread_width = config.IC_SPREAD_WIDTH
    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)

    # Short strikes at 1.1-1.2 SD — outside expected move but close enough for premium
    sd_mult = 1.3 if vix > 25 else 1.15
    put_offset = move * sd_mult
    call_offset = move * sd_mult

    # Skew strikes away from trend direction — if bullish, push calls wider
    if trend == TrendDirection.BULLISH:
        call_offset = move * (sd_mult + 0.2)  # extra buffer on call side
    elif trend == TrendDirection.BEARISH:
        put_offset = move * (sd_mult + 0.2)   # extra buffer on put side

    put_sell = round_to_strike(price - put_offset, inc)
    put_buy = round_to_strike(put_sell - spread_width, inc)
    call_sell = round_to_strike(price + call_offset, inc)
    call_buy = round_to_strike(call_sell + spread_width, inc)

    # Sanity: short strikes must be OTM
    if put_sell >= price:
        put_sell = round_to_strike(price - inc, inc)
        put_buy = put_sell - spread_width
    if call_sell <= price:
        call_sell = round_to_strike(price + inc, inc)
        call_buy = call_sell + spread_width

    # Estimate premium as ~30-40% of spread width in high vol
    est_prem_pct = 0.35 if vix > 25 else 0.30
    est_premium = spread_width * est_prem_pct * 100
    max_loss = (spread_width * 100) - est_premium

    if est_premium < spread_width * 0.20 * 100:
        return None  # not enough credit

    contracts = size_contracts(max_loss, equity, config.RISK_IRON_CONDOR)

    # Small account guard: if max loss per contract > 10% of equity, skip
    if max_loss > equity * 0.10:
        return None
    prob = 0.68 if vix > 20 else 0.60

    setup = OptionsTradeSetup(
        strategy=StrategyType.IRON_CONDOR,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="buy", option_type="put",
                      strike=put_buy, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="put",
                      strike=put_sell, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="call",
                      strike=call_sell, expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="call",
                      strike=call_buy, expiration=expiration),
        ],
        max_profit=est_premium,
        max_loss=max_loss,
        breakeven_low=put_sell - (est_premium / 100),
        breakeven_high=call_sell + (est_premium / 100),
        probability_of_profit=prob,
        risk_reward_ratio=est_premium / max_loss if max_loss > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_IRON_CONDOR,
        reason=(
            f"Iron Condor on {underlying} | VIX={vix:.1f} (elevated — sell premium) | "
            f"Range: ${put_sell:.0f}-${call_sell:.0f} | {dte}DTE | "
            f"Expected move: +/-${move:.1f}"
        ),
    )
    setup.score = _score_setup(prob, setup.risk_reward_ratio, vix_bonus=(vix > 20))
    # Iron condors are the bread and butter in high vol — give extra boost
    if vix > 22:
        setup.score += 0.10
    return setup


def build_credit_spread(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    direction: TrendDirection,
    equity: float,
    spread_width: float = None,
) -> Optional[OptionsTradeSetup]:
    """
    Credit Spread: bull put or bear call, depending on trend.
    Used when a clear directional bias exists.
    """
    if direction == TrendDirection.NEUTRAL:
        return None
    if spread_width is None:
        spread_width = config.CS_SPREAD_WIDTH
    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)
    offset = move * 0.85

    if direction == TrendDirection.BULLISH:
        short_strike = round_to_strike(price - offset, inc)
        long_strike = round_to_strike(short_strike - spread_width, inc)
        if short_strike >= price:
            short_strike = round_to_strike(price - spread_width, inc)
            long_strike = short_strike - spread_width
        strategy = StrategyType.BULL_PUT_SPREAD
        legs = [
            OptionLeg(symbol="", side="sell", option_type="put",
                      strike=short_strike, expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="put",
                      strike=long_strike, expiration=expiration),
        ]
        label = f"Bull Put Spread on {underlying} | Bullish trend | {short_strike}/{long_strike}P"
    else:
        short_strike = round_to_strike(price + offset, inc)
        long_strike = round_to_strike(short_strike + spread_width, inc)
        if short_strike <= price:
            short_strike = round_to_strike(price + spread_width, inc)
            long_strike = short_strike + spread_width
        strategy = StrategyType.BEAR_CALL_SPREAD
        legs = [
            OptionLeg(symbol="", side="sell", option_type="call",
                      strike=short_strike, expiration=expiration),
            OptionLeg(symbol="", side="buy", option_type="call",
                      strike=long_strike, expiration=expiration),
        ]
        label = f"Bear Call Spread on {underlying} | Bearish trend | {short_strike}/{long_strike}C"

    est_prem_pct = 0.30 if vix > 18 else 0.20
    est_premium = spread_width * est_prem_pct * 100
    max_loss = (spread_width * 100) - est_premium

    # Small account guard
    if max_loss > equity * 0.10:
        return None

    contracts = size_contracts(max_loss, equity, config.RISK_CREDIT_SPREAD)
    prob = 0.65 if vix > 18 else 0.55

    setup = OptionsTradeSetup(
        strategy=strategy,
        underlying=underlying,
        legs=legs,
        max_profit=est_premium,
        max_loss=max_loss,
        breakeven_low=short_strike - (est_premium / 100) if strategy == StrategyType.BULL_PUT_SPREAD else 0,
        breakeven_high=short_strike + (est_premium / 100) if strategy == StrategyType.BEAR_CALL_SPREAD else 0,
        probability_of_profit=prob,
        risk_reward_ratio=est_premium / max_loss if max_loss > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_CREDIT_SPREAD,
        reason=f"{label} | {dte}DTE | VIX={vix:.1f}",
    )
    setup.score = _score_setup(prob, setup.risk_reward_ratio, trend_bonus=True)
    return setup


def build_debit_spread(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    direction: TrendDirection,
    equity: float,
    spread_width: float = None,
) -> Optional[OptionsTradeSetup]:
    """
    Debit Spread: buy-to-open directional spread with defined risk.
    Bull Call Spread (bullish): buy lower call, sell higher call.
    Bear Put Spread (bearish): buy higher put, sell lower put.

    Unlike credit spreads (which profit from time decay), debit spreads
    profit from MOVEMENT. Great in trending, volatile markets where
    iron condors get blown up.

    Cost = net debit paid. Max profit = spread width - debit. Max loss = debit.
    """
    if direction == TrendDirection.NEUTRAL:
        return None
    if spread_width is None:
        spread_width = config.CS_SPREAD_WIDTH  # reuse credit spread width setting

    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)

    if direction == TrendDirection.BULLISH:
        # Buy ATM or slightly ITM call, sell OTM call
        long_strike = round_to_strike(price - (move * 0.1), inc)  # slightly ITM
        short_strike = round_to_strike(long_strike + spread_width, inc)
        strategy = StrategyType.BULL_CALL_SPREAD
        legs = [
            OptionLeg(symbol="", side="buy", option_type="call",
                      strike=long_strike, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="call",
                      strike=short_strike, expiration=expiration),
        ]
        label = f"Bull Call Spread on {underlying} | Bullish | {long_strike}/{short_strike}C"
    else:
        # Buy ATM or slightly ITM put, sell OTM put
        long_strike = round_to_strike(price + (move * 0.1), inc)  # slightly ITM
        short_strike = round_to_strike(long_strike - spread_width, inc)
        strategy = StrategyType.BEAR_PUT_SPREAD
        legs = [
            OptionLeg(symbol="", side="buy", option_type="put",
                      strike=long_strike, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="put",
                      strike=short_strike, expiration=expiration),
        ]
        label = f"Bear Put Spread on {underlying} | Bearish | {long_strike}/{short_strike}P"

    # Estimate debit as ~60-70% of spread width (ITM component)
    est_debit_pct = 0.65 if vix > 20 else 0.60
    est_debit = spread_width * est_debit_pct * 100  # cost per contract
    max_profit = (spread_width * 100) - est_debit
    max_loss = est_debit

    # Small account guard
    if max_loss > equity * 0.05:
        return None

    contracts = size_contracts(max_loss, equity, config.RISK_CREDIT_SPREAD)

    # PoP for debit spreads: ~45-55% depending on how close to ATM
    prob = 0.50 if vix > 20 else 0.45
    rr = max_profit / max_loss if max_loss > 0 else 0

    setup = OptionsTradeSetup(
        strategy=strategy,
        underlying=underlying,
        legs=legs,
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_low=long_strike + (est_debit / 100) if direction == TrendDirection.BULLISH else 0,
        breakeven_high=long_strike - (est_debit / 100) if direction == TrendDirection.BEARISH else 0,
        probability_of_profit=prob,
        risk_reward_ratio=rr,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_CREDIT_SPREAD,
        reason=f"{label} | {dte}DTE | VIX={vix:.1f} | Debit ~${est_debit:.0f}",
    )
    # Debit spreads score: moderate PoP, decent R:R, big trend bonus
    setup.score = _score_setup(prob, rr, vix_bonus=(vix > 20), trend_bonus=True)
    # Debit spreads LOVE high vol + trend — opposite of iron condors
    if vix > 20 and direction != TrendDirection.NEUTRAL:
        setup.score += 0.08
    return setup


def build_wheel_csp(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    equity: float,
) -> Optional[OptionsTradeSetup]:
    """
    Cash-Secured Put for the Wheel strategy.
    Low VIX, cheap stocks you'd be happy to own at a discount.
    """
    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)
    strike = round_to_strike(price - (move * 0.5), inc)

    collateral = strike * 100
    # For a $2k account, collateral must fit within cash portion
    # Max 50% of total equity in any single wheel position
    if collateral > equity * 0.50:
        return None
    if collateral > config.CASH_PORTION:
        return None  # wheel CSPs use cash, not margin

    premium_pct = 0.03 if vix > 15 else 0.02
    est_premium = strike * premium_pct * 100
    contracts = 1  # wheel is typically 1 contract for small accounts

    setup = OptionsTradeSetup(
        strategy=StrategyType.CASH_SECURED_PUT,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="sell", option_type="put",
                      strike=strike, expiration=expiration),
        ],
        max_profit=est_premium,
        max_loss=collateral - est_premium,
        breakeven_low=strike - (est_premium / 100),
        probability_of_profit=0.70,
        risk_reward_ratio=est_premium / (collateral - est_premium) if collateral > est_premium else 0,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_WHEEL_CSP,
        reason=(
            f"Wheel CSP on {underlying} | VIX={vix:.1f} (low — wheel territory) | "
            f"Strike ${strike:.2f} | Collateral ${collateral:.0f} | "
            f"{dte}DTE | Would own at ${strike:.2f} if assigned"
        ),
    )
    setup.score = _score_setup(0.75, setup.risk_reward_ratio)  # wheel: high PoP, modest R:R
    return setup


def build_momentum_trade(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    direction: TrendDirection,
    rsi: float,
    equity: float,
) -> Optional[OptionsTradeSetup]:
    """
    Momentum long call or long put.
    Buy calls on strong breakouts (RSI rising, EMA cross up).
    Buy puts on breakdowns (RSI falling, EMA cross down).
    Max 2% of equity — these are speculative.
    """
    if direction == TrendDirection.NEUTRAL:
        return None

    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)

    if direction == TrendDirection.BULLISH:
        if rsi < 40 or rsi > 80:
            return None  # want RSI in 50-75 range for momentum (not exhausted)
        # Buy slightly OTM call
        strike = round_to_strike(price + (move * 0.3), inc)
        strategy = StrategyType.LONG_CALL
        option_type = "call"
        label = f"Momentum CALL on {underlying} | Bullish breakout | RSI={rsi:.0f}"
    else:
        if rsi > 60 or rsi < 20:
            return None
        strike = round_to_strike(price - (move * 0.3), inc)
        strategy = StrategyType.LONG_PUT
        option_type = "put"
        label = f"Momentum PUT on {underlying} | Bearish breakdown | RSI={rsi:.0f}"

    # Estimate option price ~5-8% of underlying for slightly OTM
    est_cost = price * 0.06 * 100  # per contract

    # Small account: skip if a single contract costs more than 5% of equity
    if est_cost > equity * 0.05:
        return None

    max_risk = equity * config.RISK_MOMENTUM
    contracts = max(1, int(max_risk / est_cost)) if est_cost > 0 else 1

    setup = OptionsTradeSetup(
        strategy=strategy,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="buy", option_type=option_type,
                      strike=strike, expiration=expiration),
        ],
        max_profit=est_cost * 2,  # target 100% gain
        max_loss=est_cost,         # can lose entire premium
        breakeven_low=strike - (est_cost / 100) if option_type == "put" else 0,
        breakeven_high=strike + (est_cost / 100) if option_type == "call" else 0,
        probability_of_profit=0.40,  # speculative — lower prob but higher payoff
        risk_reward_ratio=2.0,       # targeting 2:1
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_MOMENTUM,
        reason=f"{label} | ${strike:.0f} strike | {dte}DTE | Cost ~${est_cost:.0f}/contract",
    )
    # Momentum is speculative (40% PoP, 2:1 R:R) — score reflects that honestly
    setup.score = _score_setup(0.40, 2.0, vix_bonus=False, trend_bonus=True)
    # High vol: options cost more BUT moves are bigger. Mild penalty only.
    # The old 40% penalty killed momentum in exactly the conditions it works best.
    if vix > 25:
        setup.score *= 0.85  # very high vol = premiums too expensive, mild penalty
    elif vix > 20:
        setup.score *= 0.95  # moderate-high vol = slightly more expensive, barely penalize
    # Low vol bonus: cheap premiums, but moves are smaller — wash
    return setup


def build_calendar_spread(
    underlying: str,
    price: float,
    vix: float,
    near_dte: int,
    far_dte: int,
    near_exp: str,
    far_exp: str,
    equity: float,
) -> Optional[OptionsTradeSetup]:
    """
    Calendar Spread: sell near-term option, buy same-strike further out.
    Profits from time decay differential (near decays faster).
    Best when IV term structure is steep (near IV >> far IV).
    """
    inc = _strike_increment(price)
    # ATM strike for max theta differential
    strike = round_to_strike(price, inc)

    # Use puts if price is slightly above strike, calls if below (stay neutral)
    option_type = "call" if price >= strike else "put"

    # Estimate: near-term option worth less, far-term worth more
    near_cost = price * 0.03 * 100  # rough near-term premium
    far_cost = price * 0.05 * 100   # rough far-term premium
    net_debit = far_cost - near_cost  # what we pay

    if net_debit <= 0:
        return None

    max_loss = net_debit  # can lose entire debit
    max_profit = net_debit * 1.5  # rough target — profit if near expires worthless
    contracts = size_contracts(max_loss, equity, config.RISK_CALENDAR)

    setup = OptionsTradeSetup(
        strategy=StrategyType.CALENDAR_SPREAD,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="sell", option_type=option_type,
                      strike=strike, expiration=near_exp),
            OptionLeg(symbol="", side="buy", option_type=option_type,
                      strike=strike, expiration=far_exp),
        ],
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_low=strike - (net_debit / 100),
        breakeven_high=strike + (net_debit / 100),
        probability_of_profit=0.55,
        risk_reward_ratio=max_profit / max_loss if max_loss > 0 else 0,
        target_dte=near_dte,
        contracts=contracts,
        risk_budget=config.RISK_CALENDAR,
        reason=(
            f"Calendar Spread on {underlying} | ${strike:.0f} strike | "
            f"Sell {near_dte}DTE / Buy {far_dte}DTE | "
            f"Theta decay play — near expires first"
        ),
    )
    setup.score = _score_setup(0.55, setup.risk_reward_ratio, vix_bonus=False)
    return setup


def build_butterfly(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    equity: float,
    spread_width: float = None,
) -> Optional[OptionsTradeSetup]:
    """
    Butterfly Spread: cheap defined-risk bet that price pins near a target.
    Buy 1 lower, sell 2 middle, buy 1 upper.
    Best in low-vol near round numbers.
    """
    if spread_width is None:
        spread_width = config.BF_SPREAD_WIDTH
    inc = _strike_increment(price)

    # Center on nearest round number
    center = round_to_strike(price, inc)
    lower = center - spread_width
    upper = center + spread_width

    # Use calls (doesn't matter much for butterfly payoff)
    est_debit = spread_width * 0.20 * 100  # butterflies are cheap
    max_profit = (spread_width * 100) - est_debit
    max_loss = est_debit

    if max_loss <= 0:
        return None

    contracts = size_contracts(max_loss, equity, config.RISK_BUTTERFLY)

    setup = OptionsTradeSetup(
        strategy=StrategyType.BUTTERFLY,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="buy", option_type="call",
                      strike=lower, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="call",
                      strike=center, expiration=expiration, quantity=2),
            OptionLeg(symbol="", side="buy", option_type="call",
                      strike=upper, expiration=expiration),
        ],
        max_profit=max_profit,
        max_loss=max_loss,
        breakeven_low=lower + (est_debit / 100),
        breakeven_high=upper - (est_debit / 100),
        probability_of_profit=0.30,  # low prob, high payoff
        risk_reward_ratio=max_profit / max_loss if max_loss > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_BUTTERFLY,
        reason=(
            f"Butterfly on {underlying} | Center ${center:.0f} (near round number) | "
            f"Low vol pinning bet | {dte}DTE | "
            f"Cheap entry: ~${est_debit:.0f}/contract"
        ),
    )
    setup.score = _score_setup(0.35, setup.risk_reward_ratio, vix_bonus=(vix < 18))
    if is_near_round_number(price):
        setup.score += 0.05  # slight bonus for round number pinning
    return setup


def build_earnings_strangle(
    underlying: str,
    price: float,
    vix: float,
    dte: int,
    expiration: str,
    iv_rank: float,
    equity: float,
) -> Optional[OptionsTradeSetup]:
    """
    Sell strangle before earnings when IV rank is high (>70%).
    Sell OTM put + OTM call. Profit from IV crush after announcement.
    MUST close before actual earnings — only capture the IV deflation.
    """
    if iv_rank < config.EARN_MIN_IV_RANK:
        return None

    inc = _strike_increment(price)
    move = _expected_move(price, vix, dte)

    # Place short strikes at ~1.2 SD (wider to survive any pre-earnings drift)
    offset = move * 1.2
    put_strike = round_to_strike(price - offset, inc)
    call_strike = round_to_strike(price + offset, inc)

    if put_strike >= price or call_strike <= price:
        return None

    # High IV → fat premiums
    est_put_prem = price * 0.025 * 100
    est_call_prem = price * 0.025 * 100
    total_premium = est_put_prem + est_call_prem

    # Undefined risk on paper, but we cap it mentally
    # Max loss estimate: 3x premium (worst case before we'd close)
    est_max_loss = total_premium * 3
    contracts = size_contracts(est_max_loss, equity, config.RISK_EARNINGS)

    setup = OptionsTradeSetup(
        strategy=StrategyType.EARNINGS_STRANGLE,
        underlying=underlying,
        legs=[
            OptionLeg(symbol="", side="sell", option_type="put",
                      strike=put_strike, expiration=expiration),
            OptionLeg(symbol="", side="sell", option_type="call",
                      strike=call_strike, expiration=expiration),
        ],
        max_profit=total_premium,
        max_loss=est_max_loss,
        breakeven_low=put_strike - (total_premium / 100),
        breakeven_high=call_strike + (total_premium / 100),
        probability_of_profit=0.60,
        risk_reward_ratio=total_premium / est_max_loss if est_max_loss > 0 else 0,
        target_dte=dte,
        contracts=contracts,
        risk_budget=config.RISK_EARNINGS,
        reason=(
            f"Earnings Strangle on {underlying} | IV Rank={iv_rank:.0%} (elevated) | "
            f"Sell ${put_strike:.0f}P / ${call_strike:.0f}C | {dte}DTE | "
            f"CLOSE BEFORE ANNOUNCEMENT — capture IV crush only"
        ),
    )
    setup.score = _score_setup(0.60, setup.risk_reward_ratio, vix_bonus=(vix > 18))
    if iv_rank > 0.80:
        setup.score += 0.05  # extra high IV = better crush potential
    return setup


# ═══════════════════════════════════════════════════════════════
# Scoring Helper
# ═══════════════════════════════════════════════════════════════

def _score_setup(
    prob: float,
    rr_ratio: float,
    vix_bonus: bool = False,
    trend_bonus: bool = False,
) -> float:
    """
    Risk-adjusted scoring. Prioritizes SAFETY over raw profit.

    - Probability of profit is king (50% weight)
    - Risk/reward ratio matters but capped so it doesn't reward garbage R:R (20%)
    - Expected value per dollar risked is the tiebreaker (15%)
    - Condition bonuses are minor (15% max)

    Hard floors:
    - PoP < 55% → score capped at 0.30 (will never pass minimum threshold)
    - R:R < 0.3 → score halved (risking $3 to make $1 is not worth it)
    """
    # Expected value: (prob × reward) - ((1-prob) × risk), normalized
    ev_per_dollar = prob * rr_ratio - (1 - prob) * 1.0
    ev_score = max(ev_per_dollar, 0.0)  # floor at 0

    score = (
        prob * 0.50 +                           # probability is king
        min(rr_ratio, 1.5) / 1.5 * 0.20 +      # R:R capped at 1.5
        min(ev_score, 0.5) * 0.15 +             # EV tiebreaker
        (0.10 if vix_bonus else 0.0) +
        (0.05 if trend_bonus else 0.0)
    )

    # Hard penalties
    if prob < 0.55:
        score = min(score, 0.30)  # low PoP = hard cap
    if rr_ratio < 0.3:
        score *= 0.50             # terrible R:R = halved

    return min(score, 1.0)


# ═══════════════════════════════════════════════════════════════
# Strategy Selector — The Brain
# ═══════════════════════════════════════════════════════════════

def select_strategy(
    market_data: dict,
    equity: float,
    existing_positions: list = None,
) -> list:
    """
    Master strategy selector. Scans ALL tickers across ALL strategies,
    scores every viable setup, and returns the top 5.

    market_data keys:
        vix: float
        ticker_data: dict[str, TickerAnalysis]  — per-ticker analysis
        available_expirations: dict[str, list[str]]  — per-ticker expiration dates
        earnings_upcoming: list[str]  — tickers with earnings in next 14 days
    """
    existing_positions = existing_positions or []
    setups = []

    vix = market_data.get("vix", 18.0)
    regime = classify_market_regime(vix)
    ticker_data = market_data.get("ticker_data", {})
    expirations_map = market_data.get("available_expirations", {})
    earnings_tickers = set(market_data.get("earnings_upcoming", []))

    now = datetime.now()
    current_hour = now.hour

    # Track which underlyings already have open positions
    open_underlyings = set()
    for pos in existing_positions:
        if pos.get("status") == "open":
            open_underlyings.add(pos.get("underlying", ""))

    # ── 1. Iron Condors — ETFs only, VIX > 20 ──
    if regime == MarketRegime.HIGH_VOL:
        for sym in config.ETF_UNIVERSE:
            if sym in open_underlyings:
                continue
            td = ticker_data.get(sym)
            if not td:
                continue
            exps = expirations_map.get(sym, [])
            for exp in exps:
                dte = _days_to_expiration(exp)
                if not (config.IC_TARGET_DTE_MIN <= dte <= config.IC_TARGET_DTE_MAX):
                    continue
                # Skip 0DTE after 11 AM ET
                if dte == 0 and current_hour >= 11:
                    continue
                ic = build_iron_condor(sym, td.price, vix, dte, exp, equity, trend=td.trend)
                if ic:
                    setups.append(ic)

    # ── 2. Credit Spreads — ETFs + high-vol stocks, need trend ──
    cs_tickers = config.ETF_UNIVERSE + config.STOCK_UNIVERSE
    for sym in cs_tickers:
        if sym in open_underlyings:
            continue
        td = ticker_data.get(sym)
        if not td or td.trend == TrendDirection.NEUTRAL:
            continue
        exps = expirations_map.get(sym, [])
        for exp in exps:
            dte = _days_to_expiration(exp)
            if not (config.CS_TARGET_DTE_MIN <= dte <= config.CS_TARGET_DTE_MAX):
                continue
            cs = build_credit_spread(
                sym, td.price, vix, dte, exp, td.trend, equity
            )
            if cs:
                # Penalize slightly in high-vol vs iron condors
                if regime == MarketRegime.HIGH_VOL:
                    cs.score *= 0.85
                setups.append(cs)

    # ── 2b. Debit Spreads — trending markets, any vol ──
    # These profit from MOVEMENT, not time decay. Great when vol is high + trending.
    ds_tickers = config.ETF_UNIVERSE + config.STOCK_UNIVERSE
    for sym in ds_tickers:
        if sym in open_underlyings:
            continue
        td = ticker_data.get(sym)
        if not td or td.trend == TrendDirection.NEUTRAL:
            continue
        exps = expirations_map.get(sym, [])
        for exp in exps:
            dte = _days_to_expiration(exp)
            if not (5 <= dte <= 21):  # 5-21 DTE for debit spreads
                continue
            ds = build_debit_spread(
                sym, td.price, vix, dte, exp, td.trend, equity
            )
            if ds:
                # Debit spreads shine in high vol + trend (opposite of credit spreads)
                if regime == MarketRegime.HIGH_VOL and td.trend != TrendDirection.NEUTRAL:
                    ds.score += 0.05  # bonus: this is their best environment
                setups.append(ds)
            break  # one expiration per ticker

    # ── 3. Wheel (CSP) — cheap stocks, low VIX ──
    if regime == MarketRegime.LOW_VOL or vix < config.WHEEL_MAX_VIX + 3:
        for sym in config.WHEEL_STOCKS:
            if sym in open_underlyings:
                continue
            td = ticker_data.get(sym)
            if not td:
                continue
            exps = expirations_map.get(sym, [])
            for exp in exps:
                dte = _days_to_expiration(exp)
                if not (config.WHEEL_TARGET_DTE_MIN <= dte <= config.WHEEL_TARGET_DTE_MAX):
                    continue
                csp = build_wheel_csp(sym, td.price, vix, dte, exp, equity)
                if csp:
                    # Bonus in low vol
                    if regime == MarketRegime.LOW_VOL:
                        csp.score += 0.15
                    setups.append(csp)

    # ── 4. Momentum Calls/Puts — individual stocks with breakout/breakdown ──
    for sym in config.STOCK_UNIVERSE:
        if sym in open_underlyings:
            continue
        td = ticker_data.get(sym)
        if not td or td.trend == TrendDirection.NEUTRAL:
            continue
        exps = expirations_map.get(sym, [])
        for exp in exps:
            dte = _days_to_expiration(exp)
            if not (config.MOM_TARGET_DTE_MIN <= dte <= config.MOM_TARGET_DTE_MAX):
                continue
            mom = build_momentum_trade(
                sym, td.price, vix, dte, exp, td.trend, td.rsi, equity
            )
            if mom:
                setups.append(mom)
            break  # one expiration per ticker for momentum

    # ── 5. Calendar Spreads — any ticker, need near + far expirations ──
    all_tickers = config.ETF_UNIVERSE + config.STOCK_UNIVERSE
    for sym in all_tickers:
        if sym in open_underlyings:
            continue
        td = ticker_data.get(sym)
        if not td:
            continue
        exps = expirations_map.get(sym, [])
        near_exps = [(exp, _days_to_expiration(exp)) for exp in exps
                     if config.CAL_NEAR_DTE_MIN <= _days_to_expiration(exp) <= config.CAL_NEAR_DTE_MAX]
        far_exps = [(exp, _days_to_expiration(exp)) for exp in exps
                    if config.CAL_FAR_DTE_MIN <= _days_to_expiration(exp) <= config.CAL_FAR_DTE_MAX]
        if near_exps and far_exps:
            near_exp, near_dte = near_exps[0]
            far_exp, far_dte = far_exps[0]
            cal = build_calendar_spread(
                sym, td.price, vix, near_dte, far_dte, near_exp, far_exp, equity
            )
            if cal:
                setups.append(cal)

    # ── 6. Butterfly Spreads — ETFs + stocks near round numbers, low vol ──
    if regime in (MarketRegime.LOW_VOL, MarketRegime.MEDIUM_VOL):
        bf_tickers = config.ETF_UNIVERSE + config.STOCK_UNIVERSE
        for sym in bf_tickers:
            if sym in open_underlyings:
                continue
            td = ticker_data.get(sym)
            if not td or not td.near_round_number:
                continue
            exps = expirations_map.get(sym, [])
            for exp in exps:
                dte = _days_to_expiration(exp)
                if not (config.BF_TARGET_DTE_MIN <= dte <= config.BF_TARGET_DTE_MAX):
                    continue
                bf = build_butterfly(sym, td.price, vix, dte, exp, equity)
                if bf:
                    setups.append(bf)
                break  # one per ticker

    # ── 7. Earnings Strangles — stocks with upcoming earnings + high IV rank ──
    for sym in earnings_tickers:
        if sym in open_underlyings:
            continue
        td = ticker_data.get(sym)
        if not td or td.iv_rank < config.EARN_MIN_IV_RANK:
            continue
        exps = expirations_map.get(sym, [])
        for exp in exps:
            dte = _days_to_expiration(exp)
            if not (config.EARN_TARGET_DTE_MIN <= dte <= config.EARN_TARGET_DTE_MAX):
                continue
            es = build_earnings_strangle(
                sym, td.price, vix, dte, exp, td.iv_rank, equity
            )
            if es:
                setups.append(es)
            break  # one per ticker

    # Sort by score (best first), then diversify — max 2 of same strategy type
    setups.sort(key=lambda s: s.score, reverse=True)

    # ── MINIMUM QUALITY THRESHOLD ──
    # If nothing scores above 0.45, the market isn't offering good setups.
    # "No trade" beats "bad trade" every single time.
    MIN_SCORE = 0.45
    qualified = [s for s in setups if s.score >= MIN_SCORE]
    if not qualified:
        return []  # nothing worth trading today — sit on hands

    diversified = []
    strategy_counts = {}
    for s in qualified:
        stype = s.strategy.value
        if strategy_counts.get(stype, 0) < 2:
            diversified.append(s)
            strategy_counts[stype] = strategy_counts.get(stype, 0) + 1
        if len(diversified) >= 5:
            break
    return diversified


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

    Universal rules applied first, then strategy-specific rules.
    """
    strategy = position.get("strategy", "")

    # ── Strategy-specific profit targets and stop losses ──
    profit_target, stop_loss = _get_exit_params(strategy)

    # Rule 1: Take profit
    if current_pnl_pct >= profit_target:
        return True, f"Take profit: {current_pnl_pct:.0%} of max profit reached (target: {profit_target:.0%})"

    # Rule 2: Stop loss
    if current_pnl_pct <= -stop_loss:
        return True, f"Stop loss: losing {abs(current_pnl_pct):.0%} of premium (limit: {stop_loss:.0%})"

    # Rule 3: Close before expiration — avoid assignment (except wheel CSPs)
    if strategy not in (StrategyType.CASH_SECURED_PUT.value, "cash_secured_put"):
        if dte_remaining <= 1:
            return True, f"Closing: {dte_remaining} DTE remaining, avoiding assignment risk"

    # Rule 4: Volatility spike — danger for short premium positions
    entry_vix = position.get("entry_vix", vix_current)
    if strategy in ("iron_condor", "bull_put_spread", "bear_call_spread", "earnings_strangle"):
        if vix_current > entry_vix * 1.30:
            return True, (
                f"Vol spike: VIX rose from {entry_vix:.1f} to {vix_current:.1f} "
                f"(+{((vix_current / entry_vix) - 1) * 100:.0f}%) — dangerous for short premium"
            )

    # Rule 5: Momentum/debit trades — cut losers faster, let winners run
    if strategy in ("long_call", "long_put", "bull_call_spread", "bear_put_spread"):
        if current_pnl_pct <= -0.50:
            return True, f"Momentum stop: lost {abs(current_pnl_pct):.0%} of entry cost"

    # Rule 6: Earnings strangles — close before announcement day
    if strategy == "earnings_strangle":
        if dte_remaining <= 1:
            return True, "Earnings strangle: closing before announcement (event risk)"

    return False, ""


def _get_exit_params(strategy: str) -> tuple:
    """Return (profit_target, stop_loss_multiplier) for a strategy."""
    params = {
        "iron_condor":      (config.IC_PROFIT_TARGET, config.IC_STOP_LOSS),
        "bull_put_spread":  (config.CS_PROFIT_TARGET, config.CS_STOP_LOSS),
        "bear_call_spread": (config.CS_PROFIT_TARGET, config.CS_STOP_LOSS),
        "cash_secured_put": (config.WHEEL_PROFIT_TARGET, config.WHEEL_STOP_LOSS),
        "covered_call":     (0.50, 1.5),
        "long_call":        (config.MOM_PROFIT_TARGET, config.MOM_STOP_LOSS),
        "long_put":         (config.MOM_PROFIT_TARGET, config.MOM_STOP_LOSS),
        "bull_call_spread": (config.CS_PROFIT_TARGET, config.MOM_STOP_LOSS),  # take profit like credit, cut loss like momentum
        "bear_put_spread":  (config.CS_PROFIT_TARGET, config.MOM_STOP_LOSS),
        "calendar_spread":  (config.CAL_PROFIT_TARGET, config.CAL_STOP_LOSS),
        "butterfly":        (config.BF_PROFIT_TARGET, config.BF_STOP_LOSS),
        "earnings_strangle": (config.EARN_PROFIT_TARGET, config.EARN_STOP_LOSS),
    }
    return params.get(strategy, (0.50, 2.0))


def calculate_position_risk(setup: OptionsTradeSetup, account_equity: float) -> dict:
    """
    Calculate risk metrics for a potential trade.
    Uses the strategy-specific risk budget instead of a flat 5%.
    """
    total_max_loss = setup.max_loss * setup.contracts
    risk_pct = total_max_loss / account_equity if account_equity > 0 else 1.0
    max_allowed = setup.risk_budget if setup.risk_budget > 0 else 0.05

    return {
        "total_max_loss": total_max_loss,
        "risk_pct_of_equity": risk_pct,
        "max_allowed_pct": max_allowed,
        "contracts": setup.contracts,
        "approved": risk_pct <= max_allowed,
        "reason": (
            f"Risk: ${total_max_loss:.0f} ({risk_pct:.1%} of equity) — "
            f"{'APPROVED' if risk_pct <= max_allowed else f'REJECTED: exceeds {max_allowed:.0%} max risk for {setup.strategy.value}'}"
        ),
    }


# ═══════════════════════════════════════════════════════════════
# Formatting Helpers
# ═══════════════════════════════════════════════════════════════

STRATEGY_EMOJI = {
    "iron_condor": "🦅",
    "bull_put_spread": "🐂",
    "bear_call_spread": "🐻",
    "cash_secured_put": "🎡",
    "covered_call": "📞",
    "long_call": "🚀",
    "long_put": "💣",
    "calendar_spread": "📅",
    "butterfly": "🦋",
    "earnings_strangle": "📊",
}


def format_setup_summary(setup: OptionsTradeSetup) -> str:
    """Human-readable summary of a trade setup."""
    emoji = STRATEGY_EMOJI.get(setup.strategy.value, "📋")
    lines = [
        f"{'═' * 50}",
        f"{emoji} {setup.strategy.value.upper().replace('_', ' ')}",
        f"{'═' * 50}",
        f"Underlying: {setup.underlying}",
        f"Contracts: {setup.contracts}",
    ]

    for leg in setup.legs:
        side_emoji = "🔴" if leg.side == "sell" else "🟢"
        qty_str = f" x{leg.quantity}" if leg.quantity > 1 else ""
        lines.append(
            f"  {side_emoji} {leg.side.upper()} {leg.option_type.upper()} "
            f"${leg.strike:.0f} exp {leg.expiration}{qty_str}"
        )

    lines.extend([
        f"Max Profit: ${setup.max_profit:.0f}/contract (${setup.max_profit * setup.contracts:.0f} total)",
        f"Max Loss:   ${setup.max_loss:.0f}/contract (${setup.max_loss * setup.contracts:.0f} total)",
        f"Prob of Profit: ~{setup.probability_of_profit:.0%}",
        f"Risk/Reward: 1:{setup.risk_reward_ratio:.2f}",
        f"Risk Budget: {setup.risk_budget:.0%} of equity",
        f"Score: {setup.score:.2f}",
        f"Reason: {setup.reason}",
    ])

    return "\n".join(lines)
