"""
Trading Strategies — signal generators that return BUY/SELL/HOLD
"""
import pandas as pd
import ta
from datetime import datetime


def calculate_rsi(bars_df: pd.DataFrame, period: int = 14) -> float:
    """Calculate RSI from a DataFrame of bars."""
    if len(bars_df) < period + 1:
        return 50.0  # neutral if not enough data
    rsi = ta.momentum.RSIIndicator(bars_df["close"], window=period)
    values = rsi.rsi().dropna()
    return values.iloc[-1] if len(values) > 0 else 50.0


def calculate_vwap(bars_df: pd.DataFrame) -> float:
    """Calculate VWAP from today's bars."""
    if len(bars_df) == 0:
        return 0.0
    typical_price = (bars_df["high"] + bars_df["low"] + bars_df["close"]) / 3
    vwap = (typical_price * bars_df["volume"]).cumsum() / bars_df["volume"].cumsum()
    return vwap.iloc[-1] if len(vwap) > 0 else 0.0


def calculate_ema(bars_df: pd.DataFrame, period: int = 20) -> float:
    """Calculate EMA."""
    if len(bars_df) < period:
        return bars_df["close"].iloc[-1] if len(bars_df) > 0 else 0.0
    ema = ta.trend.EMAIndicator(bars_df["close"], window=period)
    values = ema.ema_indicator().dropna()
    return values.iloc[-1] if len(values) > 0 else 0.0


def calculate_macd(bars_df: pd.DataFrame) -> dict:
    """Calculate MACD signal."""
    if len(bars_df) < 26:
        return {"signal": "HOLD", "macd": 0, "macd_signal": 0}
    macd = ta.trend.MACD(bars_df["close"])
    macd_line = macd.macd().dropna()
    signal_line = macd.macd_signal().dropna()
    if len(macd_line) < 2 or len(signal_line) < 2:
        return {"signal": "HOLD", "macd": 0, "macd_signal": 0}

    # Bullish crossover: MACD crosses above signal
    if macd_line.iloc[-1] > signal_line.iloc[-1] and macd_line.iloc[-2] <= signal_line.iloc[-2]:
        return {"signal": "BUY", "macd": macd_line.iloc[-1], "macd_signal": signal_line.iloc[-1]}
    # Bearish crossover
    if macd_line.iloc[-1] < signal_line.iloc[-1] and macd_line.iloc[-2] >= signal_line.iloc[-2]:
        return {"signal": "SELL", "macd": macd_line.iloc[-1], "macd_signal": signal_line.iloc[-1]}

    return {"signal": "HOLD", "macd": macd_line.iloc[-1], "macd_signal": signal_line.iloc[-1]}


def calculate_bollinger_position(bars_df: pd.DataFrame, period: int = 20) -> dict:
    """Where is price relative to Bollinger Bands? Returns 0-1 (0=lower band, 1=upper band)."""
    if len(bars_df) < period:
        return {"position": 0.5, "upper": 0, "lower": 0, "mid": 0}
    bb = ta.volatility.BollingerBands(bars_df["close"], window=period, window_dev=2)
    upper = bb.bollinger_hband().iloc[-1]
    lower = bb.bollinger_lband().iloc[-1]
    mid = bb.bollinger_mavg().iloc[-1]
    price = bars_df["close"].iloc[-1]
    width = upper - lower
    position = (price - lower) / width if width > 0 else 0.5
    return {"position": position, "upper": upper, "lower": lower, "mid": mid}


def generate_signal(bars_df: pd.DataFrame, rsi_oversold: float = 30, rsi_overbought: float = 70) -> dict:
    """
    Multi-indicator signal generator.
    Combines RSI + MACD + Bollinger Bands for confluence.
    Returns signal with confidence score.
    """
    if len(bars_df) < 30:
        return {"action": "HOLD", "confidence": 0, "reasons": ["Not enough data"]}

    rsi = calculate_rsi(bars_df)
    macd = calculate_macd(bars_df)
    bb = calculate_bollinger_position(bars_df)
    ema_20 = calculate_ema(bars_df, 20)
    ema_50 = calculate_ema(bars_df, 50)
    price = bars_df["close"].iloc[-1]

    buy_signals = 0
    sell_signals = 0
    reasons = []

    # RSI
    if rsi < rsi_oversold:
        buy_signals += 2  # strong signal
        reasons.append(f"RSI oversold ({rsi:.1f})")
    elif rsi < 40:
        buy_signals += 1
        reasons.append(f"RSI low ({rsi:.1f})")
    elif rsi > rsi_overbought:
        sell_signals += 2
        reasons.append(f"RSI overbought ({rsi:.1f})")
    elif rsi > 60:
        sell_signals += 1
        reasons.append(f"RSI high ({rsi:.1f})")

    # MACD
    if macd["signal"] == "BUY":
        buy_signals += 2
        reasons.append("MACD bullish crossover")
    elif macd["signal"] == "SELL":
        sell_signals += 2
        reasons.append("MACD bearish crossover")

    # Bollinger Bands
    if bb["position"] < 0.1:
        buy_signals += 2
        reasons.append(f"Price at lower Bollinger Band")
    elif bb["position"] < 0.3:
        buy_signals += 1
        reasons.append(f"Price near lower BB")
    elif bb["position"] > 0.9:
        sell_signals += 2
        reasons.append(f"Price at upper Bollinger Band")
    elif bb["position"] > 0.7:
        sell_signals += 1
        reasons.append(f"Price near upper BB")

    # EMA trend
    if ema_20 > ema_50 and price > ema_20:
        buy_signals += 1
        reasons.append("Uptrend (EMA 20 > 50, price above)")
    elif ema_20 < ema_50 and price < ema_20:
        sell_signals += 1
        reasons.append("Downtrend (EMA 20 < 50, price below)")

    # Decision — need at least 3 points of confluence to act
    max_score = max(buy_signals, sell_signals)
    confidence = min(max_score / 6.0, 1.0)  # normalize to 0-1

    if buy_signals >= 3 and buy_signals > sell_signals:
        return {"action": "BUY", "confidence": confidence, "reasons": reasons, "rsi": rsi}
    elif sell_signals >= 3 and sell_signals > buy_signals:
        return {"action": "SELL", "confidence": confidence, "reasons": reasons, "rsi": rsi}
    else:
        return {"action": "HOLD", "confidence": confidence, "reasons": reasons, "rsi": rsi}
