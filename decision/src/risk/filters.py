"""Risk Management Filters.

Implements position limits, daily volume caps, slippage controls,
and other risk management rules.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check."""
    approved: bool
    reason: str = ""
    adjusted_size: Optional[float] = None
    warnings: list[str] = field(default_factory=list)


class RiskManager:
    """Manages risk controls for copy-trading."""

    def __init__(self, config: dict):
        self.config = config

        # Limits
        self.max_position_usd = config.get("max_position_usd", 500)
        self.max_daily_volume_usd = config.get("max_daily_volume_usd", 2000)
        self.max_slippage_bps = config.get("max_slippage_bps", 150)
        self.min_order_usd = config.get("min_order_usd", 5)
        self.max_order_usd = config.get("max_order_usd", 200)
        self.cooldown_ms = config.get("cooldown_ms", 1000)
        self.max_open_positions = config.get("max_open_positions", 20)

        # Tracking
        self._daily_volume: float = 0
        self._daily_reset_time: float = 0
        self._market_cooldowns: dict[str, float] = {}
        self._order_history: list[dict] = []

        logger.info(
            "risk_manager_initialized",
            max_position=self.max_position_usd,
            max_daily=self.max_daily_volume_usd
        )

    async def check_order(
        self,
        market_id: str,
        side: str,
        size: float,
        price: float,
        position_tracker
    ) -> dict:
        """Run all risk checks on a proposed order.

        Args:
            market_id: Market identifier
            side: BUY or SELL
            size: Order size in USD
            price: Order price (0-1 for probability)
            position_tracker: PositionTracker instance

        Returns:
            Dict with 'approved', 'reason', and optionally 'adjusted_size'
        """
        # Reset daily counters if needed
        self._check_daily_reset()

        checks = [
            self._check_min_size(size),
            self._check_max_size(size),
            self._check_daily_volume(size),
            self._check_position_limit(market_id, size, position_tracker),
            self._check_open_positions_limit(position_tracker),
            self._check_cooldown(market_id),
            self._check_price_validity(price),
        ]

        warnings = []
        adjusted_size = size

        for result in checks:
            if not result.approved:
                return {
                    "approved": False,
                    "reason": result.reason
                }
            if result.warnings:
                warnings.extend(result.warnings)
            if result.adjusted_size is not None:
                adjusted_size = min(adjusted_size, result.adjusted_size)

        # Record the order attempt
        self._record_order(market_id, side, adjusted_size)

        return {
            "approved": True,
            "adjusted_size": adjusted_size,
            "warnings": warnings
        }

    def _check_daily_reset(self):
        """Reset daily volume counter at midnight UTC."""
        now = time.time()
        # Get current day start (midnight UTC)
        current_day = int(now // 86400) * 86400

        if current_day > self._daily_reset_time:
            self._daily_volume = 0
            self._daily_reset_time = current_day
            logger.info("daily_volume_reset")

    def _check_min_size(self, size: float) -> RiskCheckResult:
        """Check if order meets minimum size."""
        if size < self.min_order_usd:
            return RiskCheckResult(
                approved=False,
                reason=f"Order size ${size:.2f} below minimum ${self.min_order_usd}"
            )
        return RiskCheckResult(approved=True)

    def _check_max_size(self, size: float) -> RiskCheckResult:
        """Check and adjust for maximum order size."""
        if size > self.max_order_usd:
            return RiskCheckResult(
                approved=True,
                adjusted_size=self.max_order_usd,
                warnings=[f"Size capped from ${size:.2f} to ${self.max_order_usd}"]
            )
        return RiskCheckResult(approved=True)

    def _check_daily_volume(self, size: float) -> RiskCheckResult:
        """Check daily volume limit."""
        remaining = self.max_daily_volume_usd - self._daily_volume

        if remaining <= 0:
            return RiskCheckResult(
                approved=False,
                reason=f"Daily volume limit ${self.max_daily_volume_usd} reached"
            )

        if size > remaining:
            return RiskCheckResult(
                approved=True,
                adjusted_size=remaining,
                warnings=[f"Size reduced to ${remaining:.2f} (daily limit)"]
            )

        return RiskCheckResult(approved=True)

    def _check_position_limit(
        self,
        market_id: str,
        size: float,
        position_tracker
    ) -> RiskCheckResult:
        """Check per-market position limit."""
        current_position = position_tracker.get_position_value(market_id)
        new_total = current_position + size

        if new_total > self.max_position_usd:
            remaining = self.max_position_usd - current_position
            if remaining <= self.min_order_usd:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Position limit ${self.max_position_usd} reached for market"
                )
            return RiskCheckResult(
                approved=True,
                adjusted_size=remaining,
                warnings=[f"Size reduced to ${remaining:.2f} (position limit)"]
            )

        return RiskCheckResult(approved=True)

    def _check_open_positions_limit(self, position_tracker) -> RiskCheckResult:
        """Check maximum number of open positions."""
        open_count = position_tracker.get_open_position_count()

        if open_count >= self.max_open_positions:
            return RiskCheckResult(
                approved=False,
                reason=f"Max open positions ({self.max_open_positions}) reached"
            )

        return RiskCheckResult(approved=True)

    def _check_cooldown(self, market_id: str) -> RiskCheckResult:
        """Check cooldown period for market."""
        now = time.time() * 1000  # ms

        if market_id in self._market_cooldowns:
            last_order = self._market_cooldowns[market_id]
            elapsed = now - last_order

            if elapsed < self.cooldown_ms:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Cooldown active for market ({self.cooldown_ms - elapsed:.0f}ms remaining)"
                )

        return RiskCheckResult(approved=True)

    def _check_price_validity(self, price: float) -> RiskCheckResult:
        """Check if price is within valid range."""
        if price <= 0 or price >= 1:
            return RiskCheckResult(
                approved=True,  # Allow but warn
                warnings=["Price outside normal range (0-1)"]
            )
        return RiskCheckResult(approved=True)

    def _record_order(self, market_id: str, side: str, size: float):
        """Record order for tracking."""
        now = time.time() * 1000

        # Update daily volume
        self._daily_volume += size

        # Update cooldown
        self._market_cooldowns[market_id] = now

        # Store in history (keep last 100)
        self._order_history.append({
            "timestamp": now,
            "market_id": market_id,
            "side": side,
            "size": size
        })
        if len(self._order_history) > 100:
            self._order_history = self._order_history[-100:]

    def get_remaining_daily_volume(self) -> float:
        """Get remaining daily volume capacity."""
        self._check_daily_reset()
        return max(0, self.max_daily_volume_usd - self._daily_volume)

    def get_metrics(self) -> dict:
        """Get current risk metrics."""
        return {
            "daily_volume_used": self._daily_volume,
            "daily_volume_remaining": self.get_remaining_daily_volume(),
            "active_cooldowns": len(self._market_cooldowns),
            "orders_today": len([
                o for o in self._order_history
                if o["timestamp"] > self._daily_reset_time * 1000
            ])
        }
