"""Position Tracking and State Management.

Tracks open positions, PnL, and portfolio state for risk management.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Position:
    """Represents an open position in a market."""
    market_id: str
    token_id: str
    side: str  # BUY or SELL
    size: float  # Number of shares
    avg_entry_price: float
    cost_basis: float  # Total USD invested
    opened_at: float
    last_updated: float = field(default_factory=lambda: time.time())

    @property
    def current_value(self) -> float:
        """Estimated current value (assumes same price as entry)."""
        return self.size * self.avg_entry_price

    def update(self, additional_size: float, price: float):
        """Add to position (averaging in)."""
        total_cost = self.cost_basis + (additional_size * price)
        total_size = self.size + additional_size
        self.avg_entry_price = total_cost / total_size if total_size > 0 else 0
        self.size = total_size
        self.cost_basis = total_cost
        self.last_updated = time.time()


@dataclass
class Trade:
    """Represents a completed trade."""
    timestamp: float
    market_id: str
    token_id: str
    side: str
    size: float
    price: float
    fee: float = 0
    pnl: Optional[float] = None


class PositionTracker:
    """Tracks all open positions and trade history."""

    def __init__(self):
        # market_id -> Position
        self._positions: dict[str, Position] = {}
        # List of completed trades
        self._trade_history: list[Trade] = []
        # Realised PnL
        self._realised_pnl: float = 0
        # Total fees paid
        self._total_fees: float = 0

        logger.info("position_tracker_initialized")

    def open_position(
        self,
        market_id: str,
        token_id: str,
        side: str,
        size: float,
        price: float
    ):
        """Open or add to a position."""
        cost = size * price

        if market_id in self._positions:
            # Add to existing position
            position = self._positions[market_id]
            position.update(size, price)
            logger.info(
                "position_increased",
                market=market_id[:10],
                new_size=position.size,
                avg_price=position.avg_entry_price
            )
        else:
            # New position
            self._positions[market_id] = Position(
                market_id=market_id,
                token_id=token_id,
                side=side,
                size=size,
                avg_entry_price=price,
                cost_basis=cost,
                opened_at=time.time()
            )
            logger.info(
                "position_opened",
                market=market_id[:10],
                side=side,
                size=size,
                price=price
            )

        # Record trade
        self._trade_history.append(Trade(
            timestamp=time.time(),
            market_id=market_id,
            token_id=token_id,
            side="BUY" if side == "BUY" else "SELL",
            size=size,
            price=price
        ))

    def close_position(
        self,
        market_id: str,
        size: float,
        price: float,
        fee: float = 0
    ) -> Optional[float]:
        """Close or reduce a position. Returns realised PnL."""
        if market_id not in self._positions:
            logger.warning("no_position_to_close", market=market_id[:10])
            return None

        position = self._positions[market_id]

        # Calculate PnL
        if position.side == "BUY":
            # Bought low, selling high = profit
            pnl = (price - position.avg_entry_price) * size
        else:
            # Sold high, buying back low = profit
            pnl = (position.avg_entry_price - price) * size

        pnl -= fee
        self._realised_pnl += pnl
        self._total_fees += fee

        # Reduce position
        position.size -= size
        position.cost_basis -= (size * position.avg_entry_price)
        position.last_updated = time.time()

        # Record trade
        self._trade_history.append(Trade(
            timestamp=time.time(),
            market_id=market_id,
            token_id=position.token_id,
            side="SELL" if position.side == "BUY" else "BUY",
            size=size,
            price=price,
            fee=fee,
            pnl=pnl
        ))

        # Remove if fully closed
        if position.size <= 0.0001:  # Small threshold for floating point
            del self._positions[market_id]
            logger.info(
                "position_closed",
                market=market_id[:10],
                pnl=pnl
            )
        else:
            logger.info(
                "position_reduced",
                market=market_id[:10],
                remaining_size=position.size,
                pnl=pnl
            )

        return pnl

    def get_position(self, market_id: str) -> Optional[Position]:
        """Get position for a market."""
        return self._positions.get(market_id)

    def get_position_value(self, market_id: str) -> float:
        """Get current position value in USD."""
        position = self._positions.get(market_id)
        if position:
            return position.cost_basis
        return 0

    def get_all_positions(self) -> dict[str, Position]:
        """Get all open positions."""
        return self._positions.copy()

    def get_open_position_count(self) -> int:
        """Get number of open positions."""
        return len(self._positions)

    def get_total_exposure(self) -> float:
        """Get total USD exposure across all positions."""
        return sum(p.cost_basis for p in self._positions.values())

    def get_unrealised_pnl(self, market_prices: dict[str, float]) -> float:
        """Calculate unrealised PnL given current market prices."""
        total = 0
        for market_id, position in self._positions.items():
            current_price = market_prices.get(market_id, position.avg_entry_price)
            if position.side == "BUY":
                total += (current_price - position.avg_entry_price) * position.size
            else:
                total += (position.avg_entry_price - current_price) * position.size
        return total

    def get_metrics(self) -> dict:
        """Get position tracking metrics."""
        return {
            "open_positions": self.get_open_position_count(),
            "total_exposure_usd": self.get_total_exposure(),
            "realised_pnl": self._realised_pnl,
            "total_fees": self._total_fees,
            "trade_count": len(self._trade_history)
        }

    def get_trade_history(self, limit: int = 100) -> list[Trade]:
        """Get recent trade history."""
        return self._trade_history[-limit:]

    def to_dict(self) -> dict:
        """Serialize state for persistence."""
        return {
            "positions": {
                k: {
                    "market_id": v.market_id,
                    "token_id": v.token_id,
                    "side": v.side,
                    "size": v.size,
                    "avg_entry_price": v.avg_entry_price,
                    "cost_basis": v.cost_basis,
                    "opened_at": v.opened_at
                }
                for k, v in self._positions.items()
            },
            "realised_pnl": self._realised_pnl,
            "total_fees": self._total_fees
        }

    def from_dict(self, data: dict):
        """Restore state from persistence."""
        self._realised_pnl = data.get("realised_pnl", 0)
        self._total_fees = data.get("total_fees", 0)

        for market_id, pos_data in data.get("positions", {}).items():
            self._positions[market_id] = Position(
                market_id=pos_data["market_id"],
                token_id=pos_data["token_id"],
                side=pos_data["side"],
                size=pos_data["size"],
                avg_entry_price=pos_data["avg_entry_price"],
                cost_basis=pos_data["cost_basis"],
                opened_at=pos_data["opened_at"]
            )

        logger.info(
            "position_state_restored",
            positions=len(self._positions),
            realised_pnl=self._realised_pnl
        )
