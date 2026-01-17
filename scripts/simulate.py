#!/usr/bin/env python3
"""
Polymarket Copy-Trading Simulator

Monitors whale wallets in real-time and simulates paper trading.
No real trades - just tracks what WOULD happen if you copied them.

Usage: python simulate.py
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

# Check dependencies
try:
    import aiohttp
    import websockets
except ImportError:
    print("Installing dependencies...")
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp", "websockets"])
    import aiohttp
    import websockets


class PaperPortfolio:
    """Simulated portfolio tracker."""

    def __init__(self, starting_balance: float = 1000):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.positions = {}
        self.trades = []
        self.pnl = 0

    def execute_trade(self, market: str, side: str, size: float, price: float, wallet: str):
        """Simulate a trade."""
        fee = size * 0.001  # 0.1% fee

        if side == "BUY":
            cost = size + fee
            if cost > self.balance:
                print(f"  ⚠ Insufficient balance for ${size:.2f} trade")
                return False
            self.balance -= cost
        else:
            self.balance += (size - fee)

        trade = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "wallet": wallet[:10] + "...",
            "market": market[:30] + "..." if len(market) > 30 else market,
            "side": side,
            "size": size,
            "price": price,
            "fee": fee
        }
        self.trades.append(trade)

        print(f"\n  {'🟢' if side == 'BUY' else '🔴'} PAPER TRADE: {side} ${size:.2f} @ {price:.2f}")
        print(f"     Market: {trade['market']}")
        print(f"     Copying: {trade['wallet']}")
        print(f"     Balance: ${self.balance:.2f}")

        return True

    def summary(self):
        """Print portfolio summary."""
        print("\n" + "=" * 60)
        print("PAPER TRADING SUMMARY")
        print("=" * 60)
        print(f"Starting Balance:  ${self.starting_balance:.2f}")
        print(f"Current Balance:   ${self.balance:.2f}")
        print(f"Total Trades:      {len(self.trades)}")
        print(f"P&L:               ${self.balance - self.starting_balance:+.2f}")
        print("=" * 60)

        if self.trades:
            print("\nRecent Trades:")
            for t in self.trades[-10:]:
                print(f"  [{t['time']}] {t['side']} ${t['size']:.2f} - {t['market']}")


class PolymarketMonitor:
    """Monitor Polymarket for whale trades."""

    CLOB_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CLOB_REST = "https://clob.polymarket.com"

    def __init__(self, wallets: list[str], portfolio: PaperPortfolio, copy_ratio: float = 0.1):
        self.wallets = set(w.lower() for w in wallets)
        self.portfolio = portfolio
        self.copy_ratio = copy_ratio
        self.running = False
        self.trades_detected = 0

    async def monitor_websocket(self):
        """Connect to CLOB WebSocket and monitor trades."""
        print(f"\n📡 Connecting to Polymarket WebSocket...")

        while self.running:
            try:
                async with websockets.connect(self.CLOB_WSS, ping_interval=30) as ws:
                    print("✅ Connected to Polymarket CLOB")
                    print(f"👀 Watching {len(self.wallets)} whale wallets...")
                    print("\nWaiting for whale trades...\n")

                    # Subscribe to trades
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": "trades",
                        "markets": []  # All markets
                    }))

                    async for message in ws:
                        await self.handle_message(message)

            except websockets.exceptions.ConnectionClosed:
                print("⚠ WebSocket disconnected, reconnecting...")
                await asyncio.sleep(2)
            except Exception as e:
                print(f"⚠ Error: {e}")
                await asyncio.sleep(5)

    async def handle_message(self, raw_message: str):
        """Process WebSocket message."""
        try:
            msg = json.loads(raw_message)

            # Handle trade messages
            if msg.get("type") == "trade" or "maker" in msg or "taker" in msg:
                maker = msg.get("maker", "").lower()
                taker = msg.get("taker", "").lower()

                target_wallet = None
                if maker in self.wallets:
                    target_wallet = maker
                    side = "SELL"
                elif taker in self.wallets:
                    target_wallet = taker
                    side = "BUY"

                if target_wallet:
                    self.trades_detected += 1
                    size = float(msg.get("size", 0))
                    price = float(msg.get("price", 0.5))
                    market = msg.get("market", "Unknown")

                    # Calculate copy size
                    copy_size = size * self.copy_ratio
                    if copy_size >= 5:  # Minimum $5
                        self.portfolio.execute_trade(
                            market=market,
                            side=side,
                            size=min(copy_size, 100),  # Max $100
                            price=price,
                            wallet=target_wallet
                        )

        except json.JSONDecodeError:
            pass
        except Exception as e:
            pass  # Silently handle parsing errors

    async def monitor_onchain(self):
        """Poll on-chain for transfers (backup detection)."""
        # Simplified - just prints status periodically
        while self.running:
            await asyncio.sleep(60)
            print(f"\n📊 Status: {self.trades_detected} whale trades detected, Balance: ${self.portfolio.balance:.2f}")

    async def run(self):
        """Start monitoring."""
        self.running = True

        tasks = [
            asyncio.create_task(self.monitor_websocket()),
            asyncio.create_task(self.monitor_onchain())
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self.portfolio.summary()


def load_wallets():
    """Load whale wallets from config."""
    config_path = Path(__file__).parent.parent / "config" / "wallets.json"

    try:
        with open(config_path) as f:
            data = json.load(f)
            wallets = [w["address"] for w in data.get("wallets", []) if w.get("enabled", True)]
            print(f"📋 Loaded {len(wallets)} whale wallets to track")
            for w in data.get("wallets", []):
                if w.get("enabled", True):
                    print(f"   • {w.get('name', 'Unknown')}: {w['address'][:16]}...")
            return wallets
    except FileNotFoundError:
        print("⚠ wallets.json not found, using defaults")
        return [
            "0x9d84ce0306f8551e02efef1680475fc0f1dc1344",
            "0xd218e474776403a330142299f7796e8ba32eb5c9"
        ]


async def main():
    print("""
╔═══════════════════════════════════════════════════════════╗
║     POLYMARKET COPY-TRADING SIMULATOR                     ║
║     Paper Trading Mode - No Real Money                    ║
╚═══════════════════════════════════════════════════════════╝
    """)

    # Load config
    wallets = load_wallets()

    # Initialize paper portfolio
    portfolio = PaperPortfolio(starting_balance=1000)
    print(f"\n💰 Paper trading with ${portfolio.balance:.2f} starting balance")

    # Start monitor
    monitor = PolymarketMonitor(
        wallets=wallets,
        portfolio=portfolio,
        copy_ratio=0.1  # Copy 10% of whale trade size
    )

    print("\n" + "-" * 60)
    print("Press Ctrl+C to stop and see results")
    print("-" * 60)

    try:
        await monitor.run()
    except KeyboardInterrupt:
        monitor.running = False
        portfolio.summary()


if __name__ == "__main__":
    asyncio.run(main())
