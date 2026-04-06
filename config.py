"""
AlpacaBot Configuration — Stocks + Options
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# Alpaca Credentials
# ═══════════════════════════════════════════════════════════════
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"

# ═══════════════════════════════════════════════════════════════
# Telegram Notifications
# ═══════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")
_topic_id_raw = os.getenv("TELEGRAM_ALERTS_TOPIC_ID", "").strip()
TELEGRAM_ALERTS_TOPIC_ID = int(_topic_id_raw) if _topic_id_raw else None

# ═══════════════════════════════════════════════════════════════
# Global Settings
# ═══════════════════════════════════════════════════════════════
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "15"))

# ═══════════════════════════════════════════════════════════════
# Stock Trading (original bot)
# ═══════════════════════════════════════════════════════════════
MAX_CAPITAL = float(os.getenv("MAX_CAPITAL", "1000"))
MAX_POSITION_PCT = 0.20
MAX_POSITIONS = 5
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_TIMEFRAME = "1Hour"

VWAP_DEVIATION_BUY = -0.02
VWAP_DEVIATION_SELL = 0.01

WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "NVDA", "AMD", "TSLA", "SPY", "QQQ",
    "NFLX", "DIS", "PYPL", "SQ", "COIN",
    "SOFI", "PLTR", "SNAP", "UBER", "RIVN",
]

MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# ═══════════════════════════════════════════════════════════════
# Options Trading
# ═══════════════════════════════════════════════════════════════
OPTIONS_MAX_CAPITAL = float(os.getenv("OPTIONS_MAX_CAPITAL", "1000"))
OPTIONS_MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "3"))
OPTIONS_MAX_RISK_PER_TRADE = 0.05  # max 5% of equity per trade

# Strategy: Iron Condor defaults
IC_SPREAD_WIDTH = 2.0       # $2 wide spreads
IC_TARGET_DTE = 3           # 0-3 DTE for quick theta decay
IC_PROFIT_TARGET = 0.50     # close at 50% of max profit
IC_STOP_LOSS = 2.0          # close at 2x premium loss

# Strategy: Credit Spread defaults
CS_SPREAD_WIDTH = 2.0
CS_TARGET_DTE_MIN = 5
CS_TARGET_DTE_MAX = 14
CS_PROFIT_TARGET = 0.50
CS_STOP_LOSS = 2.0

# Strategy: Wheel defaults
WHEEL_TARGET_DTE_MIN = 14
WHEEL_TARGET_DTE_MAX = 45

# Stocks eligible for Wheel strategy (must be cheap enough to cover 100 shares)
WHEEL_STOCKS = [
    "SOFI",    # ~$10 — $1,000 collateral
    "PLTR",    # ~$25 — need more capital for this
    "SNAP",    # ~$10 — $1,000 collateral
    "RIVN",    # ~$12 — $1,200 collateral
    "F",       # ~$10 — $1,000 collateral
    "NIO",     # ~$5  — $500 collateral
    "HOOD",    # ~$20 — $2,000 collateral
    "T",       # ~$20 — $2,000 collateral
]
