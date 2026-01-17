"""Position Sizing Strategies.

Implements PERCENTAGE, FIXED, and ADAPTIVE sizing strategies for copy-trading.
"""

from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class SizingStrategy(str, Enum):
    """Available position sizing strategies."""
    PERCENTAGE = "PERCENTAGE"
    FIXED = "FIXED"
    ADAPTIVE = "ADAPTIVE"


class PositionSizer:
    """Calculates position sizes based on configured strategy."""

    def __init__(self, config: dict):
        self.config = config
        self.strategy = SizingStrategy(config.get("sizing_strategy", "PERCENTAGE"))

        # Strategy-specific configs
        self.percentage_config = config.get("percentage", {})
        self.fixed_config = config.get("fixed", {})
        self.adaptive_config = config.get("adaptive", {})

        logger.info("position_sizer_initialized", strategy=self.strategy.value)

    def calculate_size(
        self,
        target_size: float,
        copy_ratio: float = 1.0,
        price: float = 0.5,
        confidence: Optional[float] = None,
        volatility: Optional[float] = None
    ) -> float:
        """Calculate the position size to copy.

        Args:
            target_size: The original trade size from target wallet (USD)
            copy_ratio: Wallet-specific copy multiplier (0-1)
            price: Current market price (0-1)
            confidence: Optional confidence score for adaptive sizing
            volatility: Optional market volatility metric

        Returns:
            Calculated position size in USD
        """
        if self.strategy == SizingStrategy.PERCENTAGE:
            return self._percentage_size(target_size, copy_ratio)
        elif self.strategy == SizingStrategy.FIXED:
            return self._fixed_size(copy_ratio)
        elif self.strategy == SizingStrategy.ADAPTIVE:
            return self._adaptive_size(target_size, copy_ratio, confidence, volatility)
        else:
            # Default to percentage
            return self._percentage_size(target_size, copy_ratio)

    def _percentage_size(self, target_size: float, copy_ratio: float) -> float:
        """Calculate size as percentage of target trade.

        Example: If target trades $1000 and base_pct=10, we trade $100.
        """
        base_pct = self.percentage_config.get("base_pct", 10) / 100
        max_pct = self.percentage_config.get("max_pct", 25) / 100

        # Calculate base size
        calculated_size = target_size * base_pct * copy_ratio

        # Cap at max percentage
        max_size = target_size * max_pct
        final_size = min(calculated_size, max_size)

        logger.debug(
            "percentage_sizing",
            target=target_size,
            base_pct=base_pct,
            copy_ratio=copy_ratio,
            result=final_size
        )

        return final_size

    def _fixed_size(self, copy_ratio: float) -> float:
        """Return fixed amount adjusted by copy ratio.

        Example: If fixed amount is $25 and copy_ratio is 0.5, we trade $12.50.
        """
        base_amount = self.fixed_config.get("amount_usd", 25)
        return base_amount * copy_ratio

    def _adaptive_size(
        self,
        target_size: float,
        copy_ratio: float,
        confidence: Optional[float] = None,
        volatility: Optional[float] = None
    ) -> float:
        """Calculate size adaptively based on confidence and market conditions.

        Scales between min_pct and max_pct based on confidence score.
        Higher confidence = larger position.
        """
        min_pct = self.adaptive_config.get("min_pct", 5) / 100
        max_pct = self.adaptive_config.get("max_pct", 20) / 100
        confidence_weight = self.adaptive_config.get("confidence_weight", 0.5)

        # Default confidence if not provided
        if confidence is None:
            confidence = 0.5

        # Clamp confidence to [0, 1]
        confidence = max(0, min(1, confidence))

        # Calculate percentage based on confidence
        # Higher confidence = closer to max_pct
        pct_range = max_pct - min_pct
        adjusted_pct = min_pct + (pct_range * confidence * confidence_weight)

        # Apply volatility adjustment if provided
        if volatility is not None:
            # Higher volatility = smaller position
            volatility_factor = max(0.5, 1 - volatility)
            adjusted_pct *= volatility_factor

        calculated_size = target_size * adjusted_pct * copy_ratio

        logger.debug(
            "adaptive_sizing",
            target=target_size,
            confidence=confidence,
            volatility=volatility,
            adjusted_pct=adjusted_pct,
            result=calculated_size
        )

        return calculated_size


class KellyCalculator:
    """Kelly Criterion calculator for optimal bet sizing.

    The Kelly Criterion determines the optimal fraction of capital to bet
    to maximize long-term growth rate.

    f* = (bp - q) / b

    Where:
    - f* = fraction of bankroll to bet
    - b = odds received on the bet (decimal odds - 1)
    - p = probability of winning
    - q = probability of losing (1 - p)
    """

    def __init__(self, fraction: float = 0.25):
        """Initialize Kelly calculator.

        Args:
            fraction: Kelly fraction to use (0.25 = quarter Kelly for safety)
        """
        self.fraction = fraction

    def calculate(
        self,
        probability: float,
        odds: float,
        bankroll: float
    ) -> float:
        """Calculate Kelly-optimal bet size.

        Args:
            probability: Estimated probability of winning (0-1)
            odds: Decimal odds (e.g., 2.0 for even money)
            bankroll: Total available capital

        Returns:
            Recommended bet size
        """
        if probability <= 0 or probability >= 1:
            return 0

        if odds <= 1:
            return 0

        b = odds - 1
        p = probability
        q = 1 - p

        # Full Kelly fraction
        kelly_fraction = (b * p - q) / b

        # Apply safety fraction and cap at 0
        kelly_fraction = max(0, kelly_fraction * self.fraction)

        # Cap at maximum reasonable bet (e.g., 10% of bankroll)
        kelly_fraction = min(kelly_fraction, 0.1)

        return bankroll * kelly_fraction
