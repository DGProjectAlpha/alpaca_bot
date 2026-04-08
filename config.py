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
# Account Size — $2,000 ($1,000 cash + $1,000 margin)
# ═══════════════════════════════════════════════════════════════
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "2000"))
CASH_PORTION = float(os.getenv("CASH_PORTION", "1000"))
MARGIN_PORTION = float(os.getenv("MARGIN_PORTION", "1000"))

# ═══════════════════════════════════════════════════════════════
# Stock Trading (original bot)
# ═══════════════════════════════════════════════════════════════
MAX_CAPITAL = float(os.getenv("MAX_CAPITAL", "2000"))
MAX_POSITION_PCT = 0.15          # 15% max per position ($300)
MAX_POSITIONS = 50               # max 50 simultaneous positions
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
TRAILING_STOP_PCT = 0.03            # 3% trail below high-water mark
TRAILING_STOP_ACTIVATE_PCT = 0.015  # only activate trailing stop after +1.5% gain

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_TIMEFRAME = "1Hour"

VWAP_DEVIATION_BUY = -0.02
VWAP_DEVIATION_SELL = 0.01

WATCHLIST = [
    # ── Big Tech ──
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "TSLA",
    # ── Major ETFs ──
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLV", "XLI", "XLK", "XLP",
    "GDX", "TLT", "HYG", "EEM", "ARKK",
    # ── Financials ──
    "JPM", "GS", "BAC", "MS", "V", "MA", "AXP", "C",
    # ── Healthcare / Pharma ──
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY",
    # ── Energy ──
    "XOM", "CVX", "OXY", "COP", "SLB",
    # ── Industrials / Defense ──
    "BA", "CAT", "DE", "LMT", "RTX", "GE", "HON",
    # ── Consumer / Retail ──
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD",
    # ── Telecom / Media ──
    "NFLX", "DIS", "CMCSA", "T", "VZ",
    # ── Payments / Fintech ──
    "PYPL", "SQ", "SOFI",
    # ── Tech Mid-Cap ──
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "AVGO", "MU",
    # ── Transport / Logistics ──
    "UBER", "FDX", "UPS", "DAL", "UAL",
    # ── Auto / EV ──
    "RIVN", "F", "GM",
    # ── Utilities / REITs (defensive) ──
    "NEE", "DUK", "SO",
]

MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"

# ═══════════════════════════════════════════════════════════════
# Pre-Market / Extended Hours Trading
# ═══════════════════════════════════════════════════════════════
PREMARKET_OPEN = "04:00"             # Alpaca pre-market starts 4:00 AM ET
PREMARKET_CLOSE = "09:30"            # Ends at regular market open
AFTERHOURS_OPEN = "16:00"
AFTERHOURS_CLOSE = "20:00"
PREMARKET_SCAN_INTERVAL = 30         # minutes between pre-market scans
PREMARKET_LIMIT_OFFSET_PCT = 0.002   # 0.2% above ask for buys, below bid for sells
PREMARKET_MIN_GAP_PCT = 0.015        # 1.5% gap minimum to flag as opportunity
PREMARKET_MIN_VOLUME = 10000         # min pre-market volume to consider
PREMARKET_MAX_PROPOSALS = 5          # max proposals per scan
PREMARKET_PROPOSALS_FILE = "pending_premarket.json"

# ═══════════════════════════════════════════════════════════════
# Options Trading — General ($2,000 account)
# ═══════════════════════════════════════════════════════════════
OPTIONS_MAX_CAPITAL = float(os.getenv("OPTIONS_MAX_CAPITAL", "2000"))
OPTIONS_MAX_POSITIONS = int(os.getenv("OPTIONS_MAX_POSITIONS", "3"))  # 3 max — don't overexpose a small account
OPTIONS_MAX_DAILY_LOSS = float(os.getenv("OPTIONS_MAX_DAILY_LOSS", "200"))  # $200 daily loss circuit breaker (10%)

# ═══════════════════════════════════════════════════════════════
# Multi-Ticker Universe
# ═══════════════════════════════════════════════════════════════
ETF_UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLV", "XLK", "GDX", "TLT", "HYG", "EEM"]
STOCK_UNIVERSE = [
    "TSLA", "NVDA", "AMD", "META", "AMZN", "AAPL", "GOOGL", "MSFT",
    "JPM", "GS", "BAC", "UNH", "LLY", "XOM", "CVX", "BA", "CAT",
    "CRM", "ORCL", "NFLX", "COST", "HD",
]
WHEEL_STOCKS = [
    # Only stocks where 1 CSP contract collateral fits in our budget
    # With $2,000 account, max ~$500-800 collateral per wheel position
    "NIO",     # ~$5  — $500 collateral ✅
    "F",       # ~$10 — $1,000 collateral (tight but doable)
    "SOFI",    # ~$10 — $1,000 collateral (tight but doable)
    "SNAP",    # ~$10 — $1,000 collateral (tight but doable)
    # Removed: HOOD ($2k), T ($2k), PLTR ($2.5k), RIVN ($1.2k) — too expensive
]

# All tickers combined (for data fetching)
ALL_TICKERS = sorted(set(ETF_UNIVERSE + STOCK_UNIVERSE + WHEEL_STOCKS))

# ═══════════════════════════════════════════════════════════════
# Smart Scanning — Event-Driven
# ═══════════════════════════════════════════════════════════════
SCAN_INTERVAL_MINUTES = 10       # minutes between full scan cycles
CONDITION_CHECK_INTERVAL = 120   # seconds between lightweight condition checks
SPY_MOVE_THRESHOLD = 0.005       # 0.5% move triggers full rescan
VIX_CHANGE_THRESHOLD = 1.0       # 1-point VIX change triggers full rescan

# ═══════════════════════════════════════════════════════════════
# Strategy Risk Limits (% of equity per trade)
# Tighter limits for $2,000 account — survival > growth
# ═══════════════════════════════════════════════════════════════
RISK_IRON_CONDOR = 0.05          # 5% = $100 max loss per IC
RISK_CREDIT_SPREAD = 0.05        # 5% = $100 max loss per spread
RISK_WHEEL_CSP = 0.25            # 25% = $500 collateral (1 contract on ~$5 stock)
RISK_MOMENTUM = 0.03             # 3% = $60 max per speculative play
RISK_CALENDAR = 0.04             # 4% = $80 max per calendar
RISK_BUTTERFLY = 0.02            # 2% = $40 per butterfly (cheap lottery)
RISK_EARNINGS = 0.04             # 4% = $80 max per earnings play

# ═══════════════════════════════════════════════════════════════
# Strategy-Specific Defaults
# ═══════════════════════════════════════════════════════════════

# Iron Condors — narrower wings for small account
IC_SPREAD_WIDTH = 2.0            # $2 wide wings = $200 max loss per contract
IC_TARGET_DTE_MIN = 0
IC_TARGET_DTE_MAX = 7
IC_PROFIT_TARGET = 0.50          # close at 50% of max profit
IC_STOP_LOSS = 2.0               # close at 2x premium loss
IC_MIN_VIX = 20.0                # only in elevated vol

# Credit Spreads — narrower for small account
CS_SPREAD_WIDTH = 2.0            # $2 wide = $200 max loss per contract
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

# Butterfly Spreads — narrower for small account
BF_SPREAD_WIDTH = 2.0            # $2 wide = cheaper entry
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
