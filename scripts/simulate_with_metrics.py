#!/usr/bin/env python3
"""
Polymarket Copy-Trading Simulator with Real Prices

- Fetches REAL market prices from Polymarket API
- Generates SIMULATED trades to test the system
- Pushes metrics to Prometheus for Grafana dashboard

Usage: python simulate_with_metrics.py
"""

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

# Install dependencies if needed
try:
    import aiohttp
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except ImportError:
    print("Installing dependencies...")
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp", "prometheus_client"])
    import aiohttp
    from prometheus_client import Counter, Gauge, Histogram, start_http_server


# ============== Prometheus Metrics ==============
TRADES_DETECTED = Counter('poly_trades_detected_total', 'Trades detected', ['source', 'wallet'])
TRADES_EXECUTED = Counter('poly_trades_executed_total', 'Trades executed')
ORDERS_SUCCESS = Counter('poly_orders_success_total', 'Successful orders')
ORDERS_FAILED = Counter('poly_orders_failed_total', 'Failed orders')
ORDERS_REJECTED = Counter('poly_orders_rejected_total', 'Rejected orders')
WEBSOCKET_MESSAGES = Counter('poly_websocket_messages_total', 'WebSocket messages')
WEBSOCKET_RECONNECTIONS = Counter('poly_websocket_reconnections_total', 'Reconnections')

OPEN_POSITIONS = Gauge('poly_open_positions', 'Open positions')
TOTAL_EXPOSURE = Gauge('poly_total_exposure_usd', 'Total exposure USD')
DAILY_VOLUME = Gauge('poly_daily_volume_usd', 'Daily volume USD')
REALISED_PNL = Gauge('poly_realised_pnl_usd', 'Realised PnL USD')
BALANCE = Gauge('poly_balance_usd', 'Balance USD')
PORTFOLIO_VALUE = Gauge('poly_portfolio_value_usd', 'Portfolio value USD')
WEBSOCKET_CONNECTED = Gauge('poly_websocket_connected', 'WebSocket connected')
ONCHAIN_BLOCKS = Gauge('poly_onchain_blocks_processed', 'Blocks processed')

EXECUTION_LATENCY = Histogram('poly_execution_latency_ms', 'Execution latency',
                              buckets=[10, 25, 50, 100, 150, 200, 300, 500])


# ============== Simulated Wallets ==============
SIMULATED_WHALES = [
    {"address": "0x9d84ce0306f8551e02efef1680475fc0f1dc1344", "name": "Whale #1 (63% WR)", "skill": 0.63},
    {"address": "0xd218e474776403a330142299f7796e8ba32eb5c9", "name": "Whale #2 (67% WR)", "skill": 0.67},
    {"address": "0x1E76C7fFaEf99438a59F9c7f4276dd4f030F528e", "name": "Theo ($85M)", "skill": 0.72},
]


class RealMarketData:
    """Fetches real market data from Polymarket."""

    CLOB_API = "https://clob.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self):
        self.markets = []
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession()
        await self.refresh_markets()

    async def stop(self):
        if self.session:
            await self.session.close()

    async def refresh_markets(self):
        """Fetch active markets from Polymarket."""
        try:
            # Fetch from gamma API (public markets)
            async with self.session.get(
                f"{self.GAMMA_API}/markets?closed=false&limit=50"
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.markets = [m for m in data if m.get("active", True)]
                    print(f"📊 Loaded {len(self.markets)} active markets from Polymarket")
                    return

            # Fallback: fetch from CLOB
            async with self.session.get(f"{self.CLOB_API}/markets") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.markets = data[:50] if isinstance(data, list) else []
                    print(f"📊 Loaded {len(self.markets)} markets from CLOB")
        except Exception as e:
            print(f"⚠ Failed to fetch markets: {e}")
            self.markets = self._fallback_markets()

    def _fallback_markets(self):
        """Fallback sample markets if API fails."""
        return [
            {"question": "Will BTC exceed $100,000 in 2025?", "outcomePrices": ["0.65", "0.35"]},
            {"question": "Will there be a US recession in 2025?", "outcomePrices": ["0.25", "0.75"]},
            {"question": "Will AI pass the Turing test by 2026?", "outcomePrices": ["0.45", "0.55"]},
            {"question": "Will SpaceX land humans on Mars by 2030?", "outcomePrices": ["0.15", "0.85"]},
            {"question": "Will the Fed cut rates in Q1 2025?", "outcomePrices": ["0.72", "0.28"]},
        ]

    def get_random_market(self):
        """Get a random market with real price."""
        if not self.markets:
            return None

        market = random.choice(self.markets)
        question = market.get("question", market.get("title", "Unknown Market"))

        # Get price
        prices = market.get("outcomePrices", market.get("outcomes", ["0.5", "0.5"]))
        if isinstance(prices, list) and len(prices) > 0:
            try:
                price = float(prices[0]) if isinstance(prices[0], (str, int, float)) else 0.5
            except:
                price = 0.5
        else:
            price = 0.5

        # Ensure price is valid
        price = max(0.01, min(0.99, price))

        return {
            "question": question[:60],
            "price": price,
            "token_id": market.get("conditionId", market.get("id", "unknown"))
        }


class PaperPortfolio:
    """Simulated portfolio with metrics export."""

    def __init__(self, starting_balance: float = 1000):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.positions = {}
        self.trades = []
        self.realised_pnl = 0
        self.daily_volume = 0

        # Initialize metrics
        BALANCE.set(self.balance)
        PORTFOLIO_VALUE.set(self.balance)
        OPEN_POSITIONS.set(0)
        DAILY_VOLUME.set(0)
        REALISED_PNL.set(0)

    def execute_trade(self, market: str, side: str, size: float, price: float,
                      wallet: str, token_id: str) -> bool:
        """Execute a simulated trade."""
        start_time = time.time()
        fee = size * 0.001

        # Simulate some latency
        latency = random.uniform(50, 200)
        EXECUTION_LATENCY.observe(latency)

        if side == "BUY":
            cost = size + fee
            if cost > self.balance:
                ORDERS_REJECTED.inc()
                return False
            self.balance -= cost

            # Track position
            if token_id not in self.positions:
                self.positions[token_id] = {"size": 0, "avg_price": 0, "market": market}
            pos = self.positions[token_id]
            total_cost = pos["size"] * pos["avg_price"] + size * price
            pos["size"] += size
            pos["avg_price"] = total_cost / pos["size"] if pos["size"] > 0 else price

        else:  # SELL
            if token_id in self.positions:
                pos = self.positions[token_id]
                pnl = (price - pos["avg_price"]) * min(size, pos["size"])
                self.realised_pnl += pnl
                pos["size"] -= size
                if pos["size"] <= 0:
                    del self.positions[token_id]
            self.balance += (size - fee)

        # Record trade
        self.trades.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "wallet": wallet,
            "market": market,
            "side": side,
            "size": size,
            "price": price,
            "latency": latency
        })

        self.daily_volume += size

        # Update metrics
        TRADES_EXECUTED.inc()
        ORDERS_SUCCESS.inc()
        BALANCE.set(self.balance)
        DAILY_VOLUME.set(self.daily_volume)
        REALISED_PNL.set(self.realised_pnl)
        OPEN_POSITIONS.set(len(self.positions))
        TOTAL_EXPOSURE.set(sum(p["size"] * p["avg_price"] for p in self.positions.values()))
        PORTFOLIO_VALUE.set(self.balance + sum(p["size"] * p["avg_price"] for p in self.positions.values()))

        return True

    def summary(self):
        """Print summary."""
        portfolio_value = self.balance + sum(p["size"] * p["avg_price"] for p in self.positions.values())
        roi = ((portfolio_value - self.starting_balance) / self.starting_balance) * 100

        print("\n" + "=" * 60)
        print("PAPER TRADING SUMMARY")
        print("=" * 60)
        print(f"Starting Balance:  ${self.starting_balance:.2f}")
        print(f"Current Balance:   ${self.balance:.2f}")
        print(f"Open Positions:    {len(self.positions)}")
        print(f"Portfolio Value:   ${portfolio_value:.2f}")
        print(f"Realised P&L:      ${self.realised_pnl:+.2f}")
        print(f"Daily Volume:      ${self.daily_volume:.2f}")
        print(f"Total Trades:      {len(self.trades)}")
        print(f"ROI:               {roi:+.2f}%")
        print("=" * 60)


class TradeSimulator:
    """Generates simulated whale trades using real market data."""

    def __init__(self, market_data: RealMarketData, portfolio: PaperPortfolio):
        self.market_data = market_data
        self.portfolio = portfolio
        self.running = False
        self.trade_count = 0

    async def run(self):
        """Generate trades at random intervals."""
        self.running = True
        WEBSOCKET_CONNECTED.set(1)

        print("\n🚀 Starting trade simulation with REAL Polymarket prices...")
        print("=" * 60)

        block = 50000000
        while self.running:
            # Simulate WebSocket activity
            WEBSOCKET_MESSAGES.inc()
            ONCHAIN_BLOCKS.set(block)
            block += 1

            # Random interval between trades (5-30 seconds for demo)
            await asyncio.sleep(random.uniform(5, 20))

            # Refresh markets occasionally
            if random.random() < 0.1:
                await self.market_data.refresh_markets()

            # Generate a whale trade
            await self.generate_whale_trade()

    async def generate_whale_trade(self):
        """Simulate a whale making a trade on a real market."""
        market = self.market_data.get_random_market()
        if not market:
            return

        # Pick a random whale
        whale = random.choice(SIMULATED_WHALES)

        # Whale trade size ($500-$5000)
        whale_size = random.uniform(500, 5000)

        # Decide side based on whale skill and price
        # Good whales buy underpriced (price < 0.5) more often
        if market["price"] < 0.5:
            side = "BUY" if random.random() < whale["skill"] else "SELL"
        else:
            side = "SELL" if random.random() < whale["skill"] else "BUY"

        # Calculate copy size (10% of whale, max $100)
        copy_size = min(whale_size * 0.1, 100)
        copy_size = max(copy_size, 10)  # Min $10

        # Record detection
        TRADES_DETECTED.labels(source="CLOB", wallet=whale["address"][:10]).inc()
        self.trade_count += 1

        # Print whale activity
        print(f"\n{'🟢' if side == 'BUY' else '🔴'} WHALE DETECTED: {whale['name']}")
        print(f"   Market: {market['question']}")
        print(f"   Price:  ${market['price']:.2f} (REAL from Polymarket)")
        print(f"   Whale:  {side} ${whale_size:.0f}")
        print(f"   Copy:   {side} ${copy_size:.2f}")

        # Execute paper trade
        success = self.portfolio.execute_trade(
            market=market["question"],
            side=side,
            size=copy_size,
            price=market["price"],
            wallet=whale["name"],
            token_id=market["token_id"]
        )

        if success:
            print(f"   ✅ Executed | Balance: ${self.portfolio.balance:.2f} | P&L: ${self.portfolio.realised_pnl:+.2f}")
        else:
            print(f"   ❌ Rejected (insufficient balance)")

        # Sometimes close positions
        if random.random() < 0.3 and self.portfolio.positions:
            await self.close_random_position()

    async def close_random_position(self):
        """Randomly close a position to realize P&L."""
        if not self.portfolio.positions:
            return

        token_id = random.choice(list(self.portfolio.positions.keys()))
        pos = self.portfolio.positions[token_id]

        # Simulate price movement based on whale skill
        whale = random.choice(SIMULATED_WHALES)
        if random.random() < whale["skill"]:
            # Profitable exit
            new_price = pos["avg_price"] * random.uniform(1.05, 1.30)
        else:
            # Loss
            new_price = pos["avg_price"] * random.uniform(0.70, 0.95)

        new_price = max(0.01, min(0.99, new_price))

        print(f"\n📤 CLOSING POSITION: {pos['market'][:40]}...")
        print(f"   Entry: ${pos['avg_price']:.2f} → Exit: ${new_price:.2f}")

        self.portfolio.execute_trade(
            market=pos["market"],
            side="SELL",
            size=pos["size"] * pos["avg_price"],
            price=new_price,
            wallet="Portfolio",
            token_id=token_id
        )


async def main():
    print("""
╔═══════════════════════════════════════════════════════════════╗
║   POLYMARKET COPY-TRADING SIMULATOR                           ║
║   Real Prices • Simulated Trades • Live Dashboard             ║
╚═══════════════════════════════════════════════════════════════╝
    """)

    # Start Prometheus metrics server
    print("📊 Starting metrics server on http://localhost:9091")
    start_http_server(9091)

    # Initialize components
    market_data = RealMarketData()
    await market_data.start()

    portfolio = PaperPortfolio(starting_balance=1000)
    simulator = TradeSimulator(market_data, portfolio)

    print("\n📈 View dashboard at: http://localhost:3001")
    print("   (Make sure Grafana is running: docker compose up -d grafana prometheus)")
    print("\n" + "-" * 60)
    print("Press Ctrl+C to stop")
    print("-" * 60)

    try:
        await simulator.run()
    except KeyboardInterrupt:
        simulator.running = False
        WEBSOCKET_CONNECTED.set(0)
        await market_data.stop()
        portfolio.summary()


if __name__ == "__main__":
    asyncio.run(main())
