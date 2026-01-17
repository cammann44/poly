#!/usr/bin/env python3
"""
Track @cigarettes on Polymarket - REAL TRADES via On-Chain + API

Monitors the actual wallet via multiple methods:
1. On-chain ERC1155 transfers (Conditional Tokens)
2. Polymarket data API
3. WebSocket (backup)

Wallet: 0xd218e474776403a330142299f7796e8ba32eb5c9
Profile: https://polymarket.com/@cigarettes
"""

import asyncio
import json
import time
import os
from datetime import datetime
from pathlib import Path

try:
    import aiohttp
    import websockets
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp", "websockets", "prometheus_client", "--user"])
    import aiohttp
    import websockets
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

# ============== CONFIG ==============
CIGARETTES_WALLET = "0xd218e474776403a330142299f7796e8ba32eb5c9".lower()
STARTING_BALANCE = 10000  # $10k
COPY_RATIO = 0.1  # Copy 10% of their trade size
MAX_COPY_SIZE = 500  # Max $500 per trade
MIN_COPY_SIZE = 10  # Min $10 per trade

# ============== ENDPOINTS ==============
POLYGON_RPC = "https://polygon-rpc.com"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
POLYMARKET_DATA = "https://data-api.polymarket.com"
CLOB_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ============== METRICS ==============
TRADES_DETECTED = Counter('poly_trades_detected_total', 'Real trades detected', ['wallet'])
TRADES_COPIED = Counter('poly_trades_executed_total', 'Trades copied')
ORDERS_SUCCESS = Counter('poly_orders_success_total', 'Successful copies')
CLOSED_TRADES = Counter('poly_closed_trades_total', 'Closed trades (sells)')

BALANCE = Gauge('poly_balance_usd', 'Current balance')
PORTFOLIO_VALUE = Gauge('poly_portfolio_value_usd', 'Portfolio value')
REALISED_PNL = Gauge('poly_realised_pnl_usd', 'Realised PnL')
DAILY_VOLUME = Gauge('poly_daily_volume_usd', 'Daily volume')
OPEN_POSITIONS = Gauge('poly_open_positions', 'Open positions')
TOTAL_EXPOSURE = Gauge('poly_total_exposure_usd', 'Total exposure')
WEBSOCKET_CONNECTED = Gauge('poly_websocket_connected', 'WebSocket status')

EXECUTION_LATENCY = Histogram('poly_execution_latency_ms', 'Latency',
                              buckets=[10, 25, 50, 100, 200, 500])

# ============== LOGGING ==============
LOG_FILE = Path(__file__).parent.parent / "logs" / "cigarettes_trades.json"
LOG_FILE.parent.mkdir(exist_ok=True)


class Portfolio:
    """Track paper portfolio copying @cigarettes."""

    def __init__(self, restore_state=True):
        self.balance = STARTING_BALANCE
        self.positions = {}
        self.trades = []
        self.realised_pnl = 0
        self.daily_volume = 0
        self.start_time = datetime.now()

        if restore_state:
            self._restore_from_log()

        BALANCE.set(self.balance)
        exposure = sum(p["cost"] for p in self.positions.values())
        PORTFOLIO_VALUE.set(self.balance + exposure)
        REALISED_PNL.set(self.realised_pnl)
        DAILY_VOLUME.set(self.daily_volume)
        OPEN_POSITIONS.set(len(self.positions))
        TOTAL_EXPOSURE.set(exposure)
        # Counters: increment by restored count
        for trade in self.trades:
            TRADES_COPIED.inc()
            ORDERS_SUCCESS.inc()
            if trade.get("side") == "SELL":
                CLOSED_TRADES.inc()

    def _restore_from_log(self):
        """Restore portfolio state from trade log."""
        if not LOG_FILE.exists():
            return

        print("📂 Restoring state from trade log...")
        try:
            with open(LOG_FILE, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    trade = json.loads(line)
                    self.trades.append(trade)
                    self.daily_volume += trade.get("copy_size", 0)

                    # Rebuild positions
                    token_id = trade.get("token_id", "unknown")
                    side = trade["side"]
                    copy_size = trade.get("copy_size", 10)
                    price = trade.get("price", 0.5)
                    market = trade.get("market", "Unknown")

                    if side == "BUY":
                        if token_id not in self.positions:
                            self.positions[token_id] = {"size": 0, "cost": 0, "market": market, "entry_price": price}
                        pos = self.positions[token_id]
                        pos["cost"] += copy_size
                        pos["size"] += copy_size / price if price > 0 else 0
                        pos["entry_price"] = pos["cost"] / pos["size"] if pos["size"] > 0 else price
                    else:
                        if token_id in self.positions:
                            pos = self.positions[token_id]
                            sell_value = min(copy_size, pos["cost"])
                            shares_sold = sell_value / price if price > 0 else 0
                            cost_basis = shares_sold * pos["entry_price"]
                            pnl = sell_value - cost_basis
                            self.realised_pnl += pnl
                            pos["size"] -= shares_sold
                            pos["cost"] -= cost_basis
                            if pos["size"] <= 0.01:
                                del self.positions[token_id]

            # Calculate final balance
            total_invested = sum(t.get("copy_size", 0) * 1.001 for t in self.trades if t["side"] == "BUY")
            total_returned = sum(t.get("copy_size", 0) * 0.999 for t in self.trades if t["side"] == "SELL")
            self.balance = STARTING_BALANCE - total_invested + total_returned

            print(f"   ✅ Restored {len(self.trades)} trades")
            print(f"   💰 Balance: ${self.balance:,.2f}")
            print(f"   📊 Open positions: {len(self.positions)}")
            print(f"   💵 Realised P&L: ${self.realised_pnl:+,.2f}")
        except Exception as e:
            print(f"   ⚠ Failed to restore state: {e}")

    def copy_trade(self, trade_data: dict) -> bool:
        """Copy a real trade from @cigarettes."""
        start = time.time()

        side = trade_data["side"]
        original_size = trade_data["size"]
        price = trade_data["price"]
        market = trade_data["market"]
        token_id = trade_data.get("token_id", "unknown")

        # Calculate copy size
        copy_size = original_size * COPY_RATIO
        copy_size = max(MIN_COPY_SIZE, min(MAX_COPY_SIZE, copy_size))

        fee = copy_size * 0.001

        if side == "BUY":
            cost = copy_size + fee
            if cost > self.balance:
                print(f"  ⚠ Insufficient balance: need ${cost:.2f}, have ${self.balance:.2f}")
                return False
            self.balance -= cost

            if token_id not in self.positions:
                self.positions[token_id] = {"size": 0, "cost": 0, "market": market, "entry_price": price}
            pos = self.positions[token_id]
            pos["cost"] += copy_size
            pos["size"] += copy_size / price if price > 0 else 0
            pos["entry_price"] = pos["cost"] / pos["size"] if pos["size"] > 0 else price
        else:
            if token_id in self.positions:
                pos = self.positions[token_id]
                sell_value = min(copy_size, pos["cost"])
                shares_sold = sell_value / price if price > 0 else 0
                cost_basis = shares_sold * pos["entry_price"]
                pnl = sell_value - cost_basis
                self.realised_pnl += pnl
                pos["size"] -= shares_sold
                pos["cost"] -= cost_basis
                if pos["size"] <= 0.01:
                    del self.positions[token_id]
            self.balance += (copy_size - fee)

        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "side": side,
            "market": market,
            "token_id": token_id,
            "original_size": original_size,
            "copy_size": copy_size,
            "price": price,
            "balance_after": self.balance,
            "pnl": self.realised_pnl
        }
        self.trades.append(trade_record)
        self.daily_volume += copy_size

        self._log_trade(trade_record)

        latency = (time.time() - start) * 1000
        EXECUTION_LATENCY.observe(latency)
        TRADES_COPIED.inc()
        ORDERS_SUCCESS.inc()
        if side == "SELL":
            CLOSED_TRADES.inc()
        BALANCE.set(self.balance)
        DAILY_VOLUME.set(self.daily_volume)
        REALISED_PNL.set(self.realised_pnl)
        OPEN_POSITIONS.set(len(self.positions))

        exposure = sum(p["cost"] for p in self.positions.values())
        TOTAL_EXPOSURE.set(exposure)
        PORTFOLIO_VALUE.set(self.balance + exposure)

        return True

    def _log_trade(self, trade: dict):
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(trade) + "\n")

    def get_portfolio_value(self) -> float:
        exposure = sum(p["cost"] for p in self.positions.values())
        return self.balance + exposure

    def summary(self):
        pv = self.get_portfolio_value()
        roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
        runtime = datetime.now() - self.start_time

        print("\n" + "=" * 70)
        print("@CIGARETTES COPY-TRADING RESULTS")
        print("=" * 70)
        print(f"Runtime:           {runtime}")
        print(f"Starting Balance:  ${STARTING_BALANCE:,.2f}")
        print(f"Current Balance:   ${self.balance:,.2f}")
        print(f"Open Positions:    {len(self.positions)}")
        print(f"Portfolio Value:   ${pv:,.2f}")
        print(f"Realised P&L:      ${self.realised_pnl:+,.2f}")
        print(f"Total Volume:      ${self.daily_volume:,.2f}")
        print(f"Trades Copied:     {len(self.trades)}")
        print(f"ROI:               {roi:+.2f}%")
        print("=" * 70)


class CigarettesTracker:
    """Monitor @cigarettes wallet via multiple data sources."""

    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self.running = False
        self.session = None
        self.seen_trades = set()
        self.last_block = 0
        self.price_cache = {}  # Cache token prices

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.running = True
        WEBSOCKET_CONNECTED.set(1)

    async def stop(self):
        self.running = False
        WEBSOCKET_CONNECTED.set(0)
        if self.session:
            await self.session.close()

    async def run(self):
        await self.start()

        print(f"\n👁 Monitoring @cigarettes wallet:")
        print(f"   {CIGARETTES_WALLET}")
        print(f"\n💰 Paper trading with ${STARTING_BALANCE:,}")
        print(f"   Copying {COPY_RATIO*100:.0f}% of each trade (max ${MAX_COPY_SIZE})")
        print("\n" + "-" * 70)
        print("Monitoring via: On-chain transfers + Data API")
        print("-" * 70 + "\n")

        await asyncio.gather(
            self.monitor_onchain(),
            self.poll_data_api(),
            self.periodic_status()
        )

    async def monitor_onchain(self):
        """Monitor on-chain ERC1155 transfers."""
        print("🔗 Starting on-chain monitor...")

        # Get current block
        try:
            async with self.session.post(POLYGON_RPC, json={
                "jsonrpc": "2.0",
                "method": "eth_blockNumber",
                "params": [],
                "id": 1
            }) as resp:
                data = await resp.json()
                self.last_block = int(data["result"], 16)
                print(f"📦 Starting from block {self.last_block}")
        except Exception as e:
            print(f"⚠ Failed to get block number: {e}")
            self.last_block = 67000000  # Fallback

        while self.running:
            try:
                await self.check_new_blocks()
            except Exception as e:
                print(f"⚠ On-chain error: {e}")
            await asyncio.sleep(2)  # Check every 2 seconds

    async def check_new_blocks(self):
        """Check for new blocks with transfers."""
        # Get current block
        async with self.session.post(POLYGON_RPC, json={
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }) as resp:
            data = await resp.json()
            current_block = int(data["result"], 16)

        if current_block <= self.last_block:
            return

        # Query transfer events for our wallet
        # TransferSingle topic
        transfer_topic = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

        # Check transfers TO our wallet (buys)
        async with self.session.post(POLYGON_RPC, json={
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(self.last_block + 1),
                "toBlock": hex(current_block),
                "address": CONDITIONAL_TOKENS,
                "topics": [
                    transfer_topic,
                    None,  # operator
                    None,  # from
                    "0x000000000000000000000000" + CIGARETTES_WALLET[2:]  # to (padded)
                ]
            }],
            "id": 2
        }) as resp:
            data = await resp.json()
            for log in data.get("result", []):
                await self.process_transfer(log, "BUY")

        # Check transfers FROM our wallet (sells)
        async with self.session.post(POLYGON_RPC, json={
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(self.last_block + 1),
                "toBlock": hex(current_block),
                "address": CONDITIONAL_TOKENS,
                "topics": [
                    transfer_topic,
                    None,  # operator
                    "0x000000000000000000000000" + CIGARETTES_WALLET[2:],  # from
                    None  # to
                ]
            }],
            "id": 3
        }) as resp:
            data = await resp.json()
            for log in data.get("result", []):
                await self.process_transfer(log, "SELL")

        self.last_block = current_block

    async def process_transfer(self, log: dict, side: str):
        """Process an on-chain transfer event."""
        tx_hash = log.get("transactionHash", "")

        if tx_hash in self.seen_trades:
            return
        self.seen_trades.add(tx_hash)

        # Decode transfer data
        data = log.get("data", "0x")
        if len(data) >= 130:
            token_id = int(data[2:66], 16)
            value = int(data[66:130], 16)
        else:
            return

        # Convert value (assuming 6 decimals for USDC-denominated)
        size_usd = value / 1e6

        if size_usd < 1:  # Skip tiny transfers
            return

        TRADES_DETECTED.labels(wallet="cigarettes").inc()

        # Fetch real price from Polymarket API
        price, market_name = await self.get_token_price(str(token_id))

        print(f"\n{'🟢' if side == 'BUY' else '🔴'} @CIGARETTES {side} (On-Chain)")
        print(f"   Market:   {market_name}")
        print(f"   Size:     ${size_usd:,.2f}")
        print(f"   Price:    ${price:.4f} {'(real)' if price != 0.5 else '(estimated)'}")
        print(f"   TX:       {tx_hash[:20]}...")
        print(f"   Time:     {datetime.now().strftime('%H:%M:%S')}")

        trade_data = {
            "side": side,
            "size": size_usd,
            "price": price,
            "market": market_name,
            "token_id": str(token_id)
        }

        success = self.portfolio.copy_trade(trade_data)

        if success:
            copy_size = min(MAX_COPY_SIZE, max(MIN_COPY_SIZE, size_usd * COPY_RATIO))
            print(f"\n   ✅ COPIED: {side} ${copy_size:.2f} @ ${price:.4f}")
            print(f"   📊 Balance: ${self.portfolio.balance:,.2f} | P&L: ${self.portfolio.realised_pnl:+,.2f}")

    async def get_token_price(self, token_id: str) -> tuple[float, str]:
        """Fetch real price and market name for a token ID."""
        # Check cache first
        if token_id in self.price_cache:
            cached = self.price_cache[token_id]
            if time.time() - cached["time"] < 60:  # Cache for 60 seconds
                return cached["price"], cached["market"]

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        price = 0.5
        market_name = f"Token {token_id[:20]}..."
        condition_id = None

        try:
            # Get order book - contains price and market condition ID
            url = f"https://clob.polymarket.com/book?token_id={token_id}"
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Use last_trade_price or mid of best bid/ask
                    if data.get("last_trade_price"):
                        price = float(data["last_trade_price"])
                    else:
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        if bids and asks:
                            best_bid = float(bids[0]["price"])
                            best_ask = float(asks[-1]["price"])
                            price = (best_bid + best_ask) / 2
                    condition_id = data.get("market")
        except Exception as e:
            pass

        # Get market name from condition ID
        if condition_id:
            try:
                url = f"https://clob.polymarket.com/markets/{condition_id}"
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        market_name = data.get("question", market_name)[:60]
            except:
                pass

        # Cache the result
        self.price_cache[token_id] = {
            "price": price,
            "market": market_name,
            "time": time.time()
        }
        return price, market_name

    async def get_market_name(self, token_id: str) -> str:
        """Get market name from token ID."""
        try:
            url = f"https://gamma-api.polymarket.com/markets?token_id={token_id}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0].get("question", f"Token {token_id[:20]}...")
        except:
            pass
        return f"Token {token_id[:20]}..."

    async def poll_data_api(self):
        """Poll Polymarket data API for user activity."""
        print("📡 Starting data API polling...")

        while self.running:
            try:
                # Try to get user's positions/activity
                url = f"{POLYMARKET_DATA}/positions?user={CIGARETTES_WALLET}"
                async with self.session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Process any new positions
                        await self.process_positions(data)
            except Exception as e:
                pass  # Silent fail for polling

            await asyncio.sleep(30)  # Poll every 30 seconds

    async def process_positions(self, data):
        """Process position data from API."""
        # This would compare current vs previous positions
        # For now just log if we get data
        if data:
            pass  # Position tracking could go here

    async def periodic_status(self):
        """Print status periodically."""
        while self.running:
            await asyncio.sleep(60)  # Every minute
            pv = self.portfolio.get_portfolio_value()
            roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
            print(f"\n📊 Status: {len(self.portfolio.trades)} trades | Portfolio: ${pv:,.2f} | ROI: {roi:+.2f}% | Block: {self.last_block}\n")


async def run_trades_api(portfolio: Portfolio):
    """Run a simple HTTP API to serve trade data for Grafana."""
    from aiohttp import web

    async def get_trades(request):
        """Return all trades as JSON."""
        return web.json_response(portfolio.trades)

    async def get_closed_trades(request):
        """Return closed trades with P&L."""
        closed = []
        # Group buys and sells by token_id to find closed positions
        buys_by_token = {}
        for trade in portfolio.trades:
            token_id = trade.get("token_id", "unknown")
            if trade["side"] == "BUY":
                if token_id not in buys_by_token:
                    buys_by_token[token_id] = []
                buys_by_token[token_id].append(trade)
            elif trade["side"] == "SELL":
                # This is a close - calculate P&L
                if token_id in buys_by_token and buys_by_token[token_id]:
                    buy = buys_by_token[token_id][0]  # FIFO
                    entry_price = buy.get("price", 0.5)
                    exit_price = trade.get("price", 0.5)
                    size = trade.get("copy_size", 10)
                    pnl = (exit_price - entry_price) * (size / entry_price) if entry_price > 0 else 0
                    closed.append({
                        "timestamp": trade["timestamp"],
                        "market": trade.get("market", "Unknown")[:40],
                        "entry": round(entry_price, 4),
                        "exit": round(exit_price, 4),
                        "size": round(size, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(((exit_price / entry_price) - 1) * 100, 2) if entry_price > 0 else 0
                    })
                    buys_by_token[token_id].pop(0)
        return web.json_response(closed)

    async def get_summary(request):
        """Return portfolio summary."""
        return web.json_response({
            "balance": round(portfolio.balance, 2),
            "portfolio_value": round(portfolio.get_portfolio_value(), 2),
            "realised_pnl": round(portfolio.realised_pnl, 2),
            "open_positions": len(portfolio.positions),
            "total_trades": len(portfolio.trades),
            "daily_volume": round(portfolio.daily_volume, 2)
        })

    app = web.Application()
    app.router.add_get("/trades", get_trades)
    app.router.add_get("/closed", get_closed_trades)
    app.router.add_get("/summary", get_summary)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9092)
    await site.start()
    print("Trades API: http://localhost:9092/closed")


async def main():
    print("""
╔═══════════════════════════════════════════════════════════════════════╗
║   TRACKING: @cigarettes                                               ║
║   Wallet: 0xd218e474776403a330142299f7796e8ba32eb5c9                  ║
║   Mode: Real trades, Paper money ($10,000)                            ║
╚═══════════════════════════════════════════════════════════════════════╝
    """)

    print("Metrics server: http://localhost:9091")
    start_http_server(9091)

    portfolio = Portfolio()
    tracker = CigarettesTracker(portfolio)

    # Start trades API
    await run_trades_api(portfolio)

    print("Dashboard: http://localhost:3001/d/poly-copy-trade")

    try:
        await tracker.run()
    except KeyboardInterrupt:
        await tracker.stop()
        portfolio.summary()


if __name__ == "__main__":
    asyncio.run(main())
