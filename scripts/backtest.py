#!/usr/bin/env python3
"""Backtesting Framework.

Tests copy-trading strategy against historical data.
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class HistoricalTrade:
    """Historical trade data point."""
    timestamp: float
    wallet: str
    market_id: str
    token_id: str
    side: str
    size: float
    price: float


@dataclass
class BacktestResult:
    """Result of a backtest run."""
    start_date: str
    end_date: str
    starting_balance: float
    ending_balance: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_fees: float
    avg_trade_size: float
    avg_hold_time_hours: float


class BacktestEngine:
    """Backtesting engine for copy-trading strategy."""

    def __init__(self, config_path: str = "../config/config.yaml"):
        self.config = self._load_config(config_path)

        # Simulation state
        self.balance = 0.0
        self.starting_balance = 0.0
        self.positions = {}
        self.trades = []
        self.equity_curve = []

        # Strategy parameters
        self.sizing_strategy = self.config["decision"]["sizing_strategy"]
        self.risk_config = self.config["risk"]

    def _load_config(self, config_path: str) -> dict:
        config_file = Path(__file__).parent.parent / "config" / "config.yaml"
        if not config_file.exists():
            config_file = Path(config_path)
        with open(config_file) as f:
            return yaml.safe_load(f)

    def run_backtest(
        self,
        historical_trades: list[HistoricalTrade],
        starting_balance: float = 1000.0,
        market_prices: Optional[dict] = None
    ) -> BacktestResult:
        """Run backtest on historical trade data.

        Args:
            historical_trades: List of historical trades to simulate
            starting_balance: Starting balance in USD
            market_prices: Optional dict of market_id -> final price for PnL calc

        Returns:
            BacktestResult with performance metrics
        """
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions = {}
        self.trades = []
        self.equity_curve = [(historical_trades[0].timestamp if historical_trades else 0, starting_balance)]

        total_fees = 0.0
        fee_rate = 0.001  # 0.1% per side

        for trade in sorted(historical_trades, key=lambda x: x.timestamp):
            # Calculate copy size based on strategy
            copy_size = self._calculate_copy_size(trade)

            if copy_size <= 0:
                continue

            # Risk checks
            if not self._passes_risk_checks(trade.market_id, copy_size):
                continue

            # Execute simulated trade
            fee = copy_size * fee_rate
            total_fees += fee

            if trade.side == "BUY":
                if self.balance < copy_size + fee:
                    continue
                self.balance -= (copy_size + fee)
            else:
                self.balance += (copy_size - fee)

            # Track position
            self._update_position(trade, copy_size)

            # Record trade
            self.trades.append({
                "timestamp": trade.timestamp,
                "market_id": trade.market_id,
                "side": trade.side,
                "size": copy_size,
                "price": trade.price,
                "fee": fee
            })

            # Update equity curve
            self.equity_curve.append((trade.timestamp, self._calculate_equity(trade.price)))

        # Calculate final metrics
        ending_balance = self._calculate_equity(0.5)  # Assume 0.5 if no prices
        total_return = ((ending_balance - starting_balance) / starting_balance) * 100

        winning = sum(1 for t in self.trades if self._is_winning_trade(t))
        losing = len(self.trades) - winning

        return BacktestResult(
            start_date=datetime.fromtimestamp(historical_trades[0].timestamp).isoformat() if historical_trades else "",
            end_date=datetime.fromtimestamp(historical_trades[-1].timestamp).isoformat() if historical_trades else "",
            starting_balance=starting_balance,
            ending_balance=ending_balance,
            total_return_pct=total_return,
            total_trades=len(self.trades),
            winning_trades=winning,
            losing_trades=losing,
            win_rate=winning / len(self.trades) if self.trades else 0,
            max_drawdown_pct=self._calculate_max_drawdown(),
            sharpe_ratio=self._calculate_sharpe_ratio(),
            total_fees=total_fees,
            avg_trade_size=sum(t["size"] for t in self.trades) / len(self.trades) if self.trades else 0,
            avg_hold_time_hours=0  # Would need exit tracking
        )

    def _calculate_copy_size(self, trade: HistoricalTrade) -> float:
        """Calculate position size to copy."""
        if self.sizing_strategy == "PERCENTAGE":
            base_pct = self.config["decision"]["percentage"]["base_pct"] / 100
            return trade.size * base_pct
        elif self.sizing_strategy == "FIXED":
            return self.config["decision"]["fixed"]["amount_usd"]
        else:
            # Adaptive - simplified
            return trade.size * 0.1

    def _passes_risk_checks(self, market_id: str, size: float) -> bool:
        """Check if trade passes risk filters."""
        if size < self.risk_config["min_order_usd"]:
            return False
        if size > self.risk_config["max_order_usd"]:
            return False
        return True

    def _update_position(self, trade: HistoricalTrade, size: float):
        """Update position tracking."""
        market_id = trade.market_id

        if market_id not in self.positions:
            self.positions[market_id] = {
                "side": trade.side,
                "size": size,
                "avg_price": trade.price,
                "opened_at": trade.timestamp
            }
        else:
            pos = self.positions[market_id]
            if pos["side"] == trade.side:
                # Add to position
                total_cost = pos["size"] * pos["avg_price"] + size * trade.price
                pos["size"] += size
                pos["avg_price"] = total_cost / pos["size"]
            else:
                # Reduce position
                pos["size"] -= size
                if pos["size"] <= 0:
                    del self.positions[market_id]

    def _calculate_equity(self, default_price: float) -> float:
        """Calculate total equity including open positions."""
        equity = self.balance
        for pos in self.positions.values():
            equity += pos["size"] * pos["avg_price"]
        return equity

    def _is_winning_trade(self, trade: dict) -> bool:
        """Determine if trade was profitable (simplified)."""
        return trade["side"] == "BUY" and trade["price"] < 0.5

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from equity curve."""
        if not self.equity_curve:
            return 0

        peak = self.equity_curve[0][1]
        max_dd = 0

        for _, equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)

        return max_dd

    def _calculate_sharpe_ratio(self) -> float:
        """Calculate Sharpe ratio (simplified)."""
        if len(self.equity_curve) < 2:
            return 0

        returns = []
        for i in range(1, len(self.equity_curve)):
            ret = (self.equity_curve[i][1] - self.equity_curve[i-1][1]) / self.equity_curve[i-1][1]
            returns.append(ret)

        if not returns:
            return 0

        avg_return = sum(returns) / len(returns)
        std_dev = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5

        if std_dev == 0:
            return 0

        # Annualised (assuming daily data)
        return (avg_return * 365) / (std_dev * (365 ** 0.5))

    def print_results(self, result: BacktestResult):
        """Print backtest results."""
        print("\n" + "=" * 60)
        print("BACKTEST RESULTS")
        print("=" * 60)
        print(f"Period:            {result.start_date} to {result.end_date}")
        print(f"Starting Balance:  ${result.starting_balance:,.2f}")
        print(f"Ending Balance:    ${result.ending_balance:,.2f}")
        print(f"Total Return:      {result.total_return_pct:+.2f}%")
        print("-" * 60)
        print(f"Total Trades:      {result.total_trades}")
        print(f"Winning Trades:    {result.winning_trades}")
        print(f"Losing Trades:     {result.losing_trades}")
        print(f"Win Rate:          {result.win_rate*100:.1f}%")
        print("-" * 60)
        print(f"Max Drawdown:      {result.max_drawdown_pct:.2f}%")
        print(f"Sharpe Ratio:      {result.sharpe_ratio:.2f}")
        print(f"Total Fees:        ${result.total_fees:,.2f}")
        print(f"Avg Trade Size:    ${result.avg_trade_size:,.2f}")
        print("=" * 60)


def generate_sample_data(num_trades: int = 100) -> list[HistoricalTrade]:
    """Generate sample historical data for testing."""
    import random

    trades = []
    base_time = datetime.now() - timedelta(days=30)

    wallets = [
        "0x1234567890123456789012345678901234567890",
        "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
    ]

    markets = [
        "market_001",
        "market_002",
        "market_003"
    ]

    for i in range(num_trades):
        trade = HistoricalTrade(
            timestamp=(base_time + timedelta(hours=i * 8)).timestamp(),
            wallet=random.choice(wallets),
            market_id=random.choice(markets),
            token_id=f"token_{random.randint(1, 1000)}",
            side=random.choice(["BUY", "SELL"]),
            size=random.uniform(50, 500),
            price=random.uniform(0.3, 0.7)
        )
        trades.append(trade)

    return trades


def main():
    """Run backtest with sample data."""
    print("Generating sample historical data...")
    historical_data = generate_sample_data(200)

    print(f"Running backtest with {len(historical_data)} trades...")
    engine = BacktestEngine()
    result = engine.run_backtest(historical_data, starting_balance=1000)

    engine.print_results(result)


if __name__ == "__main__":
    main()
