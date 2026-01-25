#!/usr/bin/env python3
"""Paper Trading Mode.

Simulates order execution without placing real trades.
Useful for testing strategy and validating the pipeline.
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml


@dataclass
class PaperPosition:
    """Simulated position."""
    market_id: str
    token_id: str
    side: str
    size: float
    entry_price: float
    opened_at: float


@dataclass
class PaperTrade:
    """Simulated trade."""
    timestamp: float
    market_id: str
    side: str
    size: float
    price: float
    fee: float
    pnl: float = 0.0  # Set when trade closes a position


class PaperTradingEngine:
    """Simulates trading without real execution."""

    def __init__(self, starting_balance: float = 1000.0):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.positions: dict[str, PaperPosition] = {}
        self.trades: list[PaperTrade] = []
        self.realised_pnl = 0.0
        self.total_fees = 0.0

        # Fee rate (0.1% per side typical for Polymarket)
        self.fee_rate = 0.001

    def execute_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        size: float,
        price: float
    ) -> dict:
        """Simulate order execution."""
        fee = size * self.fee_rate
        net_cost = size + fee if side == "BUY" else -size + fee

        # Check sufficient balance for BUY
        if side == "BUY" and net_cost > self.balance:
            return {
                "success": False,
                "error": f"Insufficient balance: {self.balance:.2f} < {net_cost:.2f}"
            }

        # Update balance
        if side == "BUY":
            self.balance -= net_cost
        else:
            self.balance += (size - fee)

        self.total_fees += fee

        # Calculate PnL if this trade closes/reduces a position
        trade_pnl = 0.0
        if market_id in self.positions:
            pos = self.positions[market_id]
            if pos.side != side:
                # This trade reduces/closes the position - calculate PnL
                close_size = min(size, pos.size)
                trade_pnl = self._calculate_pnl(pos, price, close_size)

        # Record trade with PnL
        trade = PaperTrade(
            timestamp=time.time(),
            market_id=market_id,
            side=side,
            size=size,
            price=price,
            fee=fee,
            pnl=trade_pnl
        )
        self.trades.append(trade)

        # Update position
        if market_id in self.positions:
            pos = self.positions[market_id]
            if pos.side == side:
                # Add to position
                total_cost = pos.size * pos.entry_price + size * price
                pos.size += size
                pos.entry_price = total_cost / pos.size if pos.size > 0 else 0
            else:
                # Reduce or close position
                if size >= pos.size:
                    # Close and potentially open opposite
                    self.realised_pnl += trade_pnl
                    remaining = size - pos.size
                    del self.positions[market_id]
                    if remaining > 0:
                        self.positions[market_id] = PaperPosition(
                            market_id=market_id,
                            token_id=token_id,
                            side=side,
                            size=remaining,
                            entry_price=price,
                            opened_at=time.time()
                        )
                else:
                    self.realised_pnl += trade_pnl
                    pos.size -= size
        else:
            # New position
            self.positions[market_id] = PaperPosition(
                market_id=market_id,
                token_id=token_id,
                side=side,
                size=size,
                entry_price=price,
                opened_at=time.time()
            )

        return {
            "success": True,
            "order_id": f"paper_{int(time.time() * 1000)}",
            "fill_price": price,
            "fill_size": size,
            "fee": fee
        }

    def _calculate_pnl(self, position: PaperPosition, exit_price: float, size: float) -> float:
        """Calculate PnL for closing a position."""
        if position.side == "BUY":
            return (exit_price - position.entry_price) * size
        else:
            return (position.entry_price - exit_price) * size

    def get_portfolio_value(self, current_prices: dict[str, float] = None) -> float:
        """Get total portfolio value including unrealised PnL."""
        value = self.balance

        for market_id, pos in self.positions.items():
            current_price = current_prices.get(market_id, pos.entry_price) if current_prices else pos.entry_price
            position_value = pos.size * current_price
            value += position_value

        return value

    def get_unrealised_pnl(self, current_prices: dict[str, float]) -> float:
        """Get unrealised PnL across all positions."""
        pnl = 0.0
        for market_id, pos in self.positions.items():
            current_price = current_prices.get(market_id, pos.entry_price)
            pnl += self._calculate_pnl(pos, current_price, pos.size)
        return pnl

    def get_stats(self) -> dict:
        """Get trading statistics."""
        wins = sum(1 for t in self.trades if self._get_trade_pnl(t) > 0)
        losses = sum(1 for t in self.trades if self._get_trade_pnl(t) < 0)

        return {
            "starting_balance": self.starting_balance,
            "current_balance": self.balance,
            "portfolio_value": self.get_portfolio_value(),
            "realised_pnl": self.realised_pnl,
            "total_fees": self.total_fees,
            "total_trades": len(self.trades),
            "open_positions": len(self.positions),
            "win_rate": wins / len(self.trades) if self.trades else 0,
            "roi_pct": ((self.get_portfolio_value() - self.starting_balance) / self.starting_balance) * 100
        }

    def _get_trade_pnl(self, trade: PaperTrade) -> float:
        """Get trade PnL (stored when trade closes a position)."""
        return trade.pnl

    def print_summary(self):
        """Print trading summary."""
        stats = self.get_stats()
        print("\n" + "=" * 50)
        print("PAPER TRADING SUMMARY")
        print("=" * 50)
        print(f"Starting Balance:  ${stats['starting_balance']:.2f}")
        print(f"Current Balance:   ${stats['current_balance']:.2f}")
        print(f"Portfolio Value:   ${stats['portfolio_value']:.2f}")
        print(f"Realised PnL:      ${stats['realised_pnl']:.2f}")
        print(f"Total Fees:        ${stats['total_fees']:.2f}")
        print(f"Total Trades:      {stats['total_trades']}")
        print(f"Open Positions:    {stats['open_positions']}")
        print(f"ROI:               {stats['roi_pct']:.2f}%")
        print("=" * 50)


class PaperTradingSimulator:
    """Full paper trading simulation with IPC."""

    def __init__(self, config_path: str = "../config/config.yaml"):
        self.config = self._load_config(config_path)
        self.engine = PaperTradingEngine(
            starting_balance=self.config.get("paper_trading", {}).get("starting_balance_usd", 1000)
        )
        self.running = False

    def _load_config(self, config_path: str) -> dict:
        config_file = Path(__file__).parent.parent / "config" / "config.yaml"
        if not config_file.exists():
            config_file = Path(config_path)
        with open(config_file) as f:
            return yaml.safe_load(f)

    async def run(self):
        """Run paper trading simulation."""
        print("Starting Paper Trading Simulator...")
        print(f"Initial Balance: ${self.engine.starting_balance:.2f}")

        self.running = True
        execution_socket = self.config["ipc"]["execution_socket"]

        # Create execution socket server (replacing real execution)
        await self._start_paper_execution_server(execution_socket)

    async def _start_paper_execution_server(self, socket_path: str):
        """Start IPC server that simulates execution."""
        import os
        from pathlib import Path

        socket_file = Path(socket_path)
        if socket_file.exists():
            socket_file.unlink()
        socket_file.parent.mkdir(parents=True, exist_ok=True)

        server = await asyncio.start_unix_server(
            self._handle_order_request,
            path=socket_path
        )
        os.chmod(socket_path, 0o660)

        print(f"Paper execution server listening on {socket_path}")

        async with server:
            await server.serve_forever()

    async def _handle_order_request(self, reader, writer):
        """Handle incoming order requests from Decision service."""
        print("Decision service connected to paper trader")

        while self.running:
            line = await reader.readline()
            if not line:
                break

            line = line.decode().strip()
            if not line:
                continue

            try:
                request = json.loads(line)
                if request.get("type") == "ORDER_REQUEST":
                    result = self.engine.execute_order(
                        market_id=request.get("market_id", ""),
                        token_id=request.get("token_id", ""),
                        side=request.get("side", "BUY"),
                        size=request.get("size", 0),
                        price=request.get("price", 0.5)
                    )

                    print(f"Paper Trade: {request.get('side')} ${request.get('size'):.2f} @ {request.get('price'):.4f}")
                    print(f"  Result: {'SUCCESS' if result['success'] else 'FAILED'}")
                    print(f"  Balance: ${self.engine.balance:.2f}")

                    response = json.dumps(result) + "\n"
                    writer.write(response.encode())
                    await writer.drain()

            except json.JSONDecodeError:
                print(f"Invalid JSON: {line[:50]}")

        self.engine.print_summary()


async def main():
    """Main entry point."""
    simulator = PaperTradingSimulator()

    # Handle Ctrl+C
    import signal
    def signal_handler(sig, frame):
        simulator.running = False
        simulator.engine.print_summary()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    await simulator.run()


if __name__ == "__main__":
    asyncio.run(main())
