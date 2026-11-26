"""Exit gating policy: close only on profit unless a risk condition justifies a loss."""

from typing import Any, Dict, Optional, Tuple

DEFAULT_EXIT_POLICY: Dict[str, Any] = {
    "min_profit_usdt": 0.0,
    "max_loss_usdt": None,
    "allow_loss_on_hold": False,
}

PROFIT_REASONS = ("basis_target_reached", "basis_target_already_reached")
LOSS_GATED_REASONS = ("funding_flipped", "target_hold_days_reached")


def resolve_policy(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge profile settings on top of policy defaults, treating NULL as unset."""
    resolved = dict(DEFAULT_EXIT_POLICY)
    for key, default in DEFAULT_EXIT_POLICY.items():
        value = (settings or {}).get(key)
        resolved[key] = default if value is None else value
    resolved["min_profit_usdt"] = float(resolved["min_profit_usdt"] or 0.0)
    max_loss = resolved["max_loss_usdt"]
    resolved["max_loss_usdt"] = abs(float(max_loss)) if max_loss is not None else None
    resolved["allow_loss_on_hold"] = bool(resolved["allow_loss_on_hold"])
    return resolved


def _loss_cap_hit(pnl: float, max_loss_usdt: Optional[float]) -> bool:
    return max_loss_usdt is not None and pnl <= -max_loss_usdt


def decide_exit(reason: str, pnl_usdt: Optional[float], settings: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Return (allowed, note) for an exit trigger.

    - Profit-only triggers (basis target) wait until realized PnL exceeds min_profit_usdt
      (breakeven at exactly the floor still waits).
    - Funding flip / max hold may close at a loss only when the configured loss cap is
      breached, allow_loss_on_hold is enabled, or the trade is already profitable.
    - Risk triggers (adverse basis stop, ADL, drift repair, stale close) always execute.
    - Unknown PnL never traps the position open: risk management wins over stale quotes.
    """
    policy = resolve_policy(settings)
    min_profit = policy["min_profit_usdt"]
    if pnl_usdt is None:
        return True, "pnl_unavailable_allow"
    profitable = pnl_usdt > min_profit
    if reason.startswith(PROFIT_REASONS):
        if profitable:
            return True, f"profit({pnl_usdt:+.4f}>{min_profit:.4f})"
        return False, f"profit_pending({pnl_usdt:+.4f}<={min_profit:.4f})"
    if reason.startswith(LOSS_GATED_REASONS):
        if profitable:
            return True, f"profit({pnl_usdt:+.4f})"
        if policy["allow_loss_on_hold"]:
            return True, "loss_allowed_by_setting"
        if _loss_cap_hit(pnl_usdt, policy["max_loss_usdt"]):
            return True, f"stop_loss({pnl_usdt:+.4f})"
        return False, f"loss_blocked({pnl_usdt:+.4f}); waiting for profit or stop_loss"
    return True, "risk_exit"
