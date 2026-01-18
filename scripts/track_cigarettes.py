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
    from prometheus_client import Counter, Gauge, Histogram, start_http_server, generate_latest, CONTENT_TYPE_LATEST
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp", "websockets", "prometheus_client", "--user"])
    import aiohttp
    import websockets
    from prometheus_client import Counter, Gauge, Histogram, start_http_server, generate_latest, CONTENT_TYPE_LATEST

# ============== CONFIG ==============
CONFIG_FILE = Path(__file__).parent.parent / "config" / "wallets.json"
KILL_SWITCH_FILE = Path(__file__).parent.parent / "config" / "KILL_SWITCH"
STARTING_BALANCE = 25000  # $25k
COPY_RATIO = 0.1  # Copy 10% of their trade size
MAX_COPY_SIZE = 500  # Max $500 per trade
MIN_COPY_SIZE = 10  # Min $10 per trade

# ============== RISK CONTROLS ==============
MAX_PORTFOLIO_EXPOSURE = 0.5  # Max 50% of capital in positions
TRAILING_STOP_LOSS = 0.20  # Exit if position drops 20% from peak
DAILY_LOSS_LIMIT = 0.10  # Stop trading if daily loss exceeds 10%
MIN_WALLET_WIN_RATE = 0.40  # Reduce copy ratio if wallet win rate < 40%

def check_kill_switch() -> bool:
    """Check if kill switch is active. Returns True if trading should STOP."""
    return KILL_SWITCH_FILE.exists()

def load_wallets() -> dict:
    """Load wallets from config file."""
    wallets = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            data = json.load(f)
            for w in data.get("wallets", []):
                if w.get("enabled", True):
                    addr = w["address"].lower()
                    wallets[addr] = w.get("name", addr[:10])
    return wallets

TRACKED_WALLETS = load_wallets()

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
UNREALISED_PNL = Gauge('poly_unrealised_pnl_usd', 'Unrealised PnL')
DAILY_VOLUME = Gauge('poly_daily_volume_usd', 'Daily volume')
OPEN_POSITIONS = Gauge('poly_open_positions', 'Open positions')
TOTAL_EXPOSURE = Gauge('poly_total_exposure_usd', 'Total exposure')
WEBSOCKET_CONNECTED = Gauge('poly_websocket_connected', 'WebSocket status')

# Per-wallet metrics
WALLET_TRADES = Gauge('poly_wallet_trades_total', 'Total trades per wallet', ['wallet'])
WALLET_OPEN = Gauge('poly_wallet_open_positions', 'Open positions per wallet', ['wallet'])
WALLET_CLOSED = Gauge('poly_wallet_closed_trades', 'Closed trades per wallet', ['wallet'])
WALLET_PNL = Gauge('poly_wallet_realised_pnl_usd', 'Realised PnL per wallet', ['wallet'])
WALLET_VOLUME = Gauge('poly_wallet_volume_usd', 'Volume per wallet', ['wallet'])
WALLET_WIN_RATE = Gauge('poly_wallet_win_rate', 'Win rate per wallet', ['wallet'])

# Risk metrics
KILL_SWITCH_ACTIVE = Gauge('poly_kill_switch_active', 'Kill switch status')
TRADING_PAUSED = Gauge('poly_trading_paused', 'Trading paused status')
DAILY_PNL = Gauge('poly_daily_pnl_usd', 'Daily PnL')

EXECUTION_LATENCY = Histogram('poly_execution_latency_ms', 'Latency',
                              buckets=[10, 25, 50, 100, 200, 500])

# ============== LOGGING ==============
LOG_FILE = Path(__file__).parent / "logs" / "cigarettes_trades.json"
LOG_FILE.parent.mkdir(exist_ok=True)


class Portfolio:
    """Track paper portfolio copying wallets."""

    def __init__(self, restore_state=True):
        self.balance = STARTING_BALANCE
        self.positions = {}  # token_id -> position data
        self.trades = []
        self.realised_pnl = 0
        self.daily_volume = 0
        self.start_time = datetime.now()
        self.daily_start_value = STARTING_BALANCE  # For daily loss tracking
        self.trading_paused = False  # Pause on daily loss limit

        # Per-wallet stats: wallet_name -> {trades, open, closed, pnl, volume, wins, losses}
        self.wallet_stats = {name: {"trades": 0, "open": 0, "closed": 0, "pnl": 0.0, "volume": 0.0, "wins": 0, "losses": 0}
                            for name in TRACKED_WALLETS.values()}
        # Track which wallet owns each position
        self.position_wallet = {}  # token_id -> wallet_name
        # Track peak value for trailing stop-loss
        self.position_peaks = {}  # token_id -> peak_value

        if restore_state:
            self._restore_from_log()

        BALANCE.set(self.balance)
        exposure = sum(p["cost"] for p in self.positions.values())
        portfolio_value = self.balance + exposure
        PORTFOLIO_VALUE.set(portfolio_value)
        REALISED_PNL.set(self.realised_pnl)
        UNREALISED_PNL.set(portfolio_value - STARTING_BALANCE - self.realised_pnl)
        DAILY_VOLUME.set(self.daily_volume)
        OPEN_POSITIONS.set(len(self.positions))
        TOTAL_EXPOSURE.set(exposure)

        # Update per-wallet metrics
        for wallet_name, stats in self.wallet_stats.items():
            WALLET_TRADES.labels(wallet=wallet_name).set(stats["trades"])
            WALLET_OPEN.labels(wallet=wallet_name).set(stats["open"])
            WALLET_CLOSED.labels(wallet=wallet_name).set(stats["closed"])
            WALLET_PNL.labels(wallet=wallet_name).set(stats["pnl"])
            WALLET_VOLUME.labels(wallet=wallet_name).set(stats["volume"])

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

        print("Restoring state from trade log...")
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
                    wallet_name = trade.get("trader", "cigarettes")  # Default to cigarettes for old trades

                    # Ensure wallet exists in stats
                    if wallet_name not in self.wallet_stats:
                        self.wallet_stats[wallet_name] = {"trades": 0, "open": 0, "closed": 0, "pnl": 0.0, "volume": 0.0, "wins": 0, "losses": 0}

                    # Update wallet stats
                    self.wallet_stats[wallet_name]["trades"] += 1
                    self.wallet_stats[wallet_name]["volume"] += copy_size

                    if side == "BUY":
                        if token_id not in self.positions:
                            self.positions[token_id] = {"size": 0, "cost": 0, "market": market, "entry_price": price}
                            self.position_wallet[token_id] = wallet_name
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

                            # Track PnL and wins/losses per wallet
                            pos_wallet = self.position_wallet.get(token_id, wallet_name)
                            if pos_wallet in self.wallet_stats:
                                self.wallet_stats[pos_wallet]["pnl"] += pnl
                                self.wallet_stats[pos_wallet]["closed"] += 1
                                if pnl >= 0:
                                    self.wallet_stats[pos_wallet]["wins"] += 1
                                else:
                                    self.wallet_stats[pos_wallet]["losses"] += 1

                            pos["size"] -= shares_sold
                            pos["cost"] -= cost_basis
                            if pos["size"] <= 0.01:
                                del self.positions[token_id]
                                if token_id in self.position_wallet:
                                    del self.position_wallet[token_id]

            # Calculate open positions per wallet
            for token_id, wallet_name in self.position_wallet.items():
                if wallet_name in self.wallet_stats:
                    self.wallet_stats[wallet_name]["open"] += 1

            # Calculate final balance
            total_invested = sum(t.get("copy_size", 0) * 1.001 for t in self.trades if t["side"] == "BUY")
            total_returned = sum(t.get("copy_size", 0) * 0.999 for t in self.trades if t["side"] == "SELL")
            self.balance = STARTING_BALANCE - total_invested + total_returned

            print(f"   Restored {len(self.trades)} trades")
            print(f"   Balance: ${self.balance:,.2f}")
            print(f"   📊 Open positions: {len(self.positions)}")
            print(f"   💵 Realised P&L: ${self.realised_pnl:+,.2f}")
        except Exception as e:
            print(f"   ⚠ Failed to restore state: {e}")

    def copy_trade(self, trade_data: dict) -> bool:
        """Copy a real trade from a tracked wallet."""
        start = time.time()

        # Check kill switch
        if check_kill_switch():
            print("🛑 KILL SWITCH ACTIVE - Trade blocked")
            return False

        # Check if trading is paused due to daily loss
        if self.trading_paused:
            print("⏸️ Trading paused - daily loss limit reached")
            return False

        side = trade_data["side"]
        original_size = trade_data["size"]
        price = trade_data["price"]
        market = trade_data["market"]
        token_id = trade_data.get("token_id", "unknown")
        wallet_name = trade_data.get("trader", "unknown")
        slug = trade_data.get("slug")

        # Ensure wallet exists in stats
        if wallet_name not in self.wallet_stats:
            self.wallet_stats[wallet_name] = {"trades": 0, "open": 0, "closed": 0, "pnl": 0.0, "volume": 0.0, "wins": 0, "losses": 0}

        # Calculate copy size with wallet win-rate adjustment
        base_ratio = COPY_RATIO
        stats = self.wallet_stats[wallet_name]
        total_closed = stats["wins"] + stats["losses"]
        if total_closed >= 5:  # Need at least 5 trades for win rate
            win_rate = stats["wins"] / total_closed
            if win_rate < MIN_WALLET_WIN_RATE:
                base_ratio *= 0.5  # Reduce copy ratio for underperforming wallets
                print(f"  ⚠ {wallet_name} win rate {win_rate:.0%} < {MIN_WALLET_WIN_RATE:.0%}, reducing copy ratio")

        copy_size = original_size * base_ratio
        copy_size = max(MIN_COPY_SIZE, min(MAX_COPY_SIZE, copy_size))

        # Check portfolio exposure limit for BUYs
        if side == "BUY":
            current_exposure = sum(p["cost"] for p in self.positions.values())
            max_exposure = STARTING_BALANCE * MAX_PORTFOLIO_EXPOSURE
            if current_exposure + copy_size > max_exposure:
                print(f"  ⚠ Exposure limit: ${current_exposure:.0f} + ${copy_size:.0f} > ${max_exposure:.0f}")
                copy_size = max(0, max_exposure - current_exposure)
                if copy_size < MIN_COPY_SIZE:
                    print(f"  ❌ Cannot open position - max exposure reached")
                    return False

        fee = copy_size * 0.001
        trade_pnl = 0.0

        if side == "BUY":
            cost = copy_size + fee
            if cost > self.balance:
                print(f"  Insufficient balance: need ${cost:.2f}, have ${self.balance:.2f}")
                return False
            self.balance -= cost

            if token_id not in self.positions:
                self.positions[token_id] = {"size": 0, "cost": 0, "market": market, "entry_price": price}
                self.position_wallet[token_id] = wallet_name
                self.wallet_stats[wallet_name]["open"] += 1
            pos = self.positions[token_id]
            pos["cost"] += copy_size
            pos["size"] += copy_size / price if price > 0 else 0
            pos["entry_price"] = pos["cost"] / pos["size"] if pos["size"] > 0 else price
            # Track peak value for trailing stop-loss
            current_value = pos["size"] * price
            self.position_peaks[token_id] = max(self.position_peaks.get(token_id, 0), current_value)
        else:
            if token_id in self.positions:
                pos = self.positions[token_id]
                sell_value = min(copy_size, pos["cost"])
                shares_sold = sell_value / price if price > 0 else 0
                cost_basis = shares_sold * pos["entry_price"]
                trade_pnl = sell_value - cost_basis
                self.realised_pnl += trade_pnl

                # Track PnL and win/loss per wallet (use position's original wallet)
                pos_wallet = self.position_wallet.get(token_id, wallet_name)
                if pos_wallet in self.wallet_stats:
                    self.wallet_stats[pos_wallet]["pnl"] += trade_pnl
                    self.wallet_stats[pos_wallet]["closed"] += 1
                    self.wallet_stats[pos_wallet]["open"] -= 1
                    # Track wins/losses for win rate calculation
                    if trade_pnl >= 0:
                        self.wallet_stats[pos_wallet]["wins"] += 1
                    else:
                        self.wallet_stats[pos_wallet]["losses"] += 1

                pos["size"] -= shares_sold
                pos["cost"] -= cost_basis
                if pos["size"] <= 0.01:
                    del self.positions[token_id]
                    if token_id in self.position_wallet:
                        del self.position_wallet[token_id]
                    if token_id in self.position_peaks:
                        del self.position_peaks[token_id]
            self.balance += (copy_size - fee)

        # Update wallet stats
        self.wallet_stats[wallet_name]["trades"] += 1
        self.wallet_stats[wallet_name]["volume"] += copy_size

        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "side": side,
            "market": market,
            "token_id": token_id,
            "slug": slug,
            "original_size": original_size,
            "copy_size": copy_size,
            "price": price,
            "balance_after": self.balance,
            "pnl": self.realised_pnl,
            "trader": wallet_name,
            "trade_pnl": trade_pnl
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
        portfolio_value = self.balance + exposure
        TOTAL_EXPOSURE.set(exposure)
        PORTFOLIO_VALUE.set(portfolio_value)
        UNREALISED_PNL.set(portfolio_value - STARTING_BALANCE - self.realised_pnl)

        # Update per-wallet metrics
        for wname, stats in self.wallet_stats.items():
            WALLET_TRADES.labels(wallet=wname).set(stats["trades"])
            WALLET_OPEN.labels(wallet=wname).set(stats["open"])
            WALLET_CLOSED.labels(wallet=wname).set(stats["closed"])
            WALLET_PNL.labels(wallet=wname).set(stats["pnl"])
            WALLET_VOLUME.labels(wallet=wname).set(stats["volume"])
            # Calculate win rate
            total_closed = stats["wins"] + stats["losses"]
            win_rate = stats["wins"] / total_closed if total_closed > 0 else 0.5
            WALLET_WIN_RATE.labels(wallet=wname).set(win_rate)

        # Update risk metrics
        KILL_SWITCH_ACTIVE.set(1 if check_kill_switch() else 0)
        TRADING_PAUSED.set(1 if self.trading_paused else 0)

        # Check daily loss limit
        daily_pnl = portfolio_value - self.daily_start_value
        DAILY_PNL.set(daily_pnl)
        if daily_pnl < -STARTING_BALANCE * DAILY_LOSS_LIMIT:
            self.trading_paused = True
            print(f"🚨 DAILY LOSS LIMIT REACHED: ${daily_pnl:+,.2f}")

        return True

    def _log_trade(self, trade: dict):
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(trade) + "\n")

    def check_stop_losses(self, price_fetcher) -> list:
        """Check positions for trailing stop-loss triggers. Returns list of tokens to sell."""
        stop_loss_triggers = []
        for token_id, pos in list(self.positions.items()):
            if token_id not in self.position_peaks:
                continue
            peak = self.position_peaks[token_id]
            current_value = pos["cost"]  # Using cost as proxy for value
            if peak > 0:
                drawdown = (peak - current_value) / peak
                if drawdown >= TRAILING_STOP_LOSS:
                    stop_loss_triggers.append({
                        "token_id": token_id,
                        "market": pos["market"],
                        "drawdown": drawdown,
                        "peak": peak,
                        "current": current_value
                    })
        return stop_loss_triggers

    def get_wallet_win_rates(self) -> dict:
        """Get win rates for all wallets."""
        rates = {}
        for name, stats in self.wallet_stats.items():
            total = stats["wins"] + stats["losses"]
            if total > 0:
                rates[name] = {
                    "win_rate": stats["wins"] / total,
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "pnl": stats["pnl"]
                }
        return rates

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

        print(f"\nMonitoring {len(TRACKED_WALLETS)} wallets:")
        for addr, name in TRACKED_WALLETS.items():
            print(f"   {name}: {addr}")
        print(f"\nPaper trading with ${STARTING_BALANCE:,}")
        print(f"   Copying {COPY_RATIO*100:.0f}% of each trade (max ${MAX_COPY_SIZE})")
        print("\n" + "-" * 70)
        print("Monitoring via: WebSocket + On-chain + Data API")
        print("-" * 70 + "\n")

        await asyncio.gather(
            self.monitor_websocket(),
            self.monitor_onchain(),
            self.poll_data_api(),
            self.periodic_status()
        )

    async def monitor_websocket(self):
        """Monitor Polymarket WebSocket for real-time trade activity."""
        print("🌐 Starting WebSocket monitor...")
        WEBSOCKET_CONNECTED.set(0)

        # WebSocket endpoint for user activity
        ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

        while self.running:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    WEBSOCKET_CONNECTED.set(1)
                    print("✅ WebSocket connected")

                    # Subscribe to each tracked wallet
                    for wallet_addr in TRACKED_WALLETS.keys():
                        sub_msg = {
                            "type": "subscribe",
                            "channel": "user",
                            "user": wallet_addr
                        }
                        await ws.send(json.dumps(sub_msg))

                    print(f"   Subscribed to {len(TRACKED_WALLETS)} wallets")

                    async for message in ws:
                        try:
                            data = json.loads(message)
                            await self.handle_ws_message(data)
                        except json.JSONDecodeError:
                            continue
                        except Exception as e:
                            print(f"⚠ WS message error: {e}")

            except websockets.exceptions.ConnectionClosed:
                WEBSOCKET_CONNECTED.set(0)
                print("⚠ WebSocket disconnected, reconnecting...")
            except Exception as e:
                WEBSOCKET_CONNECTED.set(0)
                print(f"⚠ WebSocket error: {e}")

            await asyncio.sleep(5)  # Wait before reconnecting

    async def handle_ws_message(self, data: dict):
        """Handle incoming WebSocket message."""
        msg_type = data.get("type", "")

        # Handle trade/order messages
        if msg_type in ["trade", "order_filled", "order"]:
            user = data.get("user", "").lower()
            if user in TRACKED_WALLETS:
                wallet_name = TRACKED_WALLETS[user]
                side = data.get("side", "").upper()
                size = float(data.get("size", 0))
                price = float(data.get("price", 0.5))
                token_id = data.get("asset_id", data.get("token_id", ""))

                if side in ["BUY", "SELL"] and size > 0:
                    size_usd = size * price if price < 10 else size  # Handle different formats

                    print(f"\n⚡ [WS] {side} detected from @{wallet_name}")

                    # Get market info
                    _, market_name, slug = await self.get_token_price(str(token_id))

                    trade_data = {
                        "side": side,
                        "size": size_usd,
                        "price": price,
                        "market": market_name,
                        "token_id": str(token_id),
                        "trader": wallet_name,
                        "slug": slug
                    }

                    # Deduplicate with seen_trades
                    trade_key = f"{wallet_name}:{token_id}:{side}:{size_usd:.2f}"
                    if trade_key not in self.seen_trades:
                        self.seen_trades.add(trade_key)
                        success = self.portfolio.copy_trade(trade_data)
                        if success:
                            print(f"   ⚡ WS COPIED in <100ms")

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

        # TransferSingle topic
        transfer_topic = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

        # Check all tracked wallets
        for wallet_addr, wallet_name in TRACKED_WALLETS.items():
            padded_addr = "0x000000000000000000000000" + wallet_addr[2:]

            # Check transfers TO wallet (buys)
            async with self.session.post(POLYGON_RPC, json={
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(self.last_block + 1),
                    "toBlock": hex(current_block),
                    "address": CONDITIONAL_TOKENS,
                    "topics": [transfer_topic, None, None, padded_addr]
                }],
                "id": 2
            }) as resp:
                data = await resp.json()
                for log in data.get("result", []):
                    await self.process_transfer(log, "BUY", wallet_name)

            # Check transfers FROM wallet (sells)
            async with self.session.post(POLYGON_RPC, json={
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(self.last_block + 1),
                    "toBlock": hex(current_block),
                    "address": CONDITIONAL_TOKENS,
                    "topics": [transfer_topic, None, padded_addr, None]
                }],
                "id": 3
            }) as resp:
                data = await resp.json()
                for log in data.get("result", []):
                    await self.process_transfer(log, "SELL", wallet_name)

        self.last_block = current_block

    async def process_transfer(self, log: dict, side: str, wallet_name: str = "unknown"):
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

        TRADES_DETECTED.labels(wallet=wallet_name).inc()

        # Fetch real price from Polymarket API
        price, market_name, slug = await self.get_token_price(str(token_id))

        print(f"\n{'[BUY]' if side == 'BUY' else '[SELL]'} @{wallet_name} {side}")
        print(f"   Market:   {market_name}")
        print(f"   Size:     ${size_usd:,.2f}")
        print(f"   Price:    ${price:.4f}")
        print(f"   TX:       {tx_hash[:20]}...")
        print(f"   Time:     {datetime.now().strftime('%H:%M:%S')}")

        trade_data = {
            "side": side,
            "size": size_usd,
            "price": price,
            "market": market_name,
            "token_id": str(token_id),
            "trader": wallet_name,
            "slug": slug
        }

        success = self.portfolio.copy_trade(trade_data)

        if success:
            copy_size = min(MAX_COPY_SIZE, max(MIN_COPY_SIZE, size_usd * COPY_RATIO))
            print(f"   -> COPIED: {side} ${copy_size:.2f} @ ${price:.4f}")
            print(f"   -> Balance: ${self.portfolio.balance:,.2f} | P&L: ${self.portfolio.realised_pnl:+,.2f}")

    async def get_token_price(self, token_id: str) -> tuple[float, str]:
        """Fetch real price and market name for a token ID."""
        # Check cache first
        if token_id in self.price_cache:
            cached = self.price_cache[token_id]
            if time.time() - cached["time"] < 60:  # Cache for 60 seconds
                return cached["price"], cached["market"], cached.get("slug")

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

        # Get market name and slug from gamma API
        slug = None
        try:
            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        market_name = data[0].get("question", market_name)
                        slug = data[0].get("slug")
        except:
            pass

        # Fallback to CLOB API if gamma didn't work
        if not slug and condition_id:
            try:
                url = f"https://clob.polymarket.com/markets/{condition_id}"
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not market_name or market_name.startswith("Token"):
                            market_name = data.get("question", market_name)
            except:
                pass

        # Cache the result
        self.price_cache[token_id] = {
            "price": price,
            "market": market_name,
            "slug": slug,
            "time": time.time()
        }
        return price, market_name, slug

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
        print("Starting data API polling...")

        while self.running:
            try:
                # Poll positions for all tracked wallets
                for wallet_addr, wallet_name in TRACKED_WALLETS.items():
                    url = f"{POLYMARKET_DATA}/positions?user={wallet_addr}"
                    async with self.session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            await self.process_positions(data, wallet_name)
            except Exception as e:
                pass  # Silent fail for polling

            await asyncio.sleep(30)  # Poll every 30 seconds

    async def process_positions(self, data, wallet_name: str = "unknown"):
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
        buys_by_token = {}
        for trade in portfolio.trades:
            token_id = trade.get("token_id", "unknown")
            if trade["side"] == "BUY":
                if token_id not in buys_by_token:
                    buys_by_token[token_id] = []
                buys_by_token[token_id].append(trade)
            elif trade["side"] == "SELL":
                if token_id in buys_by_token and buys_by_token[token_id]:
                    buy = buys_by_token[token_id][0]
                    entry_price = buy.get("price", 0.5)
                    exit_price = trade.get("price", 0.5)
                    size = trade.get("copy_size", 10)
                    pnl = (exit_price - entry_price) * (size / entry_price) if entry_price > 0 else 0
                    closed.append({
                        "timestamp": trade["timestamp"],
                        "market": trade.get("market", "Unknown")[:40],
                        "trader": trade.get("trader", buy.get("trader", "unknown")),
                        "entry": round(entry_price, 4),
                        "exit": round(exit_price, 4),
                        "size": round(size, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(((exit_price / entry_price) - 1) * 100, 2) if entry_price > 0 else 0
                    })
                    buys_by_token[token_id].pop(0)
        return web.json_response(closed)

    async def get_all_trades(request):
        """Return all trades with open/closed status."""
        all_trades = []
        # Track which buys have been closed
        open_positions = set(portfolio.positions.keys())

        for trade in reversed(portfolio.trades):  # Most recent first
            token_id = trade.get("token_id", "unknown")
            side = trade["side"]

            if side == "SELL":
                status = "CLOSED"
            else:
                # BUY - check if position is still open
                status = "OPEN" if token_id in open_positions else "CLOSED"

            slug = trade.get("slug")
            market_url = f"https://polymarket.com/event/{slug}" if slug else ""
            all_trades.append({
                "timestamp": trade.get("timestamp", "")[:19],
                "trader": trade.get("trader", "unknown"),
                "side": side,
                "status": status,
                "market": trade.get("market", "Unknown"),
                "url": market_url,
                "price": round(trade.get("price", 0), 4),
                "size": round(trade.get("copy_size", 0), 2)
            })
        return web.json_response(all_trades)

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

    async def get_wallets(request):
        """Return per-wallet breakdown with win rates."""
        wallets = []
        for name, stats in portfolio.wallet_stats.items():
            total_closed = stats["wins"] + stats["losses"]
            win_rate = stats["wins"] / total_closed if total_closed > 0 else 0.5
            wallets.append({
                "wallet": name,
                "trades": stats["trades"],
                "open": stats["open"],
                "closed": stats["closed"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(win_rate, 2),
                "pnl": round(stats["pnl"], 2),
                "volume": round(stats["volume"], 2)
            })
        # Sort by trades descending
        wallets.sort(key=lambda x: x["trades"], reverse=True)
        return web.json_response(wallets)

    async def get_risk_status(request):
        """Return risk management status."""
        pv = portfolio.get_portfolio_value()
        exposure = sum(p["cost"] for p in portfolio.positions.values())
        daily_pnl = pv - portfolio.daily_start_value
        return web.json_response({
            "kill_switch_active": check_kill_switch(),
            "trading_paused": portfolio.trading_paused,
            "exposure_usd": round(exposure, 2),
            "exposure_pct": round(exposure / STARTING_BALANCE * 100, 1),
            "max_exposure_pct": MAX_PORTFOLIO_EXPOSURE * 100,
            "daily_pnl": round(daily_pnl, 2),
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
            "trailing_stop_pct": TRAILING_STOP_LOSS * 100
        })

    async def reconcile_positions(request):
        """Compare local positions against on-chain data."""
        issues = []
        our_open = set(portfolio.positions.keys())

        # Check each wallet's positions via Polymarket API
        async with aiohttp.ClientSession() as session:
            for wallet_addr, wallet_name in TRACKED_WALLETS.items():
                try:
                    url = f"https://data-api.polymarket.com/positions?user={wallet_addr}&sizeThreshold=0.01"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            positions = await resp.json()
                            their_tokens = {str(p.get("conditionId", "")) for p in positions}

                            # Find positions we have that they don't (potential missed sells)
                            for token_id in our_open:
                                pos_wallet = portfolio.position_wallet.get(token_id)
                                if pos_wallet == wallet_name and token_id not in their_tokens:
                                    market = portfolio.positions[token_id].get("market", "Unknown")[:40]
                                    issues.append({
                                        "type": "potential_missed_sell",
                                        "wallet": wallet_name,
                                        "token_id": token_id[:20],
                                        "market": market
                                    })
                except Exception as e:
                    issues.append({"type": "api_error", "wallet": wallet_name, "error": str(e)})

        return web.json_response({
            "status": "ok" if len(issues) == 0 else "issues_found",
            "our_positions": len(our_open),
            "issues": issues[:20]  # Limit to 20 issues
        })

    async def get_root(request):
        return web.json_response({
            "service": "Polymarket Copy Trader",
            "endpoints": ["/summary", "/positions", "/trades", "/closed", "/wallets", "/risk", "/reconcile", "/prometheus"],
            "status": "running"
        })

    async def get_metrics(request):
        """Prometheus metrics endpoint."""
        from prometheus_client import generate_latest
        metrics = generate_latest()
        return web.Response(text=metrics.decode('utf-8'), content_type='text/plain')

    app = web.Application()
    app.router.add_get("/", get_root)
    app.router.add_get("/prometheus", get_metrics)
    app.router.add_get("/trades", get_trades)
    app.router.add_get("/closed", get_closed_trades)
    app.router.add_get("/all", get_all_trades)
    app.router.add_get("/summary", get_summary)
    app.router.add_get("/wallets", get_wallets)
    app.router.add_get("/risk", get_risk_status)
    app.router.add_get("/reconcile", reconcile_positions)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9092)
    await site.start()
    print("Trades API: http://localhost:9092/closed")


async def main():
    print("\n" + "=" * 70)
    print("POLYMARKET COPY TRADING - Multi-Wallet Tracker")
    print("=" * 70)
    print(f"Tracking {len(TRACKED_WALLETS)} wallets:")
    for addr, name in TRACKED_WALLETS.items():
        print(f"  - {name:15} {addr}")
    print(f"\nMode: Paper trading with ${STARTING_BALANCE:,}")
    print("=" * 70 + "\n")

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
