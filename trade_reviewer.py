"""
Trade Reviewer — Tiered review system for AlpacaBot.

Three tiers:
  1. AUTO-APPROVE: Score >= auto_approve_threshold (from control.json)
     → No Claude CLI call needed. Bot scores already passed quality gate.
  2. CLAUDE REVIEW: Score between auto_reject and auto_approve thresholds
     → Borderline trades sent to Claude CLI for judgment call.
  3. AUTO-REJECT: Score < auto_reject_threshold
     → Garbage in, garbage out. Don't waste tokens.

This saves Claude CLI tokens for where they matter — the gray zone where
human-like judgment is actually needed.

Claude (the Telegram bot) has full control via control.json to adjust
thresholds, pause trading, block strategies/tickers, etc.
"""
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from trade_journal import log_activity

log = logging.getLogger("AlpacaBotOptions")
ET = ZoneInfo("America/New_York")

PENDING_FILE = Path("/workspace/AlpacaBot/pending_trades.json")
APPROVED_FILE = Path("/workspace/AlpacaBot/approved_trades.json")
REVIEW_LOG_DIR = Path("/workspace/AlpacaBot/reviews")
CONTROL_FILE = Path("/workspace/AlpacaBot/control.json")

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


def load_control() -> dict:
    """Load control settings. Returns defaults if file missing."""
    if CONTROL_FILE.exists():
        try:
            return json.loads(CONTROL_FILE.read_text())
        except Exception:
            pass
    return {
        "auto_approve_threshold": 0.75,
        "auto_reject_threshold": 0.50,
        "blocked_strategies": [],
        "blocked_tickers": [],
        "max_daily_trades": 8,
        "max_daily_loss_pct": 0.10,
        "telegram_verbosity": "trades_only",
    }


def tiered_review(proposals: list, market_data: dict, account_info: dict) -> dict:
    """
    Three-tier review system:
    1. Auto-approve high-confidence trades (saves Claude CLI tokens)
    2. Send borderline trades to Claude CLI for judgment
    3. Auto-reject low-quality trades

    Returns the same review format as before for compatibility.
    """
    control = load_control()
    approve_threshold = control.get("auto_approve_threshold", 0.75)
    reject_threshold = control.get("auto_reject_threshold", 0.50)
    blocked_strategies = control.get("blocked_strategies", [])
    blocked_tickers = control.get("blocked_tickers", [])

    auto_approved = []
    auto_rejected = []
    borderline = []

    for i, prop in enumerate(proposals):
        trade_id = i + 1
        score = prop.get("score", 0)
        strategy = prop.get("strategy", "")
        ticker = prop.get("underlying", "")
        rr = prop.get("risk_reward_ratio", 0)
        pop = prop.get("probability_of_profit", 0)

        # Check blocked lists
        if strategy in blocked_strategies:
            auto_rejected.append({
                "trade_id": trade_id,
                "decision": "reject",
                "adjusted_contracts": 0,
                "reason": f"Strategy '{strategy}' is blocked in control.json",
                "risk_notes": "Blocked by supervisor",
            })
            log_activity("auto_reject", {"trade_id": trade_id, "reason": "blocked_strategy", "strategy": strategy, "ticker": ticker})
            continue

        if ticker in blocked_tickers:
            auto_rejected.append({
                "trade_id": trade_id,
                "decision": "reject",
                "adjusted_contracts": 0,
                "reason": f"Ticker '{ticker}' is blocked in control.json",
                "risk_notes": "Blocked by supervisor",
            })
            log_activity("auto_reject", {"trade_id": trade_id, "reason": "blocked_ticker", "strategy": strategy, "ticker": ticker})
            continue

        # Tier 1: Auto-approve high-quality trades
        if score >= approve_threshold and rr >= 0.15 and pop >= 0.55:
            auto_approved.append({
                "trade_id": trade_id,
                "decision": "approve",
                "adjusted_contracts": prop.get("contracts", 1),
                "reason": f"Auto-approved: score {score:.2f} >= {approve_threshold}, R:R {rr:.2f}, PoP {pop:.0%}",
                "risk_notes": "High-conviction setup, passed all quality gates",
            })
            log_activity("auto_approve", {
                "trade_id": trade_id, "strategy": strategy, "ticker": ticker,
                "score": score, "rr": rr, "pop": pop,
            })
            continue

        # Tier 3: Auto-reject garbage
        if score < reject_threshold:
            auto_rejected.append({
                "trade_id": trade_id,
                "decision": "reject",
                "adjusted_contracts": 0,
                "reason": f"Auto-rejected: score {score:.2f} < {reject_threshold} threshold",
                "risk_notes": "Below quality floor",
            })
            log_activity("auto_reject", {
                "trade_id": trade_id, "strategy": strategy, "ticker": ticker,
                "score": score, "reason": "below_threshold",
            })
            continue

        # Tier 2: Borderline — needs Claude review
        borderline.append((trade_id, prop))

    # If there are borderline trades, send ONLY those to Claude CLI
    claude_decisions = []
    if borderline:
        log.info(f"Tiered review: {len(auto_approved)} auto-approved, {len(auto_rejected)} auto-rejected, {len(borderline)} borderline → sending to Claude")
        claude_review = _claude_review_borderline(borderline, market_data, account_info)
        claude_decisions = claude_review.get("trades", [])
        log_activity("claude_review", {
            "borderline_count": len(borderline),
            "claude_confidence": claude_review.get("overall_confidence", "unknown"),
            "decisions": [{"id": t["trade_id"], "decision": t["decision"]} for t in claude_decisions],
        })
    else:
        log.info(f"Tiered review: {len(auto_approved)} auto-approved, {len(auto_rejected)} auto-rejected, 0 borderline — no Claude CLI needed")

    # Combine all decisions
    all_trades = auto_approved + auto_rejected + claude_decisions
    all_trades.sort(key=lambda t: t.get("trade_id", 0))

    # Determine overall confidence
    approved_count = sum(1 for t in all_trades if t.get("decision") in ("approve", "adjust"))
    if approved_count == 0:
        confidence = "no_trade"
    elif approved_count >= 2:
        confidence = "high"
    else:
        confidence = "medium"

    # Build summary
    summary_parts = []
    if auto_approved:
        summary_parts.append(f"{len(auto_approved)} auto-approved (high conviction)")
    if claude_decisions:
        claude_approved = sum(1 for t in claude_decisions if t.get("decision") in ("approve", "adjust"))
        summary_parts.append(f"{claude_approved}/{len(borderline)} approved by Claude review")
    if auto_rejected:
        summary_parts.append(f"{len(auto_rejected)} auto-rejected (below threshold)")
    summary = ". ".join(summary_parts) + "." if summary_parts else "No trades proposed."

    review = {
        "market_assessment": f"SPY ${market_data.get('spy_price', 0):.2f}, VIX {market_data.get('vix', 0):.1f} ({market_data.get('regime', '?')}), trend {market_data.get('trend', '?')}",
        "overall_confidence": confidence,
        "trades": all_trades,
        "summary": summary,
        "review_mode": "tiered",
        "auto_approved": len(auto_approved),
        "auto_rejected": len(auto_rejected),
        "claude_reviewed": len(borderline),
    }

    # Save review log
    REVIEW_LOG_DIR.mkdir(parents=True, exist_ok=True)
    review_file = REVIEW_LOG_DIR / f"review_{datetime.now(ET).strftime('%Y-%m-%d_%H%M')}.json"
    review_log = {
        "timestamp": datetime.now(ET).isoformat(),
        "market": market_data,
        "proposals": proposals,
        "review": review,
        "control": control,
    }
    review_file.write_text(json.dumps(review_log, indent=2, default=str))

    return review


def _claude_review_borderline(borderline: list, market_data: dict, account_info: dict) -> dict:
    """Send only borderline trades to Claude CLI for review."""
    prompt = f"""Review these BORDERLINE options trades. They scored between the auto-approve and auto-reject thresholds, so they need your judgment.

MARKET CONDITIONS:
- SPY: ${market_data.get('spy_price', 0):.2f}
- VIX: {market_data.get('vix', 0):.1f} ({market_data.get('regime', 'unknown')})
- Trend: {market_data.get('trend', 'unknown')}
- EMA20: ${market_data.get('ema_20', 0):.2f} | EMA50: ${market_data.get('ema_50', 0):.2f}
- Date: {datetime.now(ET).strftime('%A %B %d, %Y')}
- Time: {datetime.now(ET).strftime('%H:%M ET')}

ACCOUNT:
- Equity: ${account_info.get('equity', 0):,.2f}
- Cash: ${account_info.get('cash', 0):,.2f}
- P&L Today: ${account_info.get('pnl_today', 0):+,.2f}

BORDERLINE TRADES (need your decision):
"""
    for trade_id, prop in borderline:
        prompt += f"\n--- Trade #{trade_id} ---\n"
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

    prompt += f"\nThese are borderline. Be decisive — approve or reject. Adjust if the setup is good but sizing is wrong."

    try:
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
            timeout=180,
            cwd="/workspace/AlpacaBot",
            env=clean_env,
        )

        if result.returncode != 0:
            log.error(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")
            return _fallback_review_borderline(borderline)

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
            log.error(f"Failed to parse Claude response: {output[:500]}")
            return _fallback_review_borderline(borderline)

        if "trades" not in review:
            log.error("Review missing 'trades' field")
            return _fallback_review_borderline(borderline)

        log.info(f"Claude borderline review: {review.get('overall_confidence', 'unknown')} confidence")
        return review

    except subprocess.TimeoutExpired:
        log.error("Claude borderline review timed out (180s)")
        return _fallback_review_borderline(borderline)
    except Exception as e:
        log.error(f"Claude borderline review failed: {e}")
        return _fallback_review_borderline(borderline)


def _fallback_review_borderline(borderline: list) -> dict:
    """Conservative fallback for borderline trades when Claude CLI fails."""
    log.warning("Using fallback review for borderline trades (Claude unavailable)")
    trades = []
    for trade_id, prop in borderline:
        score = prop.get("score", 0)
        rr = prop.get("risk_reward_ratio", 0)
        pop = prop.get("probability_of_profit", 0)
        # In fallback, only approve if really close to auto-approve threshold
        if score >= 0.70 and rr >= 0.15 and pop >= 0.60:
            trades.append({
                "trade_id": trade_id,
                "decision": "adjust",
                "adjusted_contracts": max(1, prop.get("contracts", 1) - 1),
                "reason": "Fallback auto-approved (borderline high score) with reduced size",
                "risk_notes": "Claude unavailable — reduced position as precaution",
            })
        else:
            trades.append({
                "trade_id": trade_id,
                "decision": "reject",
                "adjusted_contracts": 0,
                "reason": f"Fallback rejected (score {score:.2f}, borderline but Claude unavailable)",
            })

    return {
        "market_assessment": "Claude review unavailable — conservative fallback for borderline trades",
        "overall_confidence": "low",
        "trades": trades,
        "summary": "Fallback mode: borderline trades handled conservatively.",
    }


# Keep backward-compatible functions for options_bot.py imports
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
        "review_mode": review.get("review_mode", "legacy"),
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
    lines.append("⏳ Running tiered review...")
    return "\n".join(lines)


def format_review_for_telegram(review: dict, approvals: dict) -> str:
    """Format review results as a readable Telegram message."""
    confidence_emoji = {
        "high": "🟢",
        "medium": "🟡",
        "low": "🔴",
        "no_trade": "⛔",
    }
    conf = review.get("overall_confidence", "unknown")
    emoji = confidence_emoji.get(conf, "❓")

    lines = [
        f"🧠 TRADE REVIEW",
        "",
        f"{emoji} Confidence: {conf.upper()}",
    ]

    # Show review mode breakdown
    mode = review.get("review_mode", "legacy")
    if mode == "tiered":
        lines.append(f"📊 Auto-approved: {review.get('auto_approved', 0)} | Claude-reviewed: {review.get('claude_reviewed', 0)} | Auto-rejected: {review.get('auto_rejected', 0)}")
    else:
        lines.append(f"📊 {review.get('market_assessment', '')}")

    lines.append("")

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
