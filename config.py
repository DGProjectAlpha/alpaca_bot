"""
AlpacaBot Configuration — Stocks + Options (Multi-Strategy)
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
# Options Trading — General
# ═══════════════════════════════════════════════════════════════
OPTIONS_MAX_CAPITAL = float(os.getenv("OPTIONS_MAX_CAPITAL", "1000"))
OPTIONS_MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "5"))

# ═══════════════════════════════════════════════════════════════
# Multi-Ticker Universe
# ═══════════════════════════════════════════════════════════════
ETF_UNIVERSE = ["SPY", "QQQ", "IWM", "DIA"]
STOCK_UNIVERSE = ["TSLA", "NVDA", "AMD", "META", "AMZN", "AAPL", "GOOGL", "MSFT"]
WHEEL_STOCKS = [
    "SOFI",    # ~$10 — $1,000 collateral
    "SNAP",    # ~$10 — $1,000 collateral
    "F",       # ~$10 — $1,000 collateral
    "NIO",     # ~$5  — $500 collateral
    "HOOD",    # ~$20 — $2,000 collateral
    "T",       # ~$20 — $2,000 collateral
    "PLTR",    # ~$25 — $2,500 collateral
    "RIVN",    # ~$12 — $1,200 collateral
]

# All tickers combined (for data fetching)
ALL_TICKERS = sorted(set(ETF_UNIVERSE + STOCK_UNIVERSE + WHEEL_STOCKS))

# ═══════════════════════════════════════════════════════════════
# Smart Scanning — Event-Driven
# ═══════════════════════════════════════════════════════════════
CONDITION_CHECK_INTERVAL = 120   # seconds between lightweight condition checks
SPY_MOVE_THRESHOLD = 0.005       # 0.5% move triggers full rescan
VIX_CHANGE_THRESHOLD = 1.0       # 1-point VIX change triggers full rescan

# ═══════════════════════════════════════════════════════════════
# Strategy Risk Limits (% of equity per trade)
# ═══════════════════════════════════════════════════════════════
RISK_IRON_CONDOR = 0.05          # 5%  — defined risk, high probability
RISK_CREDIT_SPREAD = 0.05        # 5%  — defined risk, directional
RISK_WHEEL_CSP = 0.10            # 10% — collateral-based (actual risk is lower)
RISK_MOMENTUM = 0.02             # 2%  — speculative long options
RISK_CALENDAR = 0.03             # 3%  — moderate risk spread
RISK_BUTTERFLY = 0.01            # 1%  — cheap lottery tickets
RISK_EARNINGS = 0.04             # 4%  — short premium around events

# ═══════════════════════════════════════════════════════════════
# Strategy-Specific Defaults
# ═══════════════════════════════════════════════════════════════

# Iron Condors
IC_SPREAD_WIDTH = 5.0            # $5 wide wings
IC_TARGET_DTE_MIN = 0
IC_TARGET_DTE_MAX = 7
IC_PROFIT_TARGET = 0.50          # close at 50% of max profit
IC_STOP_LOSS = 2.0               # close at 2x premium loss
IC_MIN_VIX = 20.0                # only in elevated vol

# Credit Spreads
CS_SPREAD_WIDTH = 5.0
CS_TARGET_DTE_MIN = 5
CS_TARGET_DTE_MAX = 21
CS_PROFIT_TARGET = 0.50
CS_STOP_LOSS = 2.0

# Wheel (Cash-Secured Puts)
WHEEL_TARGET_DTE_MIN = 14
WHEEL_TARGET_DTE_MAX = 45
WHEEL_MAX_VIX = 15.0             # low-vol environment preferred
WHEEL_PROFIT_TARGET = 0.50
WHEEL_STOP_LOSS = 1.5            # tighter stop — want assignment at good price

# Momentum (Long Calls/Puts)
MOM_TARGET_DTE_MIN = 7
MOM_TARGET_DTE_MAX = 30
MOM_PROFIT_TARGET = 1.0          # take 100% gain
MOM_STOP_LOSS = 0.50             # cut at 50% loss
MOM_RSI_OVERBOUGHT = 70
MOM_RSI_OVERSOLD = 30

# Calendar Spreads
CAL_NEAR_DTE_MIN = 5
CAL_NEAR_DTE_MAX = 14
CAL_FAR_DTE_MIN = 21
CAL_FAR_DTE_MAX = 45
CAL_PROFIT_TARGET = 0.50
CAL_STOP_LOSS = 1.5

# Butterfly Spreads
BF_SPREAD_WIDTH = 5.0
BF_TARGET_DTE_MIN = 7
BF_TARGET_DTE_MAX = 21
BF_PROFIT_TARGET = 1.0           # these are cheap — let winners run
BF_STOP_LOSS = 0.80              # accept loss on most

# Earnings Strangles (sell)
EARN_MIN_IV_RANK = 0.70          # IV rank >70% to sell
EARN_TARGET_DTE_MIN = 3
EARN_TARGET_DTE_MAX = 14
EARN_PROFIT_TARGET = 0.40        # take profit faster — event risk
EARN_STOP_LOSS = 1.5
