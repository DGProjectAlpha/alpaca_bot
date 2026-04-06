"""
Trade Reviewer — Claude-powered risk manager for AlpacaBot.

Flow:
  1. Options bot generates proposed trades → pending_trades.json
  2. This module feeds proposals to Claude CLI for analysis
  3. Claude reviews market conditions, risk/reward, and each trade
  4. Returns structured approvals (approve/reject/adjust per trade)
  5. Only approved trades get executed

Claude acts as a sharp risk manager — not a yes-man. It will reject
trades that look marginal and adjust position sizes when warranted.
"""
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("AlpacaBotOptions")
ET = ZoneInfo("America/New_York")

PENDING_FILE = Path("/workspace/AlpacaBot/pending_trades.json")
APPROVED_FILE = Path("/workspace/AlpacaBot/approved_trades.json")
REVIEW_LOG_DIR = Path("/workspace/AlpacaBot/reviews")

# JSON schema for structured Claude output
REVIEW_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "market_assessment": {
            "type": "string",
            "description": "1-3 sentence assessment of current market conditions"
        },
        "overall_confidence": {
            "type": "string",
            "enum": ["high", "medium", "low", "no_trade"],
            "description": "Overall confidence in trading today"
        },
        "trades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "trade_id": {"type": "integer"},
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "reject", "adjust"],
                        "description": "Whether to execute this trade"
                    },
                    "adjusted_contracts": {
                        "type": "integer",
                        "description": "Number of contracts (same as proposed if approve, different if adjust)"
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this trade was approved/rejected/adjusted"
                    },
                    "risk_notes": {
                        "type": "string",
                        "description": "Any risk concerns to flag"
                    }
                },
                "required": ["trade_id", "decision", "adjusted_contracts", "reason"]
            }
        },
        "summary": {
            "type": "string",
            "description": "Brief summary for Telegram (2-3 sentences max)"
        }
    },
    "required": ["market_assessment", "overall_confidence", "trades", "summary"]
})

SYSTEM_PROMPT = """You are a senior options risk manager reviewing proposed trades for an automated trading bot.

Your job:
- Analyze market conditions and each proposed trade
- APPROVE trades you're confident will profit (>60% probability)
- REJECT trades that look marginal, poorly timed, or too risky
- ADJUST position sizes if the setup is good but size is wrong

Rules:
- Never approve a trade just because the bot proposed it. Be skeptical.
- High VIX (>25) is great for iron condors but watch for trend days that blow through strikes
- Credit spreads need a clear trend — reject if trend signal is weak
- Wheel CSPs are safe but boring — only approve on quality stocks at good prices
- If VIX just spiked, vol might still be expanding — that kills premium sellers
- If it's a big economic data day (FOMC, CPI, NFP, earnings), be extra cautious
- Max 5% of equity at risk per trade
- Prefer fewer high-conviction trades over many marginal ones
- If nothing looks good, say so. "No trade" IS a valid decision.

You are protecting real money. Be direct. No hedging language."""


def save_proposals(proposals: list, market_data: dict):
    """Save proposed trades to pending_trades.json for review."""
    pending = {
        "timestamp": datetime.now(ET).isoformat(),
        "market": market_data,
        "proposals": proposals,
        "status": "pending_review"
    }
    PENDING_FILE.write_text(json.dumps(pending, indent=2, default=str))
    log.info(f"Saved {len(proposals)} proposals to {PENDING_FILE}")
    return pending


def review_trades(pending: dict, account_info: dict) -> dict:
    """
    Send proposals to Claude CLI for review.
    Returns structured approval decisions.
    """
    REVIEW_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Build the review prompt
    market = pending["market"]
    proposals = pending["proposals"]

    prompt = f"""Review these proposed options trades.

MARKET CONDITIONS:
- SPY: ${market.get('spy_price', 0):.2f}
- VIX: {market.get('vix', 0):.1f} ({market.get('regime', 'unknown')})
- Trend: {market.get('trend', 'unknown')}
- EMA20: ${market.get('ema_20', 0):.2f} | EMA50: ${market.get('ema_50', 0):.2f}
- Date: {datetime.now(ET).strftime('%A %B %d, %Y')}
- Time: {datetime.now(ET).strftime('%H:%M ET')}

ACCOUNT:
- Equity: ${account_info.get('equity', 0):,.2f}
- Cash: ${account_info.get('cash', 0):,.2f}
- P&L Today: ${account_info.get('pnl_today', 0):+,.2f}

PROPOSED TRADES:
"""
    for i, prop in enumerate(proposals):
        prompt += f"\n--- Trade #{i+1} ---\n"
        prompt += f"Strategy: {prop.get('strategy', 'unknown')}\n"
        prompt += f"Underlying: {prop.get('underlying', 'unknown')}\n"
        prompt += f"Score: {prop.get('score', 0):.2f}\n"
        prompt += f"Contracts: {prop.get('contracts', 0)}\n"
        prompt += f"Max Profit: ${prop.get('max_profit', 0):.0f}/contract\n"
        prompt += f"Max Loss: ${prop.get('max_loss', 0):.0f}/contract\n"
        prompt += f"Prob of Profit: ~{prop.get('probability_of_profit', 0):.0%}\n"
        prompt += f"Risk/Reward: 1:{prop.get('risk_reward_ratio', 0):.2f}\n"
        prompt += f"DTE: {prop.get('target_dte', 0)}\n"
        prompt += f"Reason: {prop.get('reason', '')}\n"

        if prop.get('legs'):
            prompt += "Legs:\n"
            for leg in prop['legs']:
                prompt += f"  {leg.get('side', '').upper()} {leg.get('option_type', '').upper()} ${leg.get('strike', 0):.0f} exp {leg.get('expiration', '')} @ ${leg.get('premium', 0):.2f}\n"

    prompt += f"\nReview each trade. Approve, reject, or adjust. Protect the capital."

    log.info("Sending proposals to Claude for review...")

    try:
        # Build clean env — must unset CLAUDECODE to avoid nested session detection
        import os
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        result = subprocess.run(
            [
                "claude", "-p",
                "--model", "sonnet",
                "--output-format", "json",
                "--json-schema", REVIEW_SCHEMA,
                "--append-system-prompt", SYSTEM_PROMPT,
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd="/workspace/AlpacaBot",
            env=clean_env,
        )

        if result.returncode != 0:
            log.error(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
            return _fallback_review(proposals)

        # Parse the JSON output
        output = result.stdout.strip()
        try:
            wrapper = json.loads(output)
            # claude --output-format json puts structured output in "structured_output"
            if isinstance(wrapper, dict) and "structured_output" in wrapper:
                review = wrapper["structured_output"]
            elif isinstance(wrapper, dict) and "result" in wrapper:
                # Fallback: result might be a JSON string
                try:
                    review = json.loads(wrapper["result"])
                except (json.JSONDecodeError, TypeError):
                    review = wrapper
            elif isinstance(wrapper, dict) and "market_assessment" in wrapper:
                review = wrapper
            else:
                review = wrapper
        except (json.JSONDecodeError, TypeError):
            log.error(f"Failed to parse Claude response: {output[:500]}")
            return _fallback_review(proposals)

        # Validate structure
        if "trades" not in review:
            log.error("Review missing 'trades' field")
            return _fallback_review(proposals)

        log.info(f"Review complete: {review.get('overall_confidence', 'unknown')} confidence")
        log.info(f"Assessment: {review.get('market_assessment', '')}")

        # Save review log
        review_file = REVIEW_LOG_DIR / f"review_{datetime.now(ET).strftime('%Y-%m-%d_%H%M')}.json"
        review_log = {
            "timestamp": datetime.now(ET).isoformat(),
            "market": pending["market"],
            "proposals": proposals,
            "review": review,
        }
        review_file.write_text(json.dumps(review_log, indent=2, default=str))

        return review

    except subprocess.TimeoutExpired:
        log.error("Claude review timed out (120s)")
        return _fallback_review(proposals)
    except Exception as e:
        log.error(f"Review failed: {e}")
        return _fallback_review(proposals)


def save_approvals(review: dict, proposals: list):
    """Save approved trades to approved_trades.json."""
    approved = []
    for trade_decision in review.get("trades", []):
        trade_id = trade_decision.get("trade_id", 0)
        decision = trade_decision.get("decision", "reject")

        if decision in ("approve", "adjust") and 0 < trade_id <= len(proposals):
            proposal = proposals[trade_id - 1].copy()
            if decision == "adjust":
                proposal["contracts"] = trade_decision.get("adjusted_contracts", proposal.get("contracts", 1))
            proposal["review_decision"] = decision
            proposal["review_reason"] = trade_decision.get("reason", "")
            proposal["risk_notes"] = trade_decision.get("risk_notes", "")
            approved.append(proposal)

    result = {
        "timestamp": datetime.now(ET).isoformat(),
        "overall_confidence": review.get("overall_confidence", "unknown"),
        "market_assessment": review.get("market_assessment", ""),
        "summary": review.get("summary", ""),
        "approved_trades": approved,
        "total_proposed": len(proposals),
        "total_approved": len(approved),
    }

    APPROVED_FILE.write_text(json.dumps(result, indent=2, default=str))
    log.info(f"Approved {len(approved)}/{len(proposals)} trades → {APPROVED_FILE}")
    return result


def load_approvals() -> dict:
    """Load approved trades from file."""
    if APPROVED_FILE.exists():
        try:
            return json.loads(APPROVED_FILE.read_text())
        except Exception:
            return {"approved_trades": []}
    return {"approved_trades": []}


def _fallback_review(proposals: list) -> dict:
    """
    Conservative fallback if Claude review fails.
    Only approves high-score trades with reduced size.
    """
    log.warning("Using fallback review (Claude unavailable)")
    trades = []
    for i, prop in enumerate(proposals):
        score = prop.get("score", 0)
        if score >= 0.7:
            trades.append({
                "trade_id": i + 1,
                "decision": "adjust",
                "adjusted_contracts": max(1, prop.get("contracts", 1) - 1),
                "reason": "Auto-approved (high score) with reduced size — Claude review unavailable",
                "risk_notes": "Fallback mode: reduced position size as precaution"
            })
        else:
            trades.append({
                "trade_id": i + 1,
                "decision": "reject",
                "adjusted_contracts": 0,
                "reason": f"Auto-rejected (score {score:.2f} < 0.70) — Claude review unavailable",
            })

    return {
        "market_assessment": "Claude review unavailable — using conservative fallback rules",
        "overall_confidence": "low",
        "trades": trades,
        "summary": "Fallback mode: only high-confidence trades approved with reduced size."
    }


def format_proposals_for_telegram(proposals: list, market_data: dict) -> str:
    """Format proposals as a readable Telegram message."""
    now = datetime.now(ET)
    lines = [
        f"📋 TRADE PROPOSALS — {now.strftime('%A %b %d, %H:%M ET')}",
        "",
        f"SPY: ${market_data.get('spy_price', 0):.2f} | VIX: {market_data.get('vix', 0):.1f} ({market_data.get('regime', '?')})",
        f"Trend: {market_data.get('trend', '?')}",
        "",
    ]

    for i, prop in enumerate(proposals):
        lines.append(f"{'─' * 30}")
        lines.append(f"Trade #{i+1}: {prop.get('strategy', '?').upper().replace('_', ' ')}")
        lines.append(f"  {prop.get('underlying', '?')} | {prop.get('contracts', 0)} contracts | {prop.get('target_dte', 0)} DTE")
        lines.append(f"  Max Profit: ${prop.get('max_profit', 0):.0f} | Max Loss: ${prop.get('max_loss', 0):.0f}")
        lines.append(f"  Win Rate: ~{prop.get('probability_of_profit', 0):.0%} | Score: {prop.get('score', 0):.2f}")

        if prop.get('legs'):
            for leg in prop['legs']:
                emoji = "🔴" if leg.get('side') == 'sell' else "🟢"
                lines.append(f"  {emoji} {leg['side'].upper()} {leg['option_type'].upper()} ${leg['strike']:.0f} @ ${leg.get('premium', 0):.2f}")

    lines.append("")
    lines.append("⏳ Sending to Claude for review...")
    return "\n".join(lines)


def format_review_for_telegram(review: dict, approvals: dict) -> str:
    """Format Claude's review as a readable Telegram message."""
    confidence_emoji = {
        "high": "🟢",
        "medium": "🟡",
        "low": "🔴",
        "no_trade": "⛔",
    }
    conf = review.get("overall_confidence", "unknown")
    emoji = confidence_emoji.get(conf, "❓")

    lines = [
        f"🧠 CLAUDE'S REVIEW",
        "",
        f"{emoji} Confidence: {conf.upper()}",
        f"📊 {review.get('market_assessment', '')}",
        "",
    ]

    for trade in review.get("trades", []):
        tid = trade.get("trade_id", 0)
        decision = trade.get("decision", "?")
        dec_emoji = {"approve": "✅", "reject": "❌", "adjust": "🔧"}.get(decision, "❓")

        lines.append(f"{dec_emoji} Trade #{tid}: {decision.upper()}")
        lines.append(f"   {trade.get('reason', '')}")
        if trade.get("risk_notes"):
            lines.append(f"   ⚠️ {trade['risk_notes']}")
        if decision == "adjust":
            lines.append(f"   📐 Adjusted to {trade.get('adjusted_contracts', '?')} contracts")
        lines.append("")

    approved_count = approvals.get("total_approved", 0)
    total = approvals.get("total_proposed", 0)
    lines.append(f"📋 Result: {approved_count}/{total} trades approved")
    lines.append("")
    lines.append(f"💬 {review.get('summary', '')}")

    return "\n".join(lines)
