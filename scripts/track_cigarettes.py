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
    from web3 import Web3
    from eth_account import Account
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "aiohttp", "websockets", "prometheus_client", "web3", "eth-account", "--user"])
    import aiohttp
    import websockets
    from prometheus_client import Counter, Gauge, Histogram, start_http_server, generate_latest, CONTENT_TYPE_LATEST
    from web3 import Web3
    from eth_account import Account

# ============== CONFIG ==============
CONFIG_FILE = Path(__file__).parent / "config" / "wallets.json"
KILL_SWITCH_FILE = Path(__file__).parent / "config" / "KILL_SWITCH"
STARTING_BALANCE = 25000  # $25k
COPY_RATIO = 0.1  # Copy 10% of their trade size
MAX_COPY_SIZE = 500  # Max $500 per trade
MIN_COPY_SIZE = 10  # Min $10 per trade

# ============== RISK CONTROLS ==============
MAX_PORTFOLIO_EXPOSURE = 0.5  # Max 50% of capital in positions
TRAILING_STOP_LOSS = 0.20  # Exit if position drops 20% from peak
DAILY_LOSS_LIMIT = 0.10  # Stop trading if daily loss exceeds 10%
MIN_WALLET_WIN_RATE = 0.40  # Reduce copy ratio if wallet win rate < 40%

# ============== AUTO-WITHDRAWAL CONFIG ==============
# Set these via environment variables for security
COLD_WALLET_ADDRESS = os.environ.get("COLD_WALLET_ADDRESS", "")  # Your cold wallet
HOT_WALLET_PRIVATE_KEY = os.environ.get("HOT_WALLET_PRIVATE_KEY", "")  # Trading wallet private key
WITHDRAWAL_THRESHOLD = float(os.environ.get("WITHDRAWAL_THRESHOLD", "1000"))  # Withdraw when profits exceed this
MIN_BALANCE_KEEP = float(os.environ.get("MIN_BALANCE_KEEP", "5000"))  # Always keep this much for trading
WITHDRAWAL_CHECK_INTERVAL = 3600  # Check every hour (seconds)
USDC_CONTRACT_POLYGON = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC on Polygon
USDC_DECIMALS = 6

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

# Withdrawal metrics
WITHDRAWALS_TOTAL = Counter('poly_withdrawals_total', 'Total withdrawals made')
WITHDRAWALS_AMOUNT = Counter('poly_withdrawals_amount_usd', 'Total USD withdrawn')
LAST_WITHDRAWAL_TIME = Gauge('poly_last_withdrawal_timestamp', 'Last withdrawal timestamp')
COLD_WALLET_BALANCE = Gauge('poly_cold_wallet_balance_usd', 'Cold wallet USDC balance')

# Performance metrics
ROI_PERCENT = Gauge('poly_roi_percent', 'Return on investment percentage')
WIN_RATE = Gauge('poly_win_rate', 'Overall win rate')
MISSED_TRADES = Counter('poly_missed_trades_total', 'Trades not copied', ['reason'])
COPY_LATENCY_AVG = Gauge('poly_copy_latency_avg_ms', 'Average copy latency in ms')
SYSTEM_START_TIME = Gauge('poly_system_start_timestamp', 'System start timestamp')
CHANGE_24H = Gauge('poly_24h_change_usd', '24 hour portfolio change')
BEST_TRADER_PNL = Gauge('poly_best_trader_pnl_usd', 'Best trader PnL', ['wallet'])
WORST_TRADER_PNL = Gauge('poly_worst_trader_pnl_usd', 'Worst trader PnL', ['wallet'])

# ============== LOGGING ==============
LOG_FILE = Path(__file__).parent / "logs" / "cigarettes_trades.json"
WITHDRAWAL_LOG_FILE = Path(__file__).parent / "logs" / "withdrawals.json"
LOG_FILE.parent.mkdir(exist_ok=True)


class AutoWithdrawal:
    """Automatically withdraw profits to cold wallet."""

    def __init__(self):
        self.enabled = bool(COLD_WALLET_ADDRESS and HOT_WALLET_PRIVATE_KEY)
        self.w3 = None
        self.account = None
        self.usdc_contract = None
        self.last_withdrawal = 0
        self.total_withdrawn = 0
        self.withdrawal_history = []

        if self.enabled:
            self._setup_web3()
            self._load_history()
        else:
            print("Auto-withdrawal DISABLED - set COLD_WALLET_ADDRESS and HOT_WALLET_PRIVATE_KEY env vars")

    def _setup_web3(self):
        """Initialize Web3 connection and contracts."""
        try:
            self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
            if not self.w3.is_connected():
                print("Failed to connect to Polygon RPC")
                self.enabled = False
                return

            self.account = Account.from_key(HOT_WALLET_PRIVATE_KEY)
            print(f"Auto-withdrawal enabled")
            print(f"   Hot wallet:  {self.account.address}")
            print(f"   Cold wallet: {COLD_WALLET_ADDRESS}")
            print(f"   Threshold:   ${WITHDRAWAL_THRESHOLD:,.0f}")
            print(f"   Keep min:    ${MIN_BALANCE_KEEP:,.0f}")

            # USDC contract ABI (minimal for transfer)
            usdc_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                },
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]
            self.usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC_CONTRACT_POLYGON),
                abi=usdc_abi
            )
        except Exception as e:
            print(f"Failed to setup Web3: {e}")
            self.enabled = False

    def _load_history(self):
        """Load withdrawal history from log."""
        if WITHDRAWAL_LOG_FILE.exists():
            try:
                with open(WITHDRAWAL_LOG_FILE, "r") as f:
                    for line in f:
                        if line.strip():
                            record = json.loads(line)
                            self.withdrawal_history.append(record)
                            self.total_withdrawn += record.get("amount_usd", 0)
                if self.withdrawal_history:
                    self.last_withdrawal = self.withdrawal_history[-1].get("timestamp", 0)
                print(f"   Loaded {len(self.withdrawal_history)} previous withdrawals (${self.total_withdrawn:,.2f} total)")
            except Exception as e:
                print(f"   Failed to load withdrawal history: {e}")

    def get_usdc_balance(self, address: str) -> float:
        """Get USDC balance for an address."""
        if not self.enabled or not self.usdc_contract:
            return 0
        try:
            balance_raw = self.usdc_contract.functions.balanceOf(
                Web3.to_checksum_address(address)
            ).call()
            return balance_raw / (10 ** USDC_DECIMALS)
        except Exception as e:
            print(f"Failed to get USDC balance: {e}")
            return 0

    def get_hot_wallet_balance(self) -> float:
        """Get USDC balance of hot wallet."""
        if not self.account:
            return 0
        return self.get_usdc_balance(self.account.address)

    def get_cold_wallet_balance(self) -> float:
        """Get USDC balance of cold wallet."""
        return self.get_usdc_balance(COLD_WALLET_ADDRESS)

    async def check_and_withdraw(self, realised_pnl: float) -> dict:
        """Check if withdrawal should happen and execute if so."""
        if not self.enabled:
            return {"status": "disabled"}

        # Check if enough time has passed since last withdrawal
        now = time.time()
        if now - self.last_withdrawal < WITHDRAWAL_CHECK_INTERVAL:
            return {"status": "cooldown", "next_check": WITHDRAWAL_CHECK_INTERVAL - (now - self.last_withdrawal)}

        # Get current hot wallet balance
        hot_balance = self.get_hot_wallet_balance()

        # Calculate withdrawable amount
        # Withdraw profits above threshold, but keep MIN_BALANCE_KEEP for trading
        withdrawable = max(0, hot_balance - MIN_BALANCE_KEEP)

        # Only withdraw if we have profits above threshold
        if realised_pnl < WITHDRAWAL_THRESHOLD:
            return {
                "status": "below_threshold",
                "realised_pnl": realised_pnl,
                "threshold": WITHDRAWAL_THRESHOLD,
                "hot_balance": hot_balance
            }

        if withdrawable < 100:  # Min $100 to withdraw
            return {
                "status": "insufficient_withdrawable",
                "withdrawable": withdrawable,
                "hot_balance": hot_balance
            }

        # Calculate amount to withdraw (realised profits or max withdrawable)
        withdraw_amount = min(realised_pnl, withdrawable)

        # Execute withdrawal
        result = await self._execute_withdrawal(withdraw_amount)
        return result

    async def _execute_withdrawal(self, amount_usd: float) -> dict:
        """Execute USDC transfer to cold wallet."""
        try:
            amount_raw = int(amount_usd * (10 ** USDC_DECIMALS))

            # Build transaction
            nonce = self.w3.eth.get_transaction_count(self.account.address)
            gas_price = self.w3.eth.gas_price

            txn = self.usdc_contract.functions.transfer(
                Web3.to_checksum_address(COLD_WALLET_ADDRESS),
                amount_raw
            ).build_transaction({
                'chainId': 137,  # Polygon mainnet
                'gas': 100000,
                'gasPrice': gas_price,
                'nonce': nonce,
            })

            # Sign and send
            signed_txn = self.w3.eth.account.sign_transaction(txn, HOT_WALLET_PRIVATE_KEY)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt['status'] == 1:
                # Success
                self.last_withdrawal = time.time()
                self.total_withdrawn += amount_usd

                record = {
                    "timestamp": self.last_withdrawal,
                    "timestamp_iso": datetime.now().isoformat(),
                    "amount_usd": amount_usd,
                    "tx_hash": tx_hash_hex,
                    "to": COLD_WALLET_ADDRESS,
                    "gas_used": receipt['gasUsed'],
                    "status": "success"
                }
                self.withdrawal_history.append(record)
                self._log_withdrawal(record)

                # Update metrics
                WITHDRAWALS_TOTAL.inc()
                WITHDRAWALS_AMOUNT.inc(amount_usd)
                LAST_WITHDRAWAL_TIME.set(self.last_withdrawal)
                COLD_WALLET_BALANCE.set(self.get_cold_wallet_balance())

                print(f"\n{'='*60}")
                print(f"WITHDRAWAL SUCCESSFUL")
                print(f"   Amount:  ${amount_usd:,.2f} USDC")
                print(f"   To:      {COLD_WALLET_ADDRESS}")
                print(f"   TX:      {tx_hash_hex}")
                print(f"   Total withdrawn: ${self.total_withdrawn:,.2f}")
                print(f"{'='*60}\n")

                return {
                    "status": "success",
                    "amount": amount_usd,
                    "tx_hash": tx_hash_hex,
                    "total_withdrawn": self.total_withdrawn
                }
            else:
                print(f"Withdrawal transaction failed: {tx_hash_hex}")
                return {"status": "tx_failed", "tx_hash": tx_hash_hex}

        except Exception as e:
            print(f"Withdrawal error: {e}")
            return {"status": "error", "error": str(e)}

    def _log_withdrawal(self, record: dict):
        """Log withdrawal to file."""
        with open(WITHDRAWAL_LOG_FILE, "a") as f:
            f.write(json.dumps(record) + "\n")

    def get_status(self) -> dict:
        """Get current withdrawal status."""
        hot_balance = self.get_hot_wallet_balance() if self.enabled else 0
        cold_balance = self.get_cold_wallet_balance() if self.enabled else 0

        return {
            "enabled": self.enabled,
            "hot_wallet": self.account.address if self.account else None,
            "cold_wallet": COLD_WALLET_ADDRESS or None,
            "hot_balance_usd": round(hot_balance, 2),
            "cold_balance_usd": round(cold_balance, 2),
            "threshold_usd": WITHDRAWAL_THRESHOLD,
            "min_keep_usd": MIN_BALANCE_KEEP,
            "total_withdrawn_usd": round(self.total_withdrawn, 2),
            "withdrawal_count": len(self.withdrawal_history),
            "last_withdrawal": datetime.fromtimestamp(self.last_withdrawal).isoformat() if self.last_withdrawal else None
        }


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

        # Performance tracking
        self.latencies = []  # Store last 100 latencies for averaging
        self.total_wins = 0
        self.total_losses = 0
        self.value_24h_ago = STARTING_BALANCE  # Updated hourly
        self.category_stats = {}  # category -> {trades, pnl, volume}

        # Set system start time
        SYSTEM_START_TIME.set(time.time())

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
        print(f"Looking for log file at: {LOG_FILE}")
        print(f"Log file exists: {LOG_FILE.exists()}")
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
            MISSED_TRADES.labels(reason="kill_switch").inc()
            return False

        # Check if trading is paused due to daily loss
        if self.trading_paused:
            print("⏸️ Trading paused - daily loss limit reached")
            MISSED_TRADES.labels(reason="daily_loss_limit").inc()
            return False

        side = trade_data["side"]
        original_size = trade_data["size"]
        price = trade_data["price"]
        market = trade_data["market"]
        token_id = trade_data.get("token_id", "unknown")
        wallet_name = trade_data.get("trader", "unknown")
        slug = trade_data.get("slug")
        category = trade_data.get("category", "Other")

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
                    MISSED_TRADES.labels(reason="max_exposure").inc()
                    return False

        fee = copy_size * 0.001
        trade_pnl = 0.0

        if side == "BUY":
            cost = copy_size + fee
            if cost > self.balance:
                print(f"  Insufficient balance: need ${cost:.2f}, have ${self.balance:.2f}")
                MISSED_TRADES.labels(reason="insufficient_balance").inc()
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
            "category": category,
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

        # Update performance metrics
        # Track latency (keep last 100)
        self.latencies.append(latency)
        if len(self.latencies) > 100:
            self.latencies.pop(0)
        avg_latency = sum(self.latencies) / len(self.latencies)
        COPY_LATENCY_AVG.set(avg_latency)

        # ROI
        roi = ((portfolio_value - STARTING_BALANCE) / STARTING_BALANCE) * 100
        ROI_PERCENT.set(roi)

        # Overall win rate
        total_wins = sum(s["wins"] for s in self.wallet_stats.values())
        total_losses = sum(s["losses"] for s in self.wallet_stats.values())
        overall_win_rate = total_wins / (total_wins + total_losses) if (total_wins + total_losses) > 0 else 0.5
        WIN_RATE.set(overall_win_rate)

        # 24h change
        CHANGE_24H.set(portfolio_value - self.value_24h_ago)

        # Best/worst trader
        if self.wallet_stats:
            best = max(self.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            worst = min(self.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            BEST_TRADER_PNL.labels(wallet=best[0]).set(best[1]["pnl"])
            WORST_TRADER_PNL.labels(wallet=worst[0]).set(worst[1]["pnl"])

        # Category stats
        if category not in self.category_stats:
            self.category_stats[category] = {"trades": 0, "pnl": 0.0, "volume": 0.0}
        self.category_stats[category]["trades"] += 1
        self.category_stats[category]["volume"] += copy_size
        if side == "SELL":
            self.category_stats[category]["pnl"] += trade_pnl

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

    def __init__(self, portfolio: Portfolio, auto_withdrawal: AutoWithdrawal = None):
        self.portfolio = portfolio
        self.auto_withdrawal = auto_withdrawal
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

        tasks = [
            self.monitor_websocket(),
            self.monitor_onchain(),
            self.poll_data_api(),
            self.periodic_status()
        ]
        if self.auto_withdrawal and self.auto_withdrawal.enabled:
            tasks.append(self.periodic_withdrawal_check())
        await asyncio.gather(*tasks)

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
                    _, market_name, slug, category = await self.get_token_price(str(token_id))

                    trade_data = {
                        "side": side,
                        "size": size_usd,
                        "price": price,
                        "market": market_name,
                        "token_id": str(token_id),
                        "trader": wallet_name,
                        "slug": slug,
                        "category": category
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
        price, market_name, slug, category = await self.get_token_price(str(token_id))

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
            "slug": slug,
            "category": category
        }

        success = self.portfolio.copy_trade(trade_data)

        if success:
            copy_size = min(MAX_COPY_SIZE, max(MIN_COPY_SIZE, size_usd * COPY_RATIO))
            print(f"   -> COPIED: {side} ${copy_size:.2f} @ ${price:.4f}")
            print(f"   -> Balance: ${self.portfolio.balance:,.2f} | P&L: ${self.portfolio.realised_pnl:+,.2f}")

    async def get_token_price(self, token_id: str) -> tuple[float, str, str, str]:
        """Fetch real price, market name, slug, and category for a token ID."""
        # Check cache first
        if token_id in self.price_cache:
            cached = self.price_cache[token_id]
            if time.time() - cached["time"] < 60:  # Cache for 60 seconds
                return cached["price"], cached["market"], cached.get("slug"), cached.get("category", "Other")

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

        # Get market name, slug, and category from gamma API
        slug = None
        category = "Other"
        try:
            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        market_data = data[0]
                        market_name = market_data.get("question", market_name)
                        slug = market_data.get("slug")
                        # Get category from tags or groupItemTitle
                        tags = market_data.get("tags", [])
                        if tags:
                            category = tags[0].get("label", "Other") if isinstance(tags[0], dict) else tags[0]
                        elif market_data.get("groupItemTitle"):
                            category = market_data.get("groupItemTitle")
                        # Categorize by common keywords if no tags
                        if category == "Other":
                            q_lower = market_name.lower()
                            if any(w in q_lower for w in ["trump", "biden", "election", "congress", "senate", "president", "vote", "governor"]):
                                category = "Politics"
                            elif any(w in q_lower for w in ["nfl", "nba", "mlb", "soccer", "football", "basketball", "sports", "game", "match", "win"]):
                                category = "Sports"
                            elif any(w in q_lower for w in ["crypto", "bitcoin", "ethereum", "btc", "eth", "price"]):
                                category = "Crypto"
                            elif any(w in q_lower for w in ["fed", "rate", "inflation", "gdp", "economy", "stock"]):
                                category = "Finance"
                            elif any(w in q_lower for w in ["ai", "tech", "apple", "google", "microsoft"]):
                                category = "Tech"
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
            "category": category,
            "time": time.time()
        }
        return price, market_name, slug, category

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

    async def periodic_withdrawal_check(self):
        """Check for auto-withdrawal periodically."""
        print("Starting auto-withdrawal monitor...")
        while self.running:
            await asyncio.sleep(WITHDRAWAL_CHECK_INTERVAL)  # Check every hour
            if self.auto_withdrawal and self.auto_withdrawal.enabled:
                result = await self.auto_withdrawal.check_and_withdraw(self.portfolio.realised_pnl)
                if result.get("status") == "success":
                    print(f"Auto-withdrawal completed: ${result.get('amount', 0):,.2f}")
                elif result.get("status") not in ["disabled", "cooldown", "below_threshold"]:
                    print(f"Auto-withdrawal check: {result.get('status')}")


async def run_trades_api(portfolio: Portfolio, auto_withdrawal: AutoWithdrawal = None):
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

    def format_timestamp(iso_timestamp: str) -> str:
        """Convert ISO timestamp to readable format like 'Jan 18, 14:32'."""
        try:
            dt = datetime.fromisoformat(iso_timestamp.replace('Z', '+00:00'))
            return dt.strftime("%b %d, %H:%M")
        except:
            return iso_timestamp[:16] if iso_timestamp else ""

    def detect_category(market_name: str) -> str:
        """Detect category from market name keywords."""
        if not market_name:
            return "Other"
        q = market_name.lower()
        if any(w in q for w in ["trump", "biden", "election", "congress", "senate", "president", "vote", "governor", "republican", "democrat"]):
            return "Politics"
        if any(w in q for w in ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "hockey", "sports", "game", "match", "vs.", "spread", "o/u", "patriots", "lakers", "yankees", "cavaliers", "pacers", "pistons", "grizzlies", "spurs", "thunder", "heat", "celtics", "warriors", "bulls", "knicks", "nets", "clippers", "rockets", "mavericks", "suns", "76ers", "bucks", "hawks", "hornets", "magic", "wizards", "raptors", "jazz", "pelicans", "kings", "timberwolves", "blazers", "nuggets", "spartans", "bulldogs", "tigers", "tritons", "hornets", "quakers", "flames", "hurricanes", "devils"]):
            return "Sports"
        if any(w in q for w in ["crypto", "bitcoin", "ethereum", "btc", "eth", "solana", "sol"]):
            return "Crypto"
        if any(w in q for w in ["fed", "rate", "inflation", "gdp", "economy", "stock", "market", "s&p", "dow", "nasdaq"]):
            return "Finance"
        if any(w in q for w in ["ai", "tech", "apple", "google", "microsoft", "openai", "meta"]):
            return "Tech"
        return "Other"

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
            market_name = trade.get("market", "Unknown")
            category = trade.get("category")
            if not category or category == "Other":
                category = detect_category(market_name)
            all_trades.append({
                "timestamp": format_timestamp(trade.get("timestamp", "")),
                "category": category,
                "trader": trade.get("trader", "unknown"),
                "side": side,
                "market": trade.get("market", "Unknown"),
                "slug": slug,
                "price": round(trade.get("price", 0), 4),
                "copy_size": round(trade.get("copy_size", 0), 2)
            })
        return web.json_response(all_trades)

    async def get_summary(request):
        """Return portfolio summary."""
        pv = portfolio.get_portfolio_value()
        unrealised = pv - STARTING_BALANCE - portfolio.realised_pnl
        exposure = sum(p["cost"] for p in portfolio.positions.values())
        roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
        total_wins = sum(s["wins"] for s in portfolio.wallet_stats.values())
        total_losses = sum(s["losses"] for s in portfolio.wallet_stats.values())
        win_rate = total_wins / (total_wins + total_losses) if (total_wins + total_losses) > 0 else 0.5
        avg_latency = sum(portfolio.latencies) / len(portfolio.latencies) if portfolio.latencies else 0
        uptime_seconds = (datetime.now() - portfolio.start_time).total_seconds()

        # Best/worst trader
        best_trader = worst_trader = None
        if portfolio.wallet_stats:
            best = max(portfolio.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            worst = min(portfolio.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            best_trader = {"name": best[0], "pnl": round(best[1]["pnl"], 2)}
            worst_trader = {"name": worst[0], "pnl": round(worst[1]["pnl"], 2)}

        return web.json_response({
            "balance": round(portfolio.balance, 2),
            "portfolio_value": round(pv, 2),
            "realised_pnl": round(portfolio.realised_pnl, 2),
            "unrealised_pnl": round(unrealised, 2),
            "open_positions": len(portfolio.positions),
            "total_trades": len(portfolio.trades),
            "daily_volume": round(portfolio.daily_volume, 2),
            "exposure": round(exposure, 2),
            "roi_percent": round(roi, 2),
            "win_rate": round(win_rate, 2),
            "wins": total_wins,
            "losses": total_losses,
            "avg_latency_ms": round(avg_latency, 1),
            "uptime_hours": round(uptime_seconds / 3600, 1),
            "change_24h": round(pv - portfolio.value_24h_ago, 2),
            "best_trader": best_trader,
            "worst_trader": worst_trader
        })

    async def get_categories(request):
        """Return category breakdown for pie chart."""
        # Aggregate categories from all trades using detect_category
        cat_stats = {}
        for trade in portfolio.trades:
            market_name = trade.get("market", "")
            category = trade.get("category")
            if not category or category == "Other":
                category = detect_category(market_name)
            if category not in cat_stats:
                cat_stats[category] = {"trades": 0, "volume": 0, "pnl": 0}
            cat_stats[category]["trades"] += 1
            cat_stats[category]["volume"] += trade.get("copy_size", 0)
            cat_stats[category]["pnl"] += trade.get("trade_pnl", 0)

        categories = []
        for cat, stats in cat_stats.items():
            categories.append({
                "category": cat,
                "trades": stats["trades"],
                "volume": round(stats["volume"], 2),
                "pnl": round(stats["pnl"], 2)
            })
        categories.sort(key=lambda x: x["trades"], reverse=True)
        return web.json_response(categories)

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
            "endpoints": ["/summary", "/categories", "/trades", "/all", "/closed", "/wallets", "/risk", "/reconcile", "/withdrawal", "/prometheus"],
            "status": "running"
        })

    async def get_metrics(request):
        """Prometheus metrics endpoint."""
        from prometheus_client import generate_latest
        metrics = generate_latest()
        return web.Response(text=metrics.decode('utf-8'), content_type='text/plain')

    async def get_withdrawal_status(request):
        """Return auto-withdrawal status and history."""
        if not auto_withdrawal:
            return web.json_response({"enabled": False, "message": "Auto-withdrawal not configured"})
        status = auto_withdrawal.get_status()
        status["recent_withdrawals"] = auto_withdrawal.withdrawal_history[-10:]  # Last 10
        return web.json_response(status)

    async def trigger_withdrawal(request):
        """Manually trigger a withdrawal check."""
        if not auto_withdrawal or not auto_withdrawal.enabled:
            return web.json_response({"error": "Auto-withdrawal not enabled"}, status=400)
        result = await auto_withdrawal.check_and_withdraw(portfolio.realised_pnl)
        return web.json_response(result)

    app = web.Application()
    app.router.add_get("/", get_root)
    app.router.add_get("/prometheus", get_metrics)
    app.router.add_get("/trades", get_trades)
    app.router.add_get("/closed", get_closed_trades)
    app.router.add_get("/all", get_all_trades)
    app.router.add_get("/summary", get_summary)
    app.router.add_get("/categories", get_categories)
    app.router.add_get("/wallets", get_wallets)
    app.router.add_get("/risk", get_risk_status)
    app.router.add_get("/reconcile", reconcile_positions)
    app.router.add_get("/withdrawal", get_withdrawal_status)
    app.router.add_post("/withdrawal/trigger", trigger_withdrawal)

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

    # Initialize auto-withdrawal
    auto_withdrawal = AutoWithdrawal()

    portfolio = Portfolio()
    tracker = CigarettesTracker(portfolio, auto_withdrawal)

    # Start trades API
    await run_trades_api(portfolio, auto_withdrawal)

    print("Dashboard: http://localhost:3001/d/poly-copy-trade")

    try:
        await tracker.run()
    except KeyboardInterrupt:
        await tracker.stop()
        portfolio.summary()


if __name__ == "__main__":
    asyncio.run(main())
