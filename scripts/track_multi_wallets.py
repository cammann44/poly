#!/usr/bin/env python3
"""
Track target Polymarket wallets via on-chain + API data sources.

Monitors multiple wallets concurrently using:
1. On-chain ERC1155 transfers (Conditional Tokens)
2. Polymarket data API
3. WebSocket (backup)
"""

import asyncio
import json
import time
import os
import sqlite3
from collections import OrderedDict
import fcntl
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

# ============== RETRY UTILITY ==============
async def retry_with_backoff(coro_func, max_retries=5, base_delay=1, max_delay=60, context="operation"):
    """Retry an async function with exponential backoff.

    Args:
        coro_func: Async function to call (no arguments)
        max_retries: Maximum retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        context: Description for error messages

    Returns:
        Result of coro_func on success

    Raises:
        Last exception if all retries fail
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return await coro_func()
        except Exception as e:
            last_error = e
            if attempt == max_retries - 1:
                print(f"🚨 {context} failed after {max_retries} attempts: {e}")
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            print(f"⚠ {context} failed (attempt {attempt+1}/{max_retries}), retrying in {delay}s: {e}")
            await asyncio.sleep(delay)
    raise last_error

# ============== CONFIG ==============
# Handle both local dev (script in scripts/) and container (script in /app)
_script_dir = Path(__file__).resolve().parent
REPO_ROOT = _script_dir.parent if _script_dir.name == "scripts" else _script_dir
CONFIG_FILE = REPO_ROOT / "config" / "wallets.json"
KILL_SWITCH_FILE = REPO_ROOT / "config" / "KILL_SWITCH"
STARTING_BALANCE = 200000  # $200k
COPY_RATIO = 0.1  # Copy 10% of their trade size
MAX_COPY_SIZE = 500  # Max $500 per trade
MIN_COPY_SIZE = 10  # Min $10 per trade

# ============== RISK CONTROLS ==============
MAX_PORTFOLIO_EXPOSURE = 1.0  # Max 100% of capital in positions
TRAILING_STOP_LOSS = 0.20  # Exit if position drops 20% from peak
DAILY_LOSS_LIMIT = 0.10  # Stop trading if daily loss exceeds 10%
MIN_WALLET_WIN_RATE = 0.40  # Reduce copy ratio if wallet win rate < 40%

# Backfill settings (for reliability after downtime)
BACKFILL_ENABLED = os.environ.get("BACKFILL_ENABLED", "true").lower() in ["1", "true", "yes"]
BACKFILL_LOOKBACK_HOURS = int(os.environ.get("BACKFILL_LOOKBACK_HOURS", "24"))
DATA_API_LIMIT = int(os.environ.get("DATA_API_LIMIT", "50"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SEEN_TRADES_TTL_SECONDS = int(os.environ.get("SEEN_TRADES_TTL_SECONDS", "86400"))
SEEN_TRADES_MAX = int(os.environ.get("SEEN_TRADES_MAX", "50000"))

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

def detect_category(market_name: str, slug: str = "") -> str:
    """Detect category from market name keywords and slug patterns."""
    if not market_name and not slug:
        return "Other"
    q = (market_name or "").lower()
    s = (slug or "").lower()

    # Check slug prefixes first (most reliable for sports)
    if any(s.startswith(p) for p in ["nba-", "nfl-", "mlb-", "nhl-", "cbb-", "cfb-", "mls-", "ufc-", "pga-", "atp-", "wta-"]):
        return "Sports"

    if any(w in q for w in ["trump", "biden", "election", "congress", "senate", "president", "vote", "governor", "republican", "democrat", "iran", "russia", "ukraine", "china", "strike", "war", "military", "tariff", "sanctions"]):
        return "Politics"
    if any(w in q for w in ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "hockey", "sports", "game", "match", "vs.", "spread", "o/u", "super bowl", "premier league", "champions league", "world cup", "liverpool", "barcelona", "real madrid", "manchester", "chelsea", "arsenal", "tottenham", "broncos", "rams", "bills", "49ers", "bears", "texans", "chiefs", "eagles", "cowboys", "packers", "patriots", "lakers", "yankees", "cavaliers", "pacers", "pistons", "grizzlies", "spurs", "thunder", "heat", "celtics", "warriors", "bulls", "knicks", "nets", "clippers", "rockets", "mavericks", "suns", "76ers", "bucks", "hawks", "hornets", "magic", "wizards", "raptors", "jazz", "pelicans", "kings", "timberwolves", "blazers", "nuggets", "spartans", "bulldogs", "tigers", "tritons", "quakers", "flames", "hurricanes", "devils"]):
        return "Sports"
    if any(w in q for w in ["crypto", "bitcoin", "ethereum", "btc", "eth", "solana", "sol"]):
        return "Crypto"
    if any(w in q for w in ["fed", "rate", "inflation", "gdp", "economy", "stock", "market", "s&p", "dow", "nasdaq", "crude oil", "gold", "silver", "commodity", "treasury"]):
        return "Finance"
    if any(w in q for w in ["ai", "tech", "apple", "google", "microsoft", "openai", "meta"]):
        return "Tech"
    return "Other"

TRACKED_WALLETS = load_wallets()

# ============== ENDPOINTS ==============
POLYGON_RPC = "https://rpc.ankr.com/polygon"
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
# Use Railway volume if available, fallback to local
VOLUME_PATH = Path("/app/data")
if VOLUME_PATH.exists():
    LOG_FILE = VOLUME_PATH / "poly_trades.json"
    DB_FILE = VOLUME_PATH / "poly_trades.db"
    WITHDRAWAL_LOG_FILE = VOLUME_PATH / "withdrawals.json"
    MISSED_TRADES_FILE = VOLUME_PATH / "missed_trades.json"
    STATE_FILE = VOLUME_PATH / "tracker_state.json"
else:
    LOG_FILE = Path(__file__).parent / "logs" / "poly_trades.json"
    DB_FILE = Path(__file__).parent / "logs" / "poly_trades.db"
    WITHDRAWAL_LOG_FILE = Path(__file__).parent / "logs" / "withdrawals.json"
    MISSED_TRADES_FILE = Path(__file__).parent / "logs" / "missed_trades.json"
    STATE_FILE = Path(__file__).parent / "logs" / "tracker_state.json"
    LOG_FILE.parent.mkdir(exist_ok=True)

# Health monitoring config
HEALTH_CHECK_INTERVAL = 60  # Check every 60 seconds
MAX_PAUSED_DURATION = 300  # Alert if paused > 5 minutes while profitable
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # Optional alerting


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


class TradeStore:
    """Postgres-backed trade storage (fallback to SQLite if DATABASE_URL is unset)."""

    def __init__(self, db_path: Path, postgres_url: str = ""):
        self.db_path = db_path
        self.postgres_url = postgres_url
        self.use_postgres = bool(self.postgres_url)
        self.conn = None
        self._connect()
        self._init_schema()

    def _connect(self):
        if self.use_postgres:
            try:
                import psycopg2
                from psycopg2.extras import Json
            except Exception as e:
                raise RuntimeError("psycopg2 is required for Postgres support") from e
            self._psycopg2 = psycopg2
            self._pg_json = Json
            self.conn = psycopg2.connect(self.postgres_url, connect_timeout=5)
            self.conn.autocommit = True
        else:
            self.db_path.parent.mkdir(exist_ok=True)
            self.conn = sqlite3.connect(self.db_path)
            self.conn.execute("PRAGMA journal_mode=WAL")

    def _pg_run(self, func):
        try:
            return func()
        except (self._psycopg2.OperationalError, self._psycopg2.InterfaceError):
            self._connect()
            return func()

    def _init_schema(self):
        if self.use_postgres:
            def _init():
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS trades (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT,
                            token_id TEXT,
                            side TEXT,
                            trader TEXT,
                            data JSONB NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        """
                        DELETE FROM trades a
                        USING trades b
                        WHERE a.id > b.id
                          AND a.timestamp IS NOT DISTINCT FROM b.timestamp
                          AND a.token_id IS NOT DISTINCT FROM b.token_id
                          AND a.side IS NOT DISTINCT FROM b.side
                          AND a.trader IS NOT DISTINCT FROM b.trader
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS missed_trades (
                            id BIGSERIAL PRIMARY KEY,
                            timestamp TEXT,
                            token_id TEXT,
                            trader TEXT,
                            reason TEXT,
                            data JSONB NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        """
                        DELETE FROM missed_trades a
                        USING missed_trades b
                        WHERE a.id > b.id
                          AND a.timestamp IS NOT DISTINCT FROM b.timestamp
                          AND a.token_id IS NOT DISTINCT FROM b.token_id
                          AND a.trader IS NOT DISTINCT FROM b.trader
                          AND a.reason IS NOT DISTINCT FROM b.reason
                        """
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_dedupe ON trades (timestamp, token_id, side, trader)"
                    )
                    cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_missed_dedupe ON missed_trades (timestamp, token_id, trader, reason)"
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades (timestamp)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_token ON trades (token_id)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_missed_timestamp ON missed_trades (timestamp)")
            self._pg_run(_init)
        else:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    token_id TEXT,
                    side TEXT,
                    trader TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                DELETE FROM trades
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM trades
                    GROUP BY timestamp, token_id, side, trader
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    token_id TEXT,
                    trader TEXT,
                    reason TEXT,
                    data TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                DELETE FROM missed_trades
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM missed_trades
                    GROUP BY timestamp, token_id, trader, reason
                )
                """
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_dedupe ON trades (timestamp, token_id, side, trader)"
            )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_missed_dedupe ON missed_trades (timestamp, token_id, trader, reason)"
            )
            self.conn.commit()

    def has_trades(self) -> bool:
        if self.use_postgres:
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM trades LIMIT 1")
                    return cur.fetchone() is not None
            return bool(self._pg_run(_run))
        cur = self.conn.execute("SELECT 1 FROM trades LIMIT 1")
        return cur.fetchone() is not None

    def has_missed(self) -> bool:
        if self.use_postgres:
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM missed_trades LIMIT 1")
                    return cur.fetchone() is not None
            return bool(self._pg_run(_run))
        cur = self.conn.execute("SELECT 1 FROM missed_trades LIMIT 1")
        return cur.fetchone() is not None

    def load_trades(self) -> list:
        trades = []
        if self.use_postgres:
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute("SELECT data FROM trades ORDER BY id ASC")
                    return cur.fetchall()
            rows = self._pg_run(_run)
            for row in rows:
                payload = row[0]
                trades.append(payload if isinstance(payload, (dict, list)) else json.loads(payload))
            return trades
        for row in self.conn.execute("SELECT data FROM trades ORDER BY id ASC"):
            trades.append(json.loads(row[0]))
        return trades

    def load_missed_trades(self) -> list:
        trades = []
        if self.use_postgres:
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute("SELECT data FROM missed_trades ORDER BY id ASC")
                    return cur.fetchall()
            rows = self._pg_run(_run)
            for row in rows:
                payload = row[0]
                trades.append(payload if isinstance(payload, (dict, list)) else json.loads(payload))
            return trades
        for row in self.conn.execute("SELECT data FROM missed_trades ORDER BY id ASC"):
            trades.append(json.loads(row[0]))
        return trades

    def insert_trade(self, trade: dict):
        if self.use_postgres:
            data_payload = self._pg_json(trade)
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO trades (timestamp, token_id, side, trader, data) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (
                            trade.get("timestamp"),
                            trade.get("token_id"),
                            trade.get("side"),
                            trade.get("trader"),
                            data_payload,
                        ),
                    )
            self._pg_run(_run)
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO trades (timestamp, token_id, side, trader, data) VALUES (?, ?, ?, ?, ?)",
            (
                trade.get("timestamp"),
                trade.get("token_id"),
                trade.get("side"),
                trade.get("trader"),
                json.dumps(trade, separators=(",", ":")),
            ),
        )
        self.conn.commit()

    def insert_missed_trade(self, trade: dict):
        if self.use_postgres:
            data_payload = self._pg_json(trade)
            def _run():
                with self.conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO missed_trades (timestamp, token_id, trader, reason, data) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        (
                            trade.get("timestamp"),
                            trade.get("token_id"),
                            trade.get("trader"),
                            trade.get("reason"),
                            data_payload,
                        ),
                    )
            self._pg_run(_run)
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO missed_trades (timestamp, token_id, trader, reason, data) VALUES (?, ?, ?, ?, ?)",
            (
                trade.get("timestamp"),
                trade.get("token_id"),
                trade.get("trader"),
                trade.get("reason"),
                json.dumps(trade, separators=(",", ":")),
            ),
        )
        self.conn.commit()

    def bulk_insert_trades(self, trades: list):
        if self.use_postgres:
            rows = []
            for trade in trades:
                data_payload = self._pg_json(trade)
                rows.append(
                    (
                        trade.get("timestamp"),
                        trade.get("token_id"),
                        trade.get("side"),
                        trade.get("trader"),
                        data_payload,
                    )
                )
            def _run():
                prev_autocommit = self.conn.autocommit
                self.conn.autocommit = False
                try:
                    with self.conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO trades (timestamp, token_id, side, trader, data) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            rows,
                        )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
                finally:
                    self.conn.autocommit = prev_autocommit
            self._pg_run(_run)
            return
        rows = []
        for trade in trades:
            rows.append(
                (
                    trade.get("timestamp"),
                    trade.get("token_id"),
                    trade.get("side"),
                    trade.get("trader"),
                    json.dumps(trade, separators=(",", ":")),
                )
            )
        self.conn.executemany(
            "INSERT OR IGNORE INTO trades (timestamp, token_id, side, trader, data) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def bulk_insert_missed_trades(self, trades: list):
        if self.use_postgres:
            rows = []
            for trade in trades:
                data_payload = self._pg_json(trade)
                rows.append(
                    (
                        trade.get("timestamp"),
                        trade.get("token_id"),
                        trade.get("trader"),
                        trade.get("reason"),
                        data_payload,
                    )
                )
            def _run():
                prev_autocommit = self.conn.autocommit
                self.conn.autocommit = False
                try:
                    with self.conn.cursor() as cur:
                        cur.executemany(
                            "INSERT INTO missed_trades (timestamp, token_id, trader, reason, data) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            rows,
                        )
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
                finally:
                    self.conn.autocommit = prev_autocommit
            self._pg_run(_run)
            return
        rows = []
        for trade in trades:
            rows.append(
                (
                    trade.get("timestamp"),
                    trade.get("token_id"),
                    trade.get("trader"),
                    trade.get("reason"),
                    json.dumps(trade, separators=(",", ":")),
                )
            )
        self.conn.executemany(
            "INSERT OR IGNORE INTO missed_trades (timestamp, token_id, trader, reason, data) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

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
        self.daily_reset_date = datetime.now().date()  # Track when we last reset daily metrics
        self.paused_since = None  # Timestamp when trading was paused (for health monitoring)
        self.last_health_alert = None  # Prevent alert spam
        self.last_seen_trades = {addr: 0 for addr in TRACKED_WALLETS}  # wallet_addr -> last timestamp
        self.last_block = 0  # last processed on-chain block

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

        # Missed trades tracking
        self.missed_trades = []  # Store trades blocked by limits
        self.missed_trades_total = 0

        # Daily P&L tracking: date_str -> {pnl, trades, volume, wins, losses}
        self.daily_pnl = {}

        # Live price tracking for unrealised P&L
        self.position_prices = {}  # token_id -> current_price
        self.position_price_times = {}  # token_id -> timestamp of last successful price fetch
        self.last_price_update = 0

        # Persistent trade storage (Postgres/SQLite)
        self.store = None
        try:
            self.store = TradeStore(DB_FILE, DATABASE_URL)
        except Exception as e:
            print(f"⚠ Failed to initialize trade DB: {e}")

        # Error tracking for monitoring
        self.error_counts = {
            "api_failures": 0,
            "ws_disconnects": 0,
            "price_fetch_failures": 0,
            "state_save_failures": 0,
            "resolution_failures": 0
        }
        self.error_timestamps = {}  # error_type -> list of timestamps for windowed rate limiting
        self.alert_throttle = {}  # severity -> last alert timestamp

        # Set system start time
        SYSTEM_START_TIME.set(time.time())

        if restore_state:
            self._restore_from_log()

        BALANCE.set(self.balance)
        exposure = sum(p["cost"] for p in self.positions.values())
        portfolio_value = self.balance + exposure
        PORTFOLIO_VALUE.set(portfolio_value)

        # Set daily start value to current portfolio value on startup
        self.daily_start_value = portfolio_value
        self.trading_paused = False  # Always start with trading enabled
        print(f"📊 Daily start value set to current portfolio: ${portfolio_value:,.2f}")

        REALISED_PNL.set(self.realised_pnl)
        UNREALISED_PNL.set(self.get_unrealised_pnl())  # Will be 0 until prices fetched
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

        trades_source = []
        if self.store and self.store.has_trades():
            try:
                print("Restoring state from trade DB...")
                trades_source = self.store.load_trades()
            except Exception as e:
                print(f"   ⚠ Failed to load trades from DB: {e}")
                trades_source = []

        if not trades_source:
            # Seed volume with bundled log if volume is empty
            if VOLUME_PATH.exists() and not LOG_FILE.exists():
                bundled_log = Path(__file__).parent / "logs" / "poly_trades.json"
                if bundled_log.exists():
                    print(f"Seeding volume from bundled log: {bundled_log}")
                    import shutil
                    shutil.copy(bundled_log, LOG_FILE)

            if not LOG_FILE.exists():
                return

            print("Restoring state from trade log...")
            try:
                with open(LOG_FILE, "r") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        trades_source.append(json.loads(line))
            except Exception as e:
                print(f"   ⚠ Failed to read trade log: {e}")
                return

            # Backfill DB for reliability
            if self.store and trades_source:
                try:
                    self.store.bulk_insert_trades(trades_source)
                except Exception as e:
                    print(f"   ⚠ Failed to backfill trade DB: {e}")

        try:
            for trade in trades_source:
                self.trades.append(trade)
                self.daily_volume += trade.get("copy_size", 0)

                # Rebuild positions
                token_id = trade.get("token_id", "unknown")
                side = trade["side"]
                copy_size = trade.get("copy_size", 10)
                price = trade.get("price", 0.5)
                market = trade.get("market", "Unknown")
                wallet_name = trade.get("trader", next(iter(TRACKED_WALLETS.values()), "unknown"))

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
                    # Count all SELLs as closes
                    # Use stored trade_pnl if available, otherwise calculate
                    pnl = trade.get("trade_pnl", 0)

                    if token_id in self.positions:
                        pos = self.positions[token_id]

                        # If no trade_pnl stored, calculate it (legacy trades)
                        if pnl == 0 and pos["entry_price"] > 0:
                            entry_cost = trade.get("entry_cost", pos["cost"])
                            shares_sold = entry_cost / pos["entry_price"] if pos["entry_price"] > 0 else 0
                            pnl = (price - pos["entry_price"]) * shares_sold

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

                        # Update position tracking
                        entry_cost = trade.get("entry_cost", pos["cost"])
                        shares_sold = entry_cost / pos["entry_price"] if pos["entry_price"] > 0 else 0
                        pos["size"] -= shares_sold
                        pos["cost"] -= entry_cost
                        if pos["size"] <= 0.01 or pos["cost"] <= 0.01:
                            del self.positions[token_id]
                            if token_id in self.position_wallet:
                                del self.position_wallet[token_id]
                    else:
                        # Orphan SELL - use trade_pnl if available
                        self.realised_pnl += pnl
                        self.wallet_stats[wallet_name]["closed"] += 1
                        if pnl >= 0:
                            self.wallet_stats[wallet_name]["wins"] += 1
                        else:
                            self.wallet_stats[wallet_name]["losses"] += 1

            # Calculate open positions per wallet
            for token_id, wallet_name in self.position_wallet.items():
                if wallet_name in self.wallet_stats:
                    self.wallet_stats[wallet_name]["open"] += 1

            # Rebuild daily P&L from trade history
            for trade in self.trades:
                ts = trade.get("timestamp", "")
                if not ts:
                    continue
                try:
                    trade_date = ts[:10]  # Extract YYYY-MM-DD
                    if trade_date not in self.daily_pnl:
                        self.daily_pnl[trade_date] = {
                            "pnl": 0.0,
                            "trades": 0,
                            "volume": 0.0,
                            "wins": 0,
                            "losses": 0,
                            "timestamp": self._get_day_timestamp(trade_date)
                        }
                    self.daily_pnl[trade_date]["trades"] += 1
                    self.daily_pnl[trade_date]["volume"] += trade.get("copy_size", 0)
                    trade_pnl = trade.get("trade_pnl", 0)
                    if trade["side"] == "SELL":
                        self.daily_pnl[trade_date]["pnl"] += trade_pnl
                        if trade_pnl >= 0:
                            self.daily_pnl[trade_date]["wins"] += 1
                        else:
                            self.daily_pnl[trade_date]["losses"] += 1
                except Exception as e:
                    print(f"   ⚠ Error parsing trade for daily P&L: {e}")
            print(f"   📅 Rebuilt daily P&L for {len(self.daily_pnl)} days")

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

        # Restore missed trades
        if self.store and self.store.has_missed():
            try:
                self.missed_trades = self.store.load_missed_trades()
                self.missed_trades_total = len(self.missed_trades)
                print(f"   📊 Restored {len(self.missed_trades)} missed trades (total {self.missed_trades_total})")
            except Exception as e:
                print(f"   ⚠ Failed to restore missed trades from DB: {e}")
        elif MISSED_TRADES_FILE.exists():
            try:
                with open(MISSED_TRADES_FILE, "r") as f:
                    for line in f:
                        if line.strip():
                            self.missed_trades.append(json.loads(line))
                self.missed_trades_total = len(self.missed_trades)
                print(f"   📊 Restored {len(self.missed_trades)} missed trades (total {self.missed_trades_total})")
                if self.store and self.missed_trades:
                    try:
                        self.store.bulk_insert_missed_trades(self.missed_trades)
                    except Exception as e:
                        print(f"   ⚠ Failed to backfill missed trades DB: {e}")
            except Exception as e:
                print(f"   ⚠ Failed to restore missed trades: {e}")

        # Restore persistent state (trading_paused, daily_start_value, etc.)
        self._load_state()

    def record_error(self, error_type: str, threshold: int = 5, window_minutes: int = 5) -> bool:
        """Record an error and return True if threshold exceeded in window.

        Args:
            error_type: Category of error (api_failures, ws_disconnects, etc.)
            threshold: Number of errors before alerting
            window_minutes: Time window for counting errors

        Returns:
            True if threshold exceeded (should alert), False otherwise
        """
        now = time.time()
        self.error_counts[error_type] = self.error_counts.get(error_type, 0) + 1

        # Track timestamps for windowed rate
        if error_type not in self.error_timestamps:
            self.error_timestamps[error_type] = []
        self.error_timestamps[error_type].append(now)

        # Clean old timestamps outside window
        cutoff = now - (window_minutes * 60)
        self.error_timestamps[error_type] = [t for t in self.error_timestamps[error_type] if t > cutoff]

        # Return True if threshold exceeded in window
        return len(self.error_timestamps[error_type]) >= threshold

    def get_error_stats(self) -> dict:
        """Get current error statistics for /errors endpoint."""
        now = time.time()
        stats = {}
        for error_type, count in self.error_counts.items():
            timestamps = self.error_timestamps.get(error_type, [])
            # Count errors in last 5 minutes
            recent = len([t for t in timestamps if t > now - 300])
            stats[error_type] = {
                "total": count,
                "last_5_min": recent
            }
        return stats

    def _save_state(self):
        """Save critical state to file with file locking for safety."""
        try:
            state = {
                "trading_paused": self.trading_paused,
                "daily_start_value": self.daily_start_value,
                "daily_reset_date": self.daily_reset_date.isoformat(),
                "paused_since": self.paused_since.isoformat() if self.paused_since else None,
                "last_seen_trades": self.last_seen_trades,
                "last_block": self.last_block,
                "last_updated": datetime.now().isoformat()
            }
            with open(STATE_FILE, "w") as f:
                # Use exclusive lock to prevent concurrent writes
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(state, f, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            if self.record_error("state_save_failures"):
                print(f"🚨 CRITICAL: Repeated state save failures: {e}")
            else:
                print(f"⚠ Failed to save state: {e}")

    def _load_state(self):
        """Load persistent state from file."""
        if not STATE_FILE.exists():
            print("   No saved state file found, using defaults")
            return

        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)

            saved_date = datetime.fromisoformat(state.get("daily_reset_date", "2000-01-01")).date()
            today = datetime.now().date()

            # Restore last seen trade timestamps and last block regardless of day
            try:
                saved_last_seen = state.get("last_seen_trades", {})
                if isinstance(saved_last_seen, dict):
                    self.last_seen_trades = {
                        addr: int(saved_last_seen.get(addr, 0) or 0)
                        for addr in TRACKED_WALLETS
                    }
                else:
                    self.last_seen_trades = {addr: 0 for addr in TRACKED_WALLETS}
                self.last_block = int(state.get("last_block", 0) or 0)
            except Exception as e:
                print(f"   ⚠ Failed to restore last seen state: {e}")

            # Only restore if same day, otherwise start fresh
            if saved_date == today:
                self.trading_paused = state.get("trading_paused", False)
                self.daily_start_value = state.get("daily_start_value", self.daily_start_value)
                self.daily_reset_date = saved_date
                if state.get("paused_since"):
                    self.paused_since = datetime.fromisoformat(state["paused_since"])
                print(f"   📁 Restored state: paused={self.trading_paused}, daily_start=${self.daily_start_value:,.2f}")
            else:
                print(f"   📁 State file from {saved_date}, starting fresh for {today}")
                # New day - use current portfolio value as daily start
                pv = self.get_portfolio_value()
                self.daily_start_value = pv
                self.trading_paused = False
                self.daily_reset_date = today
        except Exception as e:
            print(f"   ⚠ Failed to load state: {e}")

    def _get_day_timestamp(self, date_str: str) -> int:
        """Return a unix timestamp for the start of the given date (YYYY-MM-DD)."""
        try:
            date_obj = datetime.fromisoformat(date_str)
        except ValueError:
            try:
                date_obj = datetime.fromisoformat(f"{date_str}T00:00:00")
            except ValueError:
                return int(datetime.now().timestamp())
        return int(datetime(date_obj.year, date_obj.month, date_obj.day).timestamp())

    def copy_trade(self, trade_data: dict, is_backfill: bool = False) -> bool:
        """Copy a real trade from a tracked wallet."""
        start = time.time()

        side = trade_data["side"]
        original_size = trade_data["size"]
        price = trade_data.get("price")
        market = trade_data["market"]
        token_id = trade_data.get("token_id", "unknown")
        wallet_name = trade_data.get("trader", "unknown")
        slug = trade_data.get("slug")
        category = trade_data.get("category", "Other")
        outcome = trade_data.get("outcome")

        # Ensure wallet exists in stats
        if wallet_name not in self.wallet_stats:
            self.wallet_stats[wallet_name] = {"trades": 0, "open": 0, "closed": 0, "pnl": 0.0, "volume": 0.0, "wins": 0, "losses": 0}

        # Validate size before sizing
        if original_size <= 0:
            print(f"⚠ Rejecting trade - invalid size: {original_size}")
            MISSED_TRADES.labels(reason="invalid_size").inc()
            if not is_backfill:
                self._record_missed_trade(trade_data, "invalid_size", intended_size=0)
            return False

        # Calculate copy size with smart sizing based on trader win rate
        base_ratio = COPY_RATIO
        stats = self.wallet_stats[wallet_name]
        total_closed = stats["wins"] + stats["losses"]

        if total_closed >= 3:  # Need at least 3 trades for win rate
            win_rate = stats["wins"] / total_closed

            # Tiered multiplier based on win rate
            if win_rate >= 0.70:
                multiplier = 1.5  # Star performer: 15% copy
            elif win_rate >= 0.60:
                multiplier = 1.25  # Good performer: 12.5% copy
            elif win_rate >= 0.50:
                multiplier = 1.0  # Average: 10% copy
            elif win_rate >= 0.40:
                multiplier = 0.7  # Below average: 7% copy
            else:
                multiplier = 0.4  # Poor performer: 4% copy

            # Confidence factor: scale up as we get more data (full confidence at 10+ trades)
            confidence = min(1.0, total_closed / 10)
            # Blend towards 1.0 with lower confidence
            adjusted_multiplier = 1.0 + (multiplier - 1.0) * confidence

            base_ratio *= adjusted_multiplier

            if adjusted_multiplier != 1.0:
                print(f"  📊 {wallet_name}: {win_rate:.0%} win rate ({total_closed} trades) → {adjusted_multiplier:.2f}x sizing")

        copy_size = original_size * base_ratio
        copy_size = max(MIN_COPY_SIZE, min(MAX_COPY_SIZE, copy_size))
        intended_copy_size = copy_size

        # Check kill switch
        if not is_backfill and check_kill_switch():
            print("🛑 KILL SWITCH ACTIVE - Trade blocked")
            MISSED_TRADES.labels(reason="kill_switch").inc()
            self._record_missed_trade(trade_data, "kill_switch", intended_copy_size)
            return False

        # Check if trading is paused due to daily loss
        if not is_backfill and self.trading_paused:
            print("⏸️ Trading paused - daily loss limit reached")
            MISSED_TRADES.labels(reason="daily_loss_limit").inc()
            self._record_missed_trade(trade_data, "daily_loss_limit", intended_copy_size)
            return False

        # Validate price - never use fake/invalid prices
        if price is None or price <= 0 or price > 1:
            print(f"⚠ Rejecting trade - invalid price: {price}")
            MISSED_TRADES.labels(reason="invalid_price").inc()
            if not is_backfill:
                self._record_missed_trade(trade_data, "invalid_price", intended_copy_size)
            return False

        # Check portfolio exposure limit for BUYs
        if side == "BUY" and not is_backfill:
            current_exposure = sum(p["cost"] for p in self.positions.values())
            # Use current portfolio value, not fixed starting balance
            portfolio_value = self.balance + current_exposure
            max_exposure = portfolio_value * MAX_PORTFOLIO_EXPOSURE
            if current_exposure + copy_size > max_exposure:
                print(f"  ⚠ Exposure limit: ${current_exposure:.0f} + ${copy_size:.0f} > ${max_exposure:.0f}")
                copy_size = max(0, max_exposure - current_exposure)
                if copy_size < MIN_COPY_SIZE:
                    print(f"  ❌ Cannot open position - max exposure reached")
                    MISSED_TRADES.labels(reason="max_exposure").inc()
                    if not is_backfill:
                        self._record_missed_trade(trade_data, "max_exposure", intended_copy_size)
                    return False

        fee = copy_size * 0.001
        trade_pnl = 0.0

        if side == "BUY":
            cost = copy_size + fee
            if cost > self.balance:
                print(f"  Insufficient balance: need ${cost:.2f}, have ${self.balance:.2f}")
                MISSED_TRADES.labels(reason="insufficient_balance").inc()
                if not is_backfill:
                    self._record_missed_trade(trade_data, "insufficient_balance", intended_copy_size)
                return False
            self.balance -= cost

            if token_id not in self.positions:
                self.positions[token_id] = {
                    "size": 0,
                    "cost": 0,
                    "market": market,
                    "entry_price": price,
                    "slug": slug,
                    "category": category,
                    "outcome": outcome
                }
                self.position_wallet[token_id] = wallet_name
                self.wallet_stats[wallet_name]["open"] += 1
                pos = self.positions[token_id]
                pos["cost"] += copy_size
                pos["size"] += copy_size / price if price > 0 else 0
                pos["entry_price"] = pos["cost"] / pos["size"] if pos["size"] > 0 else price
                pos["market"] = market
                pos["slug"] = slug
                pos["category"] = category
                if outcome:
                    pos["outcome"] = outcome
                # Track peak value for trailing stop-loss
                current_value = pos["size"] * price
                self.position_peaks[token_id] = max(self.position_peaks.get(token_id, 0), current_value)
                # Record price timestamp for age calculations
                self.position_prices[token_id] = price
                self.position_price_times[token_id] = time.time()
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

        timestamp_override = trade_data.get("timestamp")
        if isinstance(timestamp_override, (int, float)):
            trade_timestamp = datetime.fromtimestamp(timestamp_override).isoformat()
        elif isinstance(timestamp_override, str) and timestamp_override:
            trade_timestamp = timestamp_override
        else:
            trade_timestamp = datetime.now().isoformat()

        trade_record = {
            "timestamp": trade_timestamp,
            "side": side,
            "market": market,
            "category": category,
            "token_id": token_id,
            "slug": slug,
            "outcome": outcome,
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

        exposure = sum(p["cost"] for p in self.positions.values())
        portfolio_value = self.balance + exposure

        if not is_backfill:
            # Check for daily reset (midnight rollover)
            today = datetime.now().date()
            if today != self.daily_reset_date:
                print(f"🌅 Daily reset: new day {today}")
                self.daily_start_value = portfolio_value  # Start fresh from current value
                self.trading_paused = False  # Reset pause flag
                self.paused_since = None
                self.daily_reset_date = today
                self.daily_volume = 0  # Reset daily volume too
                self._save_state()

            # Check daily loss limit
            daily_pnl = portfolio_value - self.daily_start_value
            if daily_pnl < -STARTING_BALANCE * DAILY_LOSS_LIMIT:
                if not self.trading_paused:
                    self.trading_paused = True
                    self.paused_since = datetime.now()
                    self._save_state()
                    print(f"🚨 DAILY LOSS LIMIT REACHED: ${daily_pnl:+,.2f}")
            elif self.trading_paused and daily_pnl >= 0:
                # Resume trading if we've recovered to break-even or better
                self.trading_paused = False
                self.paused_since = None
                self._save_state()
                print(f"✅ Trading resumed - daily P&L recovered: ${daily_pnl:+,.2f}")

        # Track latency (keep last 100)
        self.latencies.append(latency)
        if len(self.latencies) > 100:
            self.latencies.pop(0)
        # Category stats
        if category not in self.category_stats:
            self.category_stats[category] = {"trades": 0, "pnl": 0.0, "volume": 0.0}
        self.category_stats[category]["trades"] += 1
        self.category_stats[category]["volume"] += copy_size
        if side == "SELL":
            self.category_stats[category]["pnl"] += trade_pnl

        # Daily P&L tracking
        if is_backfill:
            try:
                day_dt = datetime.fromisoformat(trade_timestamp.replace('Z', '+00:00'))
            except Exception:
                day_dt = datetime.now()
            today = day_dt.strftime("%Y-%m-%d")
        else:
            today = datetime.now().strftime("%Y-%m-%d")
        if today not in self.daily_pnl:
            self.daily_pnl[today] = {
                "pnl": 0.0,
                "trades": 0,
                "volume": 0.0,
                "wins": 0,
                "losses": 0,
                "timestamp": self._get_day_timestamp(today)
            }
        self.daily_pnl[today]["trades"] += 1
        self.daily_pnl[today]["volume"] += copy_size
        if side == "SELL":
            self.daily_pnl[today]["pnl"] += trade_pnl
            if trade_pnl >= 0:
                self.daily_pnl[today]["wins"] += 1
            else:
                self.daily_pnl[today]["losses"] += 1
        self._refresh_metrics()

        return True

    def _log_trade(self, trade: dict):
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(trade) + "\n")
        if self.store:
            try:
                self.store.insert_trade(trade)
            except Exception as e:
                print(f"⚠ Failed to persist trade to DB: {e}")

    def _log_missed_trade(self, trade: dict):
        with open(MISSED_TRADES_FILE, "a") as f:
            f.write(json.dumps(trade) + "\n")
        if self.store:
            try:
                self.store.insert_missed_trade(trade)
            except Exception as e:
                print(f"⚠ Failed to persist missed trade to DB: {e}")

    def _record_missed_trade(self, trade_data: dict, reason: str, intended_size: float | None = None):
        timestamp_override = trade_data.get("timestamp")
        if isinstance(timestamp_override, (int, float)):
            trade_timestamp = datetime.fromtimestamp(timestamp_override).isoformat()
        elif isinstance(timestamp_override, str) and timestamp_override:
            trade_timestamp = timestamp_override
        else:
            trade_timestamp = datetime.now().isoformat()

        original_size = trade_data.get("size", 0)
        missed_trade = {
            "timestamp": trade_timestamp,
            "trader": trade_data.get("trader", "unknown"),
            "side": trade_data.get("side", "UNKNOWN"),
            "market": trade_data.get("market", "Unknown"),
            "token_id": trade_data.get("token_id", "unknown"),
            "price": trade_data.get("price"),
            "intended_size": intended_size if intended_size is not None else original_size,
            "original_size": original_size,
            "reason": reason,
            "slug": trade_data.get("slug"),
            "category": trade_data.get("category", "Other")
        }
        self.missed_trades.append(missed_trade)
        self._log_missed_trade(missed_trade)
        self.missed_trades_total += 1

    def _refresh_metrics(self):
        """Update all Prometheus metrics that depend on portfolio state."""
        BALANCE.set(self.balance)
        DAILY_VOLUME.set(self.daily_volume)
        REALISED_PNL.set(self.realised_pnl)
        OPEN_POSITIONS.set(len(self.positions))

        exposure = sum(p["cost"] for p in self.positions.values())
        portfolio_value = self.balance + exposure
        TOTAL_EXPOSURE.set(exposure)
        PORTFOLIO_VALUE.set(portfolio_value)
        UNREALISED_PNL.set(self.get_unrealised_pnl())

        roi = ((portfolio_value - STARTING_BALANCE) / STARTING_BALANCE) * 100
        ROI_PERCENT.set(roi)

        avg_latency = sum(self.latencies) / len(self.latencies) if self.latencies else 0
        COPY_LATENCY_AVG.set(avg_latency)

        total_wins = sum(s["wins"] for s in self.wallet_stats.values())
        total_losses = sum(s["losses"] for s in self.wallet_stats.values())
        overall_win_rate = total_wins / (total_wins + total_losses) if (total_wins + total_losses) > 0 else 0.5
        WIN_RATE.set(overall_win_rate)

        CHANGE_24H.set(portfolio_value - self.value_24h_ago)

        if self.wallet_stats:
            best = max(self.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            worst = min(self.wallet_stats.items(), key=lambda x: x[1]["pnl"])
            BEST_TRADER_PNL.labels(wallet=best[0]).set(best[1]["pnl"])
            WORST_TRADER_PNL.labels(wallet=worst[0]).set(worst[1]["pnl"])

        for wname, stats in self.wallet_stats.items():
            WALLET_TRADES.labels(wallet=wname).set(stats["trades"])
            WALLET_OPEN.labels(wallet=wname).set(stats["open"])
            WALLET_CLOSED.labels(wallet=wname).set(stats["closed"])
            WALLET_PNL.labels(wallet=wname).set(stats["pnl"])
            WALLET_VOLUME.labels(wallet=wname).set(stats["volume"])
            total_closed = stats["wins"] + stats["losses"]
            win_rate = stats["wins"] / total_closed if total_closed > 0 else 0.5
            WALLET_WIN_RATE.labels(wallet=wname).set(win_rate)

        KILL_SWITCH_ACTIVE.set(1 if check_kill_switch() else 0)
        TRADING_PAUSED.set(1 if self.trading_paused else 0)

        DAILY_PNL.set(portfolio_value - self.daily_start_value)

    def check_stop_losses(self) -> list:
        """Check positions for trailing stop-loss triggers using live prices."""
        triggers = []
        for token_id, pos in list(self.positions.items()):
            peak = self.position_peaks.get(token_id)
            current_price = self.position_prices.get(token_id)
            if not peak or not current_price:
                continue
            current_value = pos.get("size", 0) * current_price
            if peak <= 0:
                continue
            drawdown = (peak - current_value) / peak
            if drawdown >= TRAILING_STOP_LOSS:
                triggers.append({
                    "token_id": token_id,
                    "market": pos.get("market", "Unknown"),
                    "drawdown": drawdown,
                    "peak": peak,
                    "current_price": current_price
                })
        return triggers

    def force_close_position(self, token_id: str, price: float, reason: str = "trailing_stop_loss") -> bool:
        """Force-close a position (used for trailing stop-loss or manual exits)."""
        pos = self.positions.pop(token_id, None)
        if not pos or price is None or price <= 0:
            return False

        shares = pos.get("size", 0)
        if shares <= 0:
            return False

        entry_price = pos.get("entry_price", price)
        wallet_name = self.position_wallet.pop(token_id, "unknown")
        proceeds = shares * price
        fee = proceeds * 0.001
        trade_pnl = (price - entry_price) * shares

        self.realised_pnl += trade_pnl
        self.balance += max(0, proceeds - fee)
        self.daily_volume += proceeds
        self.wallet_stats.setdefault(wallet_name, {"trades": 0, "open": 0, "closed": 0, "pnl": 0.0,
                                                   "volume": 0.0, "wins": 0, "losses": 0})
        stats = self.wallet_stats[wallet_name]
        stats["pnl"] += trade_pnl
        stats["closed"] += 1
        stats["open"] = max(0, stats["open"] - 1)
        stats["volume"] += proceeds
        if trade_pnl >= 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        if token_id in self.position_peaks:
            del self.position_peaks[token_id]
        self.position_prices.pop(token_id, None)
        self.position_price_times.pop(token_id, None)

        trade_record = {
            "timestamp": datetime.now().isoformat(),
            "side": "SELL",
            "market": pos.get("market", "Unknown"),
            "category": pos.get("category", "Other"),
            "token_id": token_id,
            "slug": pos.get("slug", ""),
            "outcome": pos.get("outcome"),
            "original_size": pos.get("cost", 0),
            "copy_size": shares * price,
            "price": price,
            "balance_after": self.balance,
            "pnl": self.realised_pnl,
            "trader": wallet_name,
            "trade_pnl": trade_pnl,
            "manual_close_reason": reason
        }
        self.trades.append(trade_record)
        self._log_trade(trade_record)

        CLOSED_TRADES.inc()

        today = datetime.now().strftime("%Y-%m-%d")
        if today not in self.daily_pnl:
            self.daily_pnl[today] = {"pnl": 0.0, "trades": 0, "volume": 0.0, "wins": 0, "losses": 0}
        self.daily_pnl[today]["trades"] += 1
        self.daily_pnl[today]["volume"] += shares * price
        self.daily_pnl[today]["pnl"] += trade_pnl
        if trade_pnl >= 0:
            self.daily_pnl[today]["wins"] += 1
        else:
            self.daily_pnl[today]["losses"] += 1

        self._refresh_metrics()
        return True

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
        """Get portfolio value using live prices if available, otherwise cost basis."""
        if self.position_prices:
            # Use live market values
            market_value = 0
            for token_id, pos in self.positions.items():
                if token_id in self.position_prices:
                    market_value += pos["size"] * self.position_prices[token_id]
                else:
                    market_value += pos["cost"]  # Fallback to cost basis
            return self.balance + market_value
        else:
            # Fallback to cost basis
            exposure = sum(p["cost"] for p in self.positions.values())
            return self.balance + exposure

    def get_unrealised_pnl(self) -> float:
        """Calculate true unrealised P&L using live prices.

        For positions without live prices (ended markets), use entry price
        as current price (contributing 0 to P&L). This prevents wild swings
        when price fetches fail intermittently.
        """
        unrealised = 0.0
        for token_id, pos in self.positions.items():
            entry_price = pos["entry_price"]
            shares = pos["size"]
            # Use live price if available, otherwise entry price (0 P&L)
            current_price = self.position_prices.get(token_id, entry_price)
            # P&L = (current - entry) * shares
            unrealised += (current_price - entry_price) * shares
        return unrealised

    def get_exposure(self) -> float:
        """Get total exposure (cost basis of open positions)."""
        return sum(p["cost"] for p in self.positions.values())

    def summary(self):
        pv = self.get_portfolio_value()
        roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
        runtime = datetime.now() - self.start_time

        print("\n" + "=" * 70)
        print("@MULTI-WALLET COPY-TRADING RESULTS")
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


class MultiWalletTracker:
    """Monitor tracked wallets via multiple data sources."""

    def __init__(self, portfolio: Portfolio, auto_withdrawal: AutoWithdrawal = None):
        self.portfolio = portfolio
        self.auto_withdrawal = auto_withdrawal
        self.running = False
        self.session = None
        self.seen_trades = OrderedDict()
        self.last_block = portfolio.last_block or 0
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

    def _trade_key(self, wallet_name: str, token_id: str, side: str, size: float, timestamp: float | int) -> str:
        return f"{wallet_name}:{token_id}:{side}:{size:.6f}:{int(timestamp)}"

    def _seen_prune(self, now: float | None = None):
        now = now or time.time()
        while self.seen_trades:
            _, ts = next(iter(self.seen_trades.items()))
            if now - ts <= SEEN_TRADES_TTL_SECONDS and len(self.seen_trades) <= SEEN_TRADES_MAX:
                break
            self.seen_trades.popitem(last=False)

    def _seen_contains(self, key: str, now: float | None = None) -> bool:
        ts = self.seen_trades.get(key)
        if ts is None:
            return False
        now = now or time.time()
        if now - ts > SEEN_TRADES_TTL_SECONDS:
            self.seen_trades.pop(key, None)
            return False
        return True

    def _seen_add(self, key: str, ts: float | None = None):
        now = ts or time.time()
        self.seen_trades[key] = now
        self.seen_trades.move_to_end(key)
        self._seen_prune(now)

    def _prime_seen_trades(self):
        wallet_by_name = {name: addr for addr, name in TRACKED_WALLETS.items()}
        for trade in self.portfolio.trades:
            try:
                ts = trade.get("timestamp", "")
                if isinstance(ts, (int, float)):
                    ts_epoch = int(ts)
                else:
                    ts_epoch = int(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp())
            except Exception:
                ts_epoch = 0
            size = float(trade.get("original_size", trade.get("copy_size", 0)) or 0)
            key = self._trade_key(trade.get("trader", "unknown"), trade.get("token_id", ""), trade.get("side", ""), size, ts_epoch)
            self._seen_add(key, ts_epoch if ts_epoch > 0 else None)
            trader = trade.get("trader", "")
            wallet_addr = wallet_by_name.get(trader)
            if wallet_addr:
                self.portfolio.last_seen_trades[wallet_addr] = max(self.portfolio.last_seen_trades.get(wallet_addr, 0), ts_epoch)

    async def backfill_recent_trades(self):
        if not BACKFILL_ENABLED:
            print("🕒 Backfill disabled")
            return
        lookback_seconds = BACKFILL_LOOKBACK_HOURS * 3600
        cutoff = time.time() - lookback_seconds

        # Last seen timestamp per wallet from persistent state
        last_seen = self.portfolio.last_seen_trades
        for addr in TRACKED_WALLETS:
            last_seen.setdefault(addr, 0)

        for wallet_addr, wallet_name in TRACKED_WALLETS.items():
            url = f"{POLYMARKET_DATA}/trades?user={wallet_addr}&limit=100"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        print(f"⚠ Backfill failed for {wallet_name}: status {resp.status}")
                        continue
                    trades = await resp.json()
            except Exception as e:
                print(f"⚠ Backfill error for {wallet_name}: {e}")
                continue

            # Process trades oldest-first
            for trade in reversed(trades):
                ts = int(trade.get("timestamp", 0) or 0)
                if ts <= 0:
                    continue
                if ts < cutoff or ts <= last_seen.get(wallet_addr, 0):
                    continue
                side = trade.get("side", "").upper()
                size = float(trade.get("size", 0))
                token_id = str(trade.get("asset", ""))
                slug = trade.get("slug", "")
                market_name = trade.get("title", f"Token {token_id[:20]}...")
                raw_price = trade.get("price")
                if raw_price is None or raw_price == 0:
                    last_seen[wallet_addr] = max(last_seen.get(wallet_addr, 0), ts)
                    continue
                price = float(raw_price)
                size_usd = size * price

                key = self._trade_key(wallet_name, token_id, side, size_usd, ts)
                if self._seen_contains(key):
                    last_seen[wallet_addr] = max(last_seen.get(wallet_addr, 0), ts)
                    continue
                self._seen_add(key, ts)

                category = detect_category(market_name, slug)
                cached = self.price_cache.get(str(token_id), {})
                outcome = cached.get("outcome")

                trade_data = {
                    "side": side,
                    "size": size_usd,
                    "price": price,
                    "market": market_name,
                    "token_id": token_id,
                    "trader": wallet_name,
                    "slug": slug,
                    "category": category,
                    "outcome": outcome,
                    "timestamp": ts,
                }

                print(f"🕒 Backfill {side} @{wallet_name} {market_name[:40]}...")
                self.portfolio.copy_trade(trade_data, is_backfill=True)
                last_seen[wallet_addr] = max(last_seen.get(wallet_addr, 0), ts)

        try:
            self.portfolio._save_state()
        except Exception:
            pass

    async def run(self):
        await self.start()
        self._prime_seen_trades()
        await self.backfill_recent_trades()

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
            self.periodic_status(),
            self.update_position_prices(),
            self.periodic_resolution_check(),
            self.health_monitor()
        ]
        if self.auto_withdrawal and self.auto_withdrawal.enabled:
            tasks.append(self.periodic_withdrawal_check())
        await asyncio.gather(*tasks)

    async def monitor_websocket(self):
        """Monitor Polymarket WebSocket for real-time trade activity.

        NOTE: The user channel requires API authentication (key, secret, passphrase).
        Without credentials, this will fail. API polling is used as the primary detection method.
        """
        # WebSocket disabled - requires API auth which we don't have
        # Keeping code for when API credentials become available
        print("🌐 WebSocket monitor disabled (requires API auth)")
        print("   Using API polling (every 10s) as primary detection")
        WEBSOCKET_CONNECTED.set(0)

        # Just keep the task alive but do nothing
        while self.running:
            await asyncio.sleep(60)

        # Original WebSocket code (requires auth):
        # ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        return

        ws_reconnect_attempts = 0
        while self.running:
            try:
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    WEBSOCKET_CONNECTED.set(1)
                    ws_reconnect_attempts = 0  # Reset on successful connection
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

            # Exponential backoff for reconnection
            ws_reconnect_attempts += 1
            delay = min(5 * (2 ** min(ws_reconnect_attempts - 1, 5)), 60)  # 5s -> 10s -> 20s -> 40s -> 60s max
            print(f"   Waiting {delay}s before reconnect attempt {ws_reconnect_attempts}...")
            await asyncio.sleep(delay)

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
                    timestamp_raw = data.get("timestamp") or data.get("time")
                    if isinstance(timestamp_raw, (int, float)):
                        timestamp = timestamp_raw
                    elif isinstance(timestamp_raw, str) and timestamp_raw:
                        try:
                            timestamp = datetime.fromisoformat(timestamp_raw.replace('Z', '+00:00')).timestamp()
                        except Exception:
                            timestamp = time.time()
                    else:
                        timestamp = time.time()

                    print(f"\n⚡ [WS] {side} detected from @{wallet_name}")

                    # Get market info
                    _, market_name, slug, category, outcome, option_name = await self.get_token_price(str(token_id))

                    trade_data = {
                        "side": side,
                        "size": size_usd,
                        "price": price,
                        "market": market_name,
                        "token_id": str(token_id),
                        "option_name": option_name,
                        "trader": wallet_name,
                        "slug": slug,
                        "category": category,
                        "outcome": outcome,
                        "timestamp": timestamp
                    }

                    # Deduplicate with seen_trades
                    trade_key = self._trade_key(wallet_name, str(token_id), side, size_usd, timestamp)
                    if not self._seen_contains(trade_key):
                        self._seen_add(trade_key, timestamp)
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
                current_block = int(data["result"], 16)
                if self.last_block > 0:
                    # Rewind a few blocks to avoid missing reorgs
                    self.last_block = max(self.last_block - 5, 0)
                    if self.last_block > current_block:
                        self.last_block = current_block
                    print(f"📦 Resuming from block {self.last_block} (current {current_block})")
                else:
                    self.last_block = current_block
                    print(f"📦 Starting from block {self.last_block}")
                self.portfolio.last_block = self.last_block
        except Exception as e:
            print(f"⚠ Failed to get block number: {e}")
            if self.last_block <= 0:
                self.last_block = 67000000  # Fallback
            print(f"📦 Using fallback block {self.last_block}")

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
            if "result" not in data:
                return  # RPC error, skip this cycle
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
        self.portfolio.last_block = self.last_block

    async def process_transfer(self, log: dict, side: str, wallet_name: str = "unknown"):
        """Process an on-chain transfer event."""
        tx_hash = log.get("transactionHash", "")
        event_ts = time.time()

        if self._seen_contains(tx_hash, event_ts):
            return
        self._seen_add(tx_hash, event_ts)

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
        price, market_name, slug, category, outcome, option_name = await self.get_token_price(str(token_id))

        # Skip trade if we couldn't get a real price
        if price is None:
            print(f"\n⚠ Skipping {side} from @{wallet_name} - couldn't fetch real price")
            print(f"   Market: {market_name}")
            return

        print(f"\n{'[BUY]' if side == 'BUY' else '[SELL]'} @{wallet_name} {side}")
        print(f"   Market:   {market_name}")
        if option_name:
            print(f"   Option:   {option_name} ({outcome})")
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
            "category": category,
            "outcome": outcome,
            "option_name": option_name,
            "timestamp": event_ts
        }

        success = self.portfolio.copy_trade(trade_data)

        if success:
            copy_size = min(MAX_COPY_SIZE, max(MIN_COPY_SIZE, size_usd * COPY_RATIO))
            print(f"   -> COPIED: {side} ${copy_size:.2f} @ ${price:.4f}")
            print(f"   -> Balance: ${self.portfolio.balance:,.2f} | P&L: ${self.portfolio.realised_pnl:+,.2f}")

    async def get_token_price(self, token_id: str) -> tuple[float, str, str, str, str, str]:
        """Fetch real price, market name, slug, category, outcome, and option name for a token ID.
        Returns None for price if unable to fetch real price - NEVER uses fake/default prices.
        Returns: (price, market_name, slug, category, outcome, option_name)
        """
        # Check cache first
        if token_id in self.price_cache:
            cached = self.price_cache[token_id]
            if time.time() - cached["time"] < 60:  # Cache for 60 seconds
                return cached["price"], cached["market"], cached.get("slug"), cached.get("category", "Other"), cached.get("outcome"), cached.get("option_name")

        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        price = None  # No default - must get real price
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

        # Get market name, slug, category, outcome, and option name from gamma API
        slug = None
        category = None
        outcome = None
        option_name = None
        try:
            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        market_data = data[0]
                        market_name = market_data.get("question", market_name)
                        slug = market_data.get("slug", "")

                        # Get the option name (e.g., "SRZP" for multi-outcome markets)
                        option_name = market_data.get("groupItemTitle")

                        # Determine outcome by matching token_id to clobTokenIds
                        clob_tokens = market_data.get("clobTokenIds", [])
                        outcomes_list = market_data.get("outcomes", ["Yes", "No"])
                        if token_id in clob_tokens:
                            idx = clob_tokens.index(token_id)
                            if idx < len(outcomes_list):
                                outcome = outcomes_list[idx]  # "Yes" or "No"

                        # Try to get category from API (multiple sources)
                        # 1. Direct category field
                        if market_data.get("category"):
                            category = market_data["category"]
                        # 2. Tags
                        elif market_data.get("tags"):
                            tags = market_data["tags"]
                            category = tags[0].get("label") if isinstance(tags[0], dict) else tags[0]
                        # 3. Events category or series title
                        elif market_data.get("events"):
                            event = market_data["events"][0]
                            if event.get("category"):
                                category = event["category"]
                            elif event.get("series"):
                                series_title = event["series"][0].get("title", "").lower()
                                if any(s in series_title for s in ["nba", "nfl", "mlb", "nhl", "ufc", "pga", "tennis", "soccer", "football"]):
                                    category = "Sports"
        except Exception as e:
            print(f"⚠ Error fetching category from gamma API for {token_id[:20]}: {e}")

        # If no category from API, use detect_category helper
        if not category or category == "Other":
            category = detect_category(market_name, slug)

        # Fallback to CLOB API if gamma didn't work
        if not slug and condition_id:
            try:
                url = f"https://clob.polymarket.com/markets/{condition_id}"
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not market_name or market_name.startswith("Token"):
                            market_name = data.get("question", market_name)
            except Exception as e:
                print(f"⚠ Error fetching from CLOB API for {condition_id}: {e}")

        # Cache the result
        self.price_cache[token_id] = {
            "price": price,
            "market": market_name,
            "slug": slug,
            "category": category,
            "outcome": outcome,
            "option_name": option_name,
            "time": time.time()
        }
        return price, market_name, slug, category, outcome, option_name

    async def get_market_name(self, token_id: str) -> str:
        """Get market name from token ID."""
        try:
            url = f"https://gamma-api.polymarket.com/markets?token_id={token_id}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return data[0].get("question", f"Token {token_id[:20]}...")
        except Exception as e:
            print(f"⚠ Error fetching market name for {token_id[:20]}: {e}")
        return f"Token {token_id[:20]}..."

    async def poll_data_api(self):
        """Poll Polymarket data API for recent trades."""
        import sys
        print("📡 Starting data API polling (every 10s)...", flush=True)

        # Track last seen trade timestamp per wallet
        last_seen = self.portfolio.last_seen_trades
        for addr in TRACKED_WALLETS:
            last_seen.setdefault(addr, 0)
        poll_count = 0

        api_backoff = 10
        while self.running:
            try:
                poll_count += 1
                if poll_count % 30 == 1:  # Log every 5 minutes
                    print(f"📡 API poll #{poll_count} - checking {len(TRACKED_WALLETS)} wallets", flush=True)
                for wallet_addr, wallet_name in TRACKED_WALLETS.items():
                    url = f"{POLYMARKET_DATA}/trades?user={wallet_addr}&limit={DATA_API_LIMIT}"
                    async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            trades = await resp.json()
                            # Process trades oldest-first to maintain correct last_seen
                            for trade in reversed(trades):
                                ts = int(trade.get("timestamp", 0) or 0)
                                if ts <= 0:
                                    continue
                                if ts > last_seen[wallet_addr]:
                                    # New trade detected
                                    side = trade.get("side", "").upper()
                                    size = float(trade.get("size", 0))
                                    token_id = trade.get("asset", "")
                                    slug = trade.get("slug", "")
                                    market_name = trade.get("title", f"Token {token_id[:20]}...")

                                    # Get real price - skip if not available
                                    raw_price = trade.get("price")
                                    if raw_price is None or raw_price == 0:
                                        print(f"\n⚠ Skipping {side} from @{wallet_name} - no price in API response")
                                        last_seen[wallet_addr] = max(last_seen.get(wallet_addr, 0), ts)
                                        continue
                                    price = float(raw_price)
                                    size_usd = size * price

                                    if side in ["BUY", "SELL"] and size > 0:
                                        # Deduplicate
                                        trade_key = self._trade_key(wallet_name, str(token_id), side, size_usd, ts)
                                        if not self._seen_contains(trade_key):
                                            self._seen_add(trade_key, ts)

                                            # Get category
                                            category = detect_category(market_name, slug)

                                            # Try to get outcome from cache
                                            cached = self.price_cache.get(str(token_id), {})
                                            outcome = cached.get("outcome")

                                            trade_data = {
                                                "side": side,
                                                "size": size_usd,
                                                "price": price,
                                                "market": market_name,
                                                "token_id": str(token_id),
                                                "trader": wallet_name,
                                                "slug": slug,
                                                "category": category,
                                                "outcome": outcome,
                                                "timestamp": ts
                                            }

                                            print(f"\n📡 [API] {side} detected from @{wallet_name}", flush=True)
                                            print(f"   Market: {market_name[:50]}...", flush=True)

                                            success = self.portfolio.copy_trade(trade_data)
                                            if success:
                                                print(f"   📡 API COPIED", flush=True)

                                    # Update last_seen after processing each trade
                                    last_seen[wallet_addr] = max(last_seen.get(wallet_addr, 0), ts)
                api_backoff = 10
                await asyncio.sleep(10)  # Poll every 10 seconds
            except Exception as e:
                print(f"⚠ API polling error: {e}")
                await asyncio.sleep(api_backoff)
                api_backoff = min(api_backoff * 2, 60)

    async def periodic_status(self):
        """Print status periodically."""
        while self.running:
            await asyncio.sleep(60)  # Every minute
            pv = self.portfolio.get_portfolio_value()
            roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
            unrealised = self.portfolio.get_unrealised_pnl()
            print(f"\n📊 Status: {len(self.portfolio.trades)} trades | Portfolio: ${pv:,.2f} | Unrealised: ${unrealised:+,.2f} | ROI: {roi:+.2f}% | Block: {self.last_block}\n")

    async def update_position_prices(self):
        """Periodically fetch live prices for all open positions."""
        print("💰 Starting position price updater (every 2 min)...")
        await asyncio.sleep(10)  # Initial delay to let things start

        price_backoff = 10
        while self.running:
            try:
                positions = list(self.portfolio.positions.keys())
                if not positions:
                    await asyncio.sleep(120)
                    continue

                updated = 0
                failed = 0
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

                # Batch fetch prices (max 10 concurrent to avoid rate limits)
                batch_size = 10
                for i in range(0, len(positions), batch_size):
                    batch = positions[i:i + batch_size]
                    tasks = []
                    for token_id in batch:
                        tasks.append(self._fetch_token_price(token_id, headers))

                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for token_id, result in zip(batch, results):
                        if isinstance(result, Exception):
                            failed += 1
                        elif result is not None:
                            self.portfolio.position_prices[token_id] = result
                            self.portfolio.position_price_times[token_id] = time.time()
                            pos = self.portfolio.positions.get(token_id)
                            if pos:
                                current_value = pos.get("size", 0) * result
                                self.portfolio.position_peaks[token_id] = max(self.portfolio.position_peaks.get(token_id, 0), current_value)
                            updated += 1
                        else:
                            failed += 1

                    # Small delay between batches to avoid rate limits
                    if i + batch_size < len(positions):
                        await asyncio.sleep(1)

                self.portfolio.last_price_update = time.time()

                triggers = self.portfolio.check_stop_losses()
                for trigger in triggers:
                    token_id = trigger["token_id"]
                    price = trigger["current_price"]
                    print(f"🚨 Trailing stop-loss triggered for {trigger['market']} (drawdown {trigger['drawdown']:.1%})")
                    self.portfolio.force_close_position(token_id, price)

                # Update metrics with live prices
                unrealised = self.portfolio.get_unrealised_pnl()
                pv = self.portfolio.get_portfolio_value()
                UNREALISED_PNL.set(unrealised)
                PORTFOLIO_VALUE.set(pv)
                roi = ((pv - STARTING_BALANCE) / STARTING_BALANCE) * 100
                ROI_PERCENT.set(roi)

                print(f"💰 Updated {updated}/{len(positions)} prices | Unrealised P&L: ${unrealised:+,.2f}")
                price_backoff = 10
                await asyncio.sleep(120)  # Update every 2 minutes
            except Exception as e:
                print(f"⚠ Price update error: {e}")
                await asyncio.sleep(price_backoff)
                price_backoff = min(price_backoff * 2, 60)

    async def _fetch_token_price(self, token_id: str, headers: dict) -> float:
        """Fetch current price for a single token using gamma API."""
        try:
            # Use gamma API for accurate market-implied prices
            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        market = data[0]
                        # Parse the JSON strings
                        clob_tokens = json.loads(market.get("clobTokenIds", "[]"))
                        outcome_prices = json.loads(market.get("outcomePrices", "[]"))

                        if token_id in clob_tokens and outcome_prices:
                            idx = clob_tokens.index(token_id)
                            if idx < len(outcome_prices):
                                return float(outcome_prices[idx])
        except Exception as e:
            print(f"⚠ Error fetching price from gamma API: {e}")
        return None

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

    async def periodic_resolution_check(self):
        """Check for resolved markets every 5 minutes and close positions."""
        print("🏁 Starting resolution checker (every 5 min)...")
        await asyncio.sleep(60)  # Initial delay

        while self.running:
            try:
                headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
                resolved = 0
                wins = 0
                losses = 0
                total_pnl = 0

                positions_to_check = list(self.portfolio.positions.items())

                async with aiohttp.ClientSession() as session:
                    for token_id, pos in positions_to_check:
                        try:
                            url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
                            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data and len(data) > 0:
                                        market = data[0]
                                        is_closed = market.get("closed", False)
                                        accepting_orders = market.get("acceptingOrders", True)

                                        if is_closed and not accepting_orders:
                                            clob_tokens = json.loads(market.get("clobTokenIds", "[]"))
                                            outcome_prices = json.loads(market.get("outcomePrices", "[]"))

                                            if token_id in clob_tokens:
                                                idx = clob_tokens.index(token_id)
                                                if idx < len(outcome_prices):
                                                    resolution_price = float(outcome_prices[idx])
                                                    entry_price = pos.get("entry_price", 0)
                                                    shares = pos.get("size", 0)
                                                    pnl = (resolution_price - entry_price) * shares
                                                    # Actual return = shares * resolution_price
                                                    actual_return = shares * resolution_price

                                                    # Create SELL trade
                                                    sell_trade = {
                                                        "timestamp": datetime.now().isoformat(),
                                                        "wallet": pos.get("wallet", "unknown"),
                                                        "trader": self.portfolio.position_wallet.get(token_id, "unknown"),
                                                        "side": "SELL",
                                                        "token_id": token_id,
                                                        "market": pos.get("market", market.get("question", "Unknown")),
                                                        "slug": market.get("slug", ""),
                                                        "price": resolution_price,
                                                        "size": pos.get("original_size", 0),
                                                        "copy_size": actual_return,  # Store actual return, not cost
                                                        "entry_cost": pos.get("cost", 0),  # Store original cost for reference
                                                        "trade_pnl": pnl,
                                                        "outcome": "RESOLVED",
                                                        "resolution": "WIN" if resolution_price >= 0.5 else "LOSS"
                                                    }
                                                    self.portfolio.trades.append(sell_trade)
                                                    self.portfolio.realised_pnl += pnl
                                                    total_pnl += pnl

                                                    del self.portfolio.positions[token_id]
                                                    if token_id in self.portfolio.position_prices:
                                                        del self.portfolio.position_prices[token_id]
                                                    if token_id in self.portfolio.position_price_times:
                                                        del self.portfolio.position_price_times[token_id]

                                                    resolved += 1
                                                    if resolution_price >= 0.5:
                                                        wins += 1
                                                    else:
                                                        losses += 1
                        except Exception as e:
                            print(f"⚠ Error checking resolution for {token_id[:20]}: {e}")
                        await asyncio.sleep(0.05)  # Small delay between checks

                if resolved > 0:
                    self.portfolio.balance += total_pnl
                    # Save to log file
                    with open(LOG_FILE, 'w') as f:
                        for t in self.portfolio.trades:
                            f.write(json.dumps(t) + "\n")

                    REALISED_PNL.set(self.portfolio.realised_pnl)
                    BALANCE.set(self.portfolio.balance)
                    OPEN_POSITIONS.set(len(self.portfolio.positions))

                    print(f"🏁 Resolved {resolved} markets | Wins: {wins} | Losses: {losses} | P&L: ${total_pnl:+,.2f}")

            except Exception as e:
                print(f"⚠ Resolution check error: {e}")

            await asyncio.sleep(300)  # Check every 5 minutes

    async def health_monitor(self):
        """Monitor system health and send alerts for anomalies."""
        print("🏥 Starting health monitor...")
        await asyncio.sleep(30)  # Initial delay

        while self.running:
            try:
                pv = self.portfolio.get_portfolio_value()
                daily_pnl = pv - self.portfolio.daily_start_value

                # Check for stuck paused state while profitable
                if self.portfolio.trading_paused and self.portfolio.paused_since:
                    paused_duration = (datetime.now() - self.portfolio.paused_since).total_seconds()

                    # Alert if paused > 5 min but we're in profit
                    if paused_duration > MAX_PAUSED_DURATION and daily_pnl >= 0:
                        # Auto-fix: unpause since we're profitable
                        self.portfolio.trading_paused = False
                        self.portfolio.paused_since = None
                        self.portfolio._save_state()
                        alert_msg = f"🔧 Auto-fixed: Trading was paused for {paused_duration/60:.1f}min while profitable (${daily_pnl:+,.2f}). Unpaused."
                        print(alert_msg)
                        await self._send_alert(alert_msg)

                # Check for large unrealised loss (potential issue)
                unrealised = self.portfolio.get_unrealised_pnl()
                if unrealised < -5000:  # Alert on $5k+ unrealised loss
                    now = datetime.now()
                    if not self.portfolio.last_health_alert or (now - self.portfolio.last_health_alert).total_seconds() > 3600:
                        alert_msg = f"⚠️ Large unrealised loss: ${unrealised:,.2f}"
                        print(alert_msg)
                        await self._send_alert(alert_msg)
                        self.portfolio.last_health_alert = now

                # Periodic state save (every health check cycle)
                self.portfolio._save_state()

            except Exception as e:
                print(f"⚠ Health monitor error: {e}")

            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    async def _send_alert(self, message: str, severity: str = "warning"):
        """Send alert with severity levels and throttling.

        Args:
            message: Alert message
            severity: 'info', 'warning', or 'critical'
        """
        severity_config = {
            "info": {"emoji": "ℹ️", "throttle_seconds": 300},      # Max 1 per 5 min
            "warning": {"emoji": "⚠️", "throttle_seconds": 120},   # Max 1 per 2 min
            "critical": {"emoji": "🚨", "throttle_seconds": 0}     # Always send
        }

        config = severity_config.get(severity, severity_config["warning"])
        emoji = config["emoji"]
        throttle = config["throttle_seconds"]

        # Log to console always
        print(f"{emoji} [{severity.upper()}] {message}")

        # Check throttling (critical always sends)
        now = time.time()
        if throttle > 0:
            last_alert = self.portfolio.alert_throttle.get(severity, 0)
            if now - last_alert < throttle:
                return  # Throttled
            self.portfolio.alert_throttle[severity] = now

        # Send to Discord if configured
        if not DISCORD_WEBHOOK_URL:
            return

        try:
            payload = {"content": f"{emoji} **{severity.upper()}** - Poly Tracker\n{message}"}
            async with self.session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 204:
                    print(f"⚠ Discord alert failed: {resp.status}")
        except Exception as e:
            print(f"⚠ Failed to send alert: {e}")


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
        except Exception:
            return iso_timestamp[:16] if iso_timestamp else ""

    async def get_all_trades(request):
        """Return all trades with open/closed status, current price, and P&L."""
        all_trades = []
        open_positions = set(portfolio.positions.keys())

        # Build a map of BUY trades by token_id to link with SELLs
        buy_prices = {}  # token_id -> list of (price, timestamp) for BUYs
        for trade in portfolio.trades:
            if trade.get("side") == "BUY":
                token_id = trade.get("token_id", "")
                if token_id not in buy_prices:
                    buy_prices[token_id] = []
                buy_prices[token_id].append({
                    "price": trade.get("price", 0),
                    "timestamp": trade.get("timestamp", ""),
                    "outcome": trade.get("outcome")
                })

        def _normalize_price(value, fallback=0.0):
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        for trade in reversed(portfolio.trades):  # Most recent first
            token_id = trade.get("token_id", "unknown")
            side = trade["side"]
            trade_price = trade.get("price", 0)
            copy_size = trade.get("copy_size", 0)

            if side == "SELL":
                status = "CLOSED"
                exit_price = trade_price
                # Find the original BUY entry price
                entry_price = None
                outcome = trade.get("outcome")
                if token_id in buy_prices and buy_prices[token_id]:
                    # Use the earliest BUY price as entry
                    buy_info = buy_prices[token_id][0]
                    entry_price = buy_info["price"]
                    if not outcome:
                        outcome = buy_info.get("outcome")

                # Calculate P&L for SELL
                if entry_price and entry_price > 0 and exit_price:
                    pnl = (exit_price - entry_price) * (copy_size / exit_price) if exit_price > 0 else 0
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                else:
                    pnl = trade.get("trade_pnl", 0) or 0.0
                    pnl_pct = 0.0

                current_price = exit_price or entry_price  # Show exit price as "current" for closed trades
            else:
                # BUY trade
                entry_price = trade_price
                outcome = trade.get("outcome")

                if token_id in open_positions:
                    status = "OPEN"
                    current_price = portfolio.position_prices.get(token_id)
                    if current_price and entry_price > 0:
                        pnl_pct = ((current_price - entry_price) / entry_price) * 100
                        pnl = (current_price - entry_price) * (copy_size / entry_price)
                    else:
                        # No live price - use entry as current (0 P&L until we get price)
                        current_price = entry_price
                        pnl = 0.0
                        pnl_pct = 0.0
                else:
                    # Closed BUY - find the SELL price
                    status = "CLOSED"
                    sell_price = None
                    for t in portfolio.trades:
                        if t.get("token_id") == token_id and t.get("side") == "SELL":
                            sell_price = t.get("price")
                            break
                    current_price = sell_price if sell_price else entry_price
                    if sell_price and entry_price > 0:
                        pnl = (sell_price - entry_price) * (copy_size / entry_price)
                        pnl_pct = ((sell_price - entry_price) / entry_price) * 100
                    else:
                        pnl = 0.0
                        pnl_pct = 0.0

            # Use stored outcome, or derive from price as last resort
            if not outcome:
                # Try to get from trade data first
                outcome = trade.get("outcome")
            if not outcome:
                # Fallback: derive from price (less accurate for multi-outcome markets)
                price_for_outcome = entry_price if entry_price else (current_price if current_price else None)
                if price_for_outcome:
                    outcome = "Yes" if price_for_outcome >= 0.5 else "No"

            slug = trade.get("slug") or ""
            market_name = trade.get("market") or "Unknown"
            category = trade.get("category") or detect_category(market_name, slug)
            option_name = trade.get("option_name") or ""

            entry_price = entry_price if entry_price else _normalize_price(trade.get("price"))
            current_price = current_price if current_price else entry_price
            pnl = pnl if pnl is not None else 0.0
            pnl_pct = pnl_pct if pnl_pct is not None else 0.0

            # Calculate price age in minutes (only for open positions with live prices)
            price_age_min = 0
            if status == "OPEN" and token_id in portfolio.position_price_times:
                price_age_sec = time.time() - portfolio.position_price_times[token_id]
                price_age_min = round(price_age_sec / 60, 1)

            all_trades.append({
                "timestamp": format_timestamp(trade.get("timestamp", "")),
                "status": status,
                "category": category,
                "trader": trade.get("trader", "unknown"),
                "side": side,
                "outcome": outcome,
                "option": option_name,  # e.g., "SRZP" for multi-outcome markets
                "market": market_name,
                "slug": slug,
                "entry": round(entry_price, 4) if entry_price else 0,
                "current": round(current_price, 4) if current_price else 0,
                "size": round(copy_size, 2),
                "pnl": round(pnl, 2) if pnl is not None else 0,
                "pnl_pct": round(pnl_pct, 1) if pnl_pct is not None else 0,
                "price_age_min": price_age_min
            })
        return web.json_response(all_trades)

    async def download_csv(request):
        """Download trades as CSV for manual verification."""
        import io
        import csv

        output = io.StringIO()
        writer = csv.writer(output)

        # Header row
        writer.writerow([
            "Trade Timestamp",
            "Current Timestamp",
            "Status",
            "Side",
            "Market",
            "Option",
            "Outcome",
            "Buy Price",
            "Current/Sold Price",
            "Size ($)",
            "P&L ($)",
            "P&L %",
            "Price Age (min)",
            "Token ID",
            "Polymarket URL"
        ])

        open_positions = set(portfolio.positions.keys())
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for trade in portfolio.trades:
            token_id = trade.get("token_id", "unknown")
            side = trade.get("side")
            trade_price = trade.get("price", 0)
            copy_size = trade.get("copy_size", 0)
            slug = trade.get("slug", "")
            market_name = trade.get("market", "Unknown")
            option_name = trade.get("option_name", "")
            outcome = trade.get("outcome", "")

            if side == "SELL":
                status = "CLOSED"
                entry_price = None
                # Find original BUY price
                for t in portfolio.trades:
                    if t.get("token_id") == token_id and t.get("side") == "BUY":
                        entry_price = t.get("price")
                        if not outcome:
                            outcome = t.get("outcome", "")
                        if not slug:
                            slug = t.get("slug", "")
                        break
                current_price = trade_price  # Exit price
            else:
                entry_price = trade_price
                if token_id in open_positions:
                    status = "OPEN"
                    current_price = portfolio.position_prices.get(token_id)
                    if not current_price:
                        current_price = entry_price  # Fallback
                else:
                    status = "CLOSED"
                    # Find SELL price
                    current_price = None
                    for t in portfolio.trades:
                        if t.get("token_id") == token_id and t.get("side") == "SELL":
                            current_price = t.get("price")
                            break
                    if not current_price:
                        current_price = entry_price

            # Calculate P&L
            if entry_price and current_price and entry_price > 0:
                pnl = (current_price - entry_price) * (copy_size / entry_price)
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
            else:
                pnl = 0
                pnl_pct = 0

            # Price age
            price_age_min = ""
            if status == "OPEN" and token_id in portfolio.position_price_times:
                price_age_sec = time.time() - portfolio.position_price_times[token_id]
                price_age_min = round(price_age_sec / 60, 1)

            # Polymarket URL - use /market/ which redirects correctly
            poly_url = f"https://polymarket.com/market/{slug}" if slug else ""

            writer.writerow([
                trade.get("timestamp", ""),
                now_str,
                status,
                side,
                market_name,
                option_name,
                outcome,
                round(entry_price, 4) if entry_price else "",
                round(current_price, 4) if current_price else "",
                round(copy_size, 2),
                round(pnl, 2),
                round(pnl_pct, 1),
                price_age_min,
                token_id,
                poly_url
            ])

        # Return CSV response
        csv_content = output.getvalue()
        output.close()

        return web.Response(
            text=csv_content,
            content_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=polymarket_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            }
        )

    async def get_summary(request):
        """Return portfolio summary."""
        pv = portfolio.get_portfolio_value()
        unrealised = portfolio.get_unrealised_pnl()  # True unrealised using live prices
        exposure = portfolio.get_exposure()
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
            "worst_trader": worst_trader,
            "prices_updated": portfolio.last_price_update > 0,
            "prices_age_sec": round(time.time() - portfolio.last_price_update) if portfolio.last_price_update > 0 else 0
        })

    async def get_categories(request):
        """Return category breakdown for pie chart."""
        # Aggregate categories from all trades using detect_category
        cat_stats = {}
        for trade in portfolio.trades:
            market_name = trade.get("market", "")
            slug = trade.get("slug", "")
            category = trade.get("category")
            if not category or category == "Other":
                category = detect_category(market_name, slug)
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

    async def unpause_trading(request):
        """Manually unpause trading and reset daily start value."""
        old_paused = portfolio.trading_paused
        old_start_value = portfolio.daily_start_value

        # Reset pause flag and update daily start value to current portfolio value
        portfolio.trading_paused = False
        pv = portfolio.get_portfolio_value()
        portfolio.daily_start_value = pv
        portfolio.daily_reset_date = datetime.now().date()

        print(f"🔓 Trading manually unpaused. Daily start value: ${old_start_value:,.2f} → ${pv:,.2f}")

        return web.json_response({
            "success": True,
            "was_paused": old_paused,
            "old_daily_start_value": round(old_start_value, 2),
            "new_daily_start_value": round(pv, 2),
            "message": "Trading unpaused - daily metrics reset to current portfolio value"
        })

    async def recalculate_balance(request):
        """Completely rebuild portfolio state from trade log."""
        old_balance = portfolio.balance
        old_realised = portfolio.realised_pnl

        # Rebuild everything from scratch
        positions = {}  # token_id -> {cost, size, entry_price}
        balance = STARTING_BALANCE
        realised_pnl = 0
        wins = 0
        losses = 0

        for trade in portfolio.trades:
            token_id = trade.get("token_id", "")
            side = trade["side"]
            copy_size = trade.get("copy_size", 0)
            price = trade.get("price", 0)

            if side == "BUY":
                cost = copy_size * 1.001  # with fee
                balance -= cost
                shares = copy_size / price if price > 0 else 0

                if token_id not in positions:
                    positions[token_id] = {"cost": 0, "size": 0, "entry_price": 0}
                pos = positions[token_id]
                pos["cost"] += copy_size
                pos["size"] += shares
                pos["entry_price"] = pos["cost"] / pos["size"] if pos["size"] > 0 else price

            else:  # SELL
                # Calculate actual return and P&L
                if token_id in positions:
                    pos = positions[token_id]
                    entry_price = pos["entry_price"]
                    # For resolved trades, use resolution price
                    # For manual sells, use trade price
                    exit_price = price if price > 0 else 0

                    # How much are we selling?
                    sell_cost = min(copy_size, pos["cost"]) if trade.get("resolution") else trade.get("entry_cost", pos["cost"])
                    shares_sold = sell_cost / entry_price if entry_price > 0 else 0

                    # Actual return = shares * exit_price
                    actual_return = shares_sold * exit_price
                    pnl = actual_return - sell_cost

                    balance += actual_return * 0.999  # with fee
                    realised_pnl += pnl

                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1

                    # Update position
                    pos["size"] -= shares_sold
                    pos["cost"] -= sell_cost
                    if pos["size"] <= 0.01 or pos["cost"] <= 0.01:
                        del positions[token_id]
                else:
                    # Orphan SELL - position not tracked, use trade_pnl if available
                    pnl = trade.get("trade_pnl", 0)
                    realised_pnl += pnl
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1

        # Calculate open position value
        open_cost = sum(p["cost"] for p in positions.values())

        # Update portfolio state
        portfolio.balance = balance
        portfolio.realised_pnl = realised_pnl
        portfolio.positions = {k: {"cost": v["cost"], "size": v["size"], "entry_price": v["entry_price"], "market": ""}
                               for k, v in positions.items()}

        # Update metrics
        BALANCE.set(balance)
        REALISED_PNL.set(realised_pnl)
        OPEN_POSITIONS.set(len(positions))

        print(f"🔄 Rebuilt: Balance ${old_balance:,.2f} → ${balance:,.2f}, Realised ${old_realised:,.2f} → ${realised_pnl:,.2f}")
        print(f"   Open positions: {len(positions)}, Wins: {wins}, Losses: {losses}")

        return web.json_response({
            "success": True,
            "old_balance": round(old_balance, 2),
            "new_balance": round(balance, 2),
            "old_realised_pnl": round(old_realised, 2),
            "new_realised_pnl": round(realised_pnl, 2),
            "open_positions": len(positions),
            "open_cost": round(open_cost, 2),
            "wins": wins,
            "losses": losses
        })

    async def get_missed_trades(request):
        """Return missed trades for analysis."""
        # Calculate potential value for each missed trade
        missed_with_current = []
        for trade in portfolio.missed_trades:
            token_id = trade.get("token_id", "")
            entry_price = trade.get("price", 0)
            intended_size = trade.get("intended_size")
            if intended_size is None:
                intended_size = trade.get("size", 0)
            # Try to get current price
            current_price = entry_price  # Default to entry if can't fetch
            try:
                url = f"https://clob.polymarket.com/book?token_id={token_id}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("last_trade_price"):
                                current_price = float(data["last_trade_price"])
            except Exception as e:
                print(f"⚠ Error fetching missed trade price: {e}")

            potential_pnl = (current_price - entry_price) * (intended_size / entry_price) if entry_price > 0 else 0

            missed_with_current.append({
                **trade,
                "current_price": round(current_price, 4),
                "potential_pnl": round(potential_pnl, 2),
                "potential_pnl_pct": round((current_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0
            })

        total_missed_volume = sum((t.get("intended_size") if t.get("intended_size") is not None else t.get("size", 0)) for t in portfolio.missed_trades)
        total_potential_pnl = sum(t.get("potential_pnl", 0) for t in missed_with_current)

        return web.json_response({
            "total_missed": len(portfolio.missed_trades),
            "total_missed_volume": round(total_missed_volume, 2),
            "total_potential_pnl": round(total_potential_pnl, 2),
            "total_missed_logged": portfolio.missed_trades_total,
            "trades": missed_with_current[-100:]  # Last 100 missed trades
        })

    async def get_daily_pnl(request):
        """Return daily P&L breakdown for charts."""
        daily_data = []
        cumulative_pnl = 0
        for date_str in sorted(portfolio.daily_pnl.keys()):
            day = portfolio.daily_pnl[date_str]
            cumulative_pnl += day["pnl"]
            win_rate = day["wins"] / (day["wins"] + day["losses"]) if (day["wins"] + day["losses"]) > 0 else 0
            daily_data.append({
                "date": date_str,
                "timestamp": day.get("timestamp"),
                "pnl": round(day["pnl"], 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
                "trades": day["trades"],
                "volume": round(day["volume"], 2),
                "wins": day["wins"],
                "losses": day["losses"],
                "win_rate": round(win_rate, 2)
            })
        return web.json_response(daily_data)

    async def reset_entry_prices(request):
        """Reset entry prices to current prices for positions with default 0.5 price.
        This zeros out unrealised P&L but gives accurate going-forward tracking.
        """
        reset_count = 0
        skipped = 0

        for token_id, pos in portfolio.positions.items():
            entry_price = pos.get("entry_price", 0)
            # Only reset positions with default price (0.5)
            if entry_price == 0.5:
                current_price = portfolio.position_prices.get(token_id)
                if current_price and current_price != 0.5:
                    pos["entry_price"] = current_price
                    # Recalculate cost based on new entry price
                    pos["cost"] = pos["size"] * current_price
                    reset_count += 1
                else:
                    skipped += 1

        # Also update the trades in memory
        for trade in portfolio.trades:
            if trade.get("price") == 0.5 and trade.get("side") == "BUY":
                token_id = trade.get("token_id")
                current_price = portfolio.position_prices.get(token_id)
                if current_price and current_price != 0.5:
                    trade["price"] = current_price

        return web.json_response({
            "status": "success",
            "reset_count": reset_count,
            "skipped_no_price": skipped,
            "message": f"Reset {reset_count} positions to current market prices. Unrealised P&L recalculated."
        })

    async def update_trade_prices(request):
        """Update prices for specific trades (used for backfilling historical prices).
        POST body: {"updates": [{"token_id": "...", "timestamp": "...", "price": 0.067}, ...]}
        """
        try:
            data = await request.json()
            updates = data.get("updates", [])

            if not updates:
                return web.json_response({"error": "No updates provided"}, status=400)

            # Create lookup by token_id + timestamp
            update_map = {}
            for u in updates:
                key = (u.get("token_id", ""), u.get("timestamp", ""))
                if key[0] and key[1] and u.get("price"):
                    update_map[key] = float(u["price"])

            updated_count = 0
            # Update in-memory trades
            for trade in portfolio.trades:
                key = (trade.get("token_id", ""), trade.get("timestamp", ""))
                if key in update_map:
                    old_price = trade.get("price")
                    new_price = update_map[key]
                    if old_price != new_price:
                        trade["price"] = new_price
                        updated_count += 1

            # Also update positions if they have matching entry prices
            for token_id, pos in portfolio.positions.items():
                for u in updates:
                    if u.get("token_id") == token_id and pos.get("entry_price") in [0.5, 0, 0.0]:
                        new_price = float(u["price"])
                        pos["entry_price"] = new_price
                        pos["cost"] = pos["size"] * new_price

            # Rewrite the log file with updated prices
            if updated_count > 0:
                with open(LOG_FILE, 'w') as f:
                    for t in portfolio.trades:
                        f.write(json.dumps(t) + "\n")

            return web.json_response({
                "status": "success",
                "updates_received": len(updates),
                "trades_updated": updated_count
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def remove_fake_prices(request):
        """Remove all trades and positions with fake/default prices (0.5, 0).
        Also removes orphan SELLs (SELLs without matching BUYs)."""
        fake_prices = [0.5, 0, 0.0]

        # Count before
        trades_before = len(portfolio.trades)
        positions_before = len(portfolio.positions)

        # Remove ALL trades with fake prices (BUYs and SELLs)
        portfolio.trades = [t for t in portfolio.trades
                          if t.get("price") not in fake_prices]

        # Find all token_ids that have BUY trades
        buy_token_ids = {t.get("token_id") for t in portfolio.trades if t.get("side") == "BUY"}

        # Remove orphan SELLs (SELLs without matching BUYs)
        orphan_sells_before = len([t for t in portfolio.trades if t.get("side") == "SELL"])
        portfolio.trades = [t for t in portfolio.trades
                          if t.get("side") != "SELL" or t.get("token_id") in buy_token_ids]
        orphan_sells_removed = orphan_sells_before - len([t for t in portfolio.trades if t.get("side") == "SELL"])

        # Remove positions with fake entry prices
        positions_to_remove = [tid for tid, pos in portfolio.positions.items()
                              if pos.get("entry_price") in fake_prices]
        for tid in positions_to_remove:
            del portfolio.positions[tid]
            if tid in portfolio.position_prices:
                del portfolio.position_prices[tid]
            if tid in portfolio.position_wallet:
                del portfolio.position_wallet[tid]

        # Rewrite log file
        with open(LOG_FILE, 'w') as f:
            for t in portfolio.trades:
                f.write(json.dumps(t) + "\n")

        trades_removed = trades_before - len(portfolio.trades)
        positions_removed = positions_before - len(portfolio.positions)

        return web.json_response({
            "status": "success",
            "trades_removed": trades_removed,
            "orphan_sells_removed": orphan_sells_removed,
            "positions_removed": positions_removed,
            "trades_remaining": len(portfolio.trades),
            "positions_remaining": len(portfolio.positions)
        })

    async def backfill_outcomes(request):
        """Backfill outcomes, option names, slugs, and market names for existing trades."""
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        updated = 0
        failed = 0

        # Get unique token_ids that need data lookup
        tokens_to_lookup = set()
        for trade in portfolio.trades:
            # Check if any field is missing
            needs_update = (
                not trade.get("outcome") or
                trade.get("outcome") in ["YES", "NO"] or
                not trade.get("option_name") or
                not trade.get("slug") or
                (trade.get("market", "").startswith("Token "))
            )
            if needs_update:
                tokens_to_lookup.add(trade.get("token_id"))

        async with aiohttp.ClientSession() as session:
            for token_id in tokens_to_lookup:
                if not token_id:
                    continue
                try:
                    url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data and len(data) > 0:
                                market_data = data[0]
                                # Parse clobTokenIds and outcomes - they're JSON strings in the API
                                clob_tokens_raw = market_data.get("clobTokenIds", "[]")
                                outcomes_raw = market_data.get("outcomes", '["Yes", "No"]')
                                clob_tokens = json.loads(clob_tokens_raw) if isinstance(clob_tokens_raw, str) else clob_tokens_raw
                                outcomes_list = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

                                option_name = market_data.get("groupItemTitle")  # e.g., "SRZP"
                                slug = market_data.get("slug", "")
                                market_name = market_data.get("question", "")

                                outcome = None
                                if token_id in clob_tokens:
                                    idx = clob_tokens.index(token_id)
                                    if idx < len(outcomes_list):
                                        outcome = outcomes_list[idx]

                                # Update all trades with this token_id
                                for trade in portfolio.trades:
                                    if trade.get("token_id") == token_id:
                                        if outcome:
                                            trade["outcome"] = outcome
                                        if option_name:
                                            trade["option_name"] = option_name
                                        if slug:
                                            trade["slug"] = slug
                                        if market_name and (not trade.get("market") or trade.get("market", "").startswith("Token ")):
                                            trade["market"] = market_name
                                        updated += 1
                except Exception as e:
                    print(f"⚠ Error backfilling outcome for token: {e}")
                    failed += 1

                await asyncio.sleep(0.1)  # Rate limit

        # Rewrite log file
        with open(LOG_FILE, 'w') as f:
            for t in portfolio.trades:
                f.write(json.dumps(t) + "\n")

        return web.json_response({
            "status": "success",
            "tokens_checked": len(tokens_to_lookup),
            "trades_updated": updated,
            "failed": failed
        })

    async def resolve_ended_markets(request):
        """Check for resolved markets and close positions with final P&L."""
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        resolved = 0
        wins = 0
        losses = 0
        failed = 0
        total_pnl = 0

        # Get all open positions
        positions_to_check = list(portfolio.positions.items())

        async with aiohttp.ClientSession() as session:
            for token_id, pos in positions_to_check:
                try:
                    url = f"https://gamma-api.polymarket.com/markets?clob_token_ids={token_id}"
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data and len(data) > 0:
                                market = data[0]
                                is_closed = market.get("closed", False)
                                accepting_orders = market.get("acceptingOrders", True)

                                # Market is resolved if closed and not accepting orders
                                if is_closed and not accepting_orders:
                                    # Get resolution price for our token
                                    clob_tokens = json.loads(market.get("clobTokenIds", "[]"))
                                    outcome_prices = json.loads(market.get("outcomePrices", "[]"))

                                    if token_id in clob_tokens:
                                        idx = clob_tokens.index(token_id)
                                        if idx < len(outcome_prices):
                                            resolution_price = float(outcome_prices[idx])
                                            entry_price = pos.get("entry_price", 0)
                                            shares = pos.get("size", 0)

                                            # Calculate P&L
                                            pnl = (resolution_price - entry_price) * shares
                                            # Actual return = shares * resolution_price
                                            actual_return = shares * resolution_price

                                            # Create a SELL trade to record the close
                                            sell_trade = {
                                                "timestamp": datetime.now().isoformat(),
                                                "wallet": pos.get("wallet", "unknown"),
                                                "trader": portfolio.position_wallet.get(token_id, "unknown"),
                                                "side": "SELL",
                                                "token_id": token_id,
                                                "market": pos.get("market", market.get("question", "Unknown")),
                                                "slug": market.get("slug", ""),
                                                "price": resolution_price,
                                                "size": pos.get("original_size", 0),
                                                "copy_size": actual_return,  # Store actual return, not cost
                                                "entry_cost": pos.get("cost", 0),  # Store original cost for reference
                                                "trade_pnl": pnl,
                                                "outcome": "RESOLVED",
                                                "resolution": "WIN" if resolution_price >= 0.5 else "LOSS"
                                            }
                                            portfolio.trades.append(sell_trade)

                                            # Update realised P&L
                                            portfolio.realised_pnl += pnl
                                            total_pnl += pnl

                                            # Remove from open positions
                                            del portfolio.positions[token_id]
                                            if token_id in portfolio.position_prices:
                                                del portfolio.position_prices[token_id]
                                            if token_id in portfolio.position_price_times:
                                                del portfolio.position_price_times[token_id]

                                            resolved += 1
                                            if resolution_price >= 0.5:
                                                wins += 1
                                            else:
                                                losses += 1
                except Exception as e:
                    failed += 1

                await asyncio.sleep(0.1)  # Rate limit

        # Update balance with realised P&L
        portfolio.balance += total_pnl

        # Rewrite log file
        with open(LOG_FILE, 'w') as f:
            for t in portfolio.trades:
                f.write(json.dumps(t) + "\n")

        # Update metrics
        REALISED_PNL.set(portfolio.realised_pnl)
        BALANCE.set(portfolio.balance)
        OPEN_POSITIONS.set(len(portfolio.positions))

        return web.json_response({
            "status": "success",
            "positions_checked": len(positions_to_check),
            "resolved": resolved,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "failed": failed,
            "positions_remaining": len(portfolio.positions)
        })

    async def debug_positions(request):
        """Debug endpoint to verify position prices and P&L calculations."""
        positions_data = []
        total_unrealised = 0

        for token_id, pos in list(portfolio.positions.items())[:20]:  # First 20
            entry_price = pos.get("entry_price", 0)
            current_price = portfolio.position_prices.get(token_id)
            shares = pos.get("size", 0)
            cost = pos.get("cost", 0)
            market = pos.get("market", "Unknown")[:50]

            if current_price is not None:
                pos_pnl = (current_price - entry_price) * shares
                current_value = shares * current_price
            else:
                pos_pnl = 0
                current_value = cost

            total_unrealised += pos_pnl

            positions_data.append({
                "token_id": token_id[:20] + "...",
                "market": market,
                "entry_price": round(entry_price, 4),
                "current_price": round(current_price, 4) if current_price else 0,
                "shares": round(shares, 2),
                "cost": round(cost, 2),
                "current_value": round(current_value, 2),
                "pnl": round(pos_pnl, 2)
            })

        # Price distribution
        prices = list(portfolio.position_prices.values())
        price_stats = {
            "count": len(prices),
            "min": round(min(prices), 4) if prices else 0,
            "max": round(max(prices), 4) if prices else 0,
            "avg": round(sum(prices) / len(prices), 4) if prices else 0,
            "outside_0_1": len([p for p in prices if p < 0 or p > 1])
        }

        return web.json_response({
            "total_positions": len(portfolio.positions),
            "prices_fetched": len(portfolio.position_prices),
            "price_stats": price_stats,
            "sample_positions": positions_data,
            "calculated_unrealised_sample": round(total_unrealised, 2)
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
            "endpoints": ["/health", "/summary", "/categories", "/trades", "/all", "/download", "/closed", "/wallets", "/risk", "/missed", "/daily", "/reconcile", "/withdrawal", "/backfill-prices", "/prometheus"],
            "status": "running"
        })

    async def get_health(request):
        """Health check endpoint for monitoring."""
        pv = portfolio.get_portfolio_value()
        exposure = sum(p["cost"] for p in portfolio.positions.values())
        uptime = (datetime.now() - portfolio.start_time).total_seconds()
        daily_pnl = pv - portfolio.daily_start_value

        # Check if API polling is working (trades in last 10 minutes)
        recent_trades = [t for t in portfolio.trades[-20:] if t.get("timestamp")]
        api_healthy = len(recent_trades) > 0

        # Calculate paused duration if applicable
        paused_duration = None
        paused_but_profitable = False
        if portfolio.trading_paused and portfolio.paused_since:
            paused_duration = (datetime.now() - portfolio.paused_since).total_seconds()
            paused_but_profitable = daily_pnl >= 0

        # Determine overall health status
        if portfolio.trading_paused and paused_but_profitable:
            status = "warning"  # Paused while profitable is concerning
        elif not api_healthy:
            status = "degraded"
        else:
            status = "healthy"

        return web.json_response({
            "status": status,
            "uptime_seconds": round(uptime),
            "websocket_connected": WEBSOCKET_CONNECTED._value._value if hasattr(WEBSOCKET_CONNECTED, '_value') else 0,
            "total_trades": len(portfolio.trades),
            "open_positions": len(portfolio.positions),
            "missed_trades": len(portfolio.missed_trades),
            "portfolio_value": round(pv, 2),
            "exposure_pct": round(exposure / STARTING_BALANCE * 100, 1),
            "daily_pnl": round(daily_pnl, 2),
            "log_file": str(LOG_FILE),
            "state_file": str(STATE_FILE),
            "volume_mounted": VOLUME_PATH.exists(),
            "checks": {
                "api_polling": api_healthy,
                "log_writable": LOG_FILE.parent.exists(),
                "state_writable": STATE_FILE.parent.exists(),
                "kill_switch": check_kill_switch(),
                "trading_paused": portfolio.trading_paused,
                "paused_duration_sec": round(paused_duration) if paused_duration else 0,
                "paused_but_profitable": paused_but_profitable
            }
        })

    async def get_errors(request):
        """Return error statistics for monitoring."""
        return web.json_response({
            "errors": portfolio.get_error_stats(),
            "alert_throttle": {k: round(time.time() - v) for k, v in portfolio.alert_throttle.items()},
            "timestamp": datetime.now().isoformat()
        })

    async def get_metrics(request):
        """Prometheus metrics endpoint."""
        from prometheus_client import generate_latest
        metrics = generate_latest()
        return web.Response(text=metrics.decode('utf-8'), content_type='text/plain')

    async def trigger_price_update(request):
        """Manually trigger a full price update for all positions."""
        positions = list(portfolio.positions.keys())
        if not positions:
            return web.json_response({"status": "no_positions", "updated": 0})

        updated = 0
        failed = 0
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

        async with aiohttp.ClientSession() as session:
            batch_size = 20
            for i in range(0, len(positions), batch_size):
                batch = positions[i:i + batch_size]
                for token_id in batch:
                    try:
                        url = f"https://clob.polymarket.com/book?token_id={token_id}"
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                price = None
                                if data.get("last_trade_price"):
                                    price = float(data["last_trade_price"])
                                elif data.get("bids") and data.get("asks"):
                                    bids = data["bids"]
                                    asks = data["asks"]
                                    if bids and asks:
                                        price = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                                if price and 0 < price < 1:
                                    portfolio.position_prices[token_id] = price
                                    portfolio.position_price_times[token_id] = time.time()
                                    updated += 1
                                else:
                                    failed += 1
                            else:
                                failed += 1
                    except Exception as e:
                        print(f"⚠ Error refreshing price for {token_id[:20]}: {e}")
                        failed += 1

                # Small delay between batches
                if i + batch_size < len(positions):
                    await asyncio.sleep(0.5)

        portfolio.last_price_update = time.time()

        return web.json_response({
            "status": "success",
            "total_positions": len(positions),
            "updated": updated,
            "failed": failed
        })

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
    app.router.add_get("/health", get_health)
    app.router.add_get("/errors", get_errors)
    app.router.add_get("/prometheus", get_metrics)
    app.router.add_get("/trades", get_trades)
    app.router.add_get("/closed", get_closed_trades)
    app.router.add_get("/all", get_all_trades)
    app.router.add_get("/download", download_csv)
    app.router.add_get("/summary", get_summary)
    app.router.add_get("/categories", get_categories)
    app.router.add_get("/wallets", get_wallets)
    app.router.add_get("/risk", get_risk_status)
    app.router.add_post("/unpause", unpause_trading)
    app.router.add_post("/recalculate", recalculate_balance)
    app.router.add_get("/missed", get_missed_trades)
    app.router.add_get("/daily", get_daily_pnl)
    app.router.add_get("/debug", debug_positions)
    app.router.add_post("/reset-prices", reset_entry_prices)
    app.router.add_post("/backfill-prices", update_trade_prices)
    app.router.add_post("/remove-fake-prices", remove_fake_prices)
    app.router.add_post("/backfill-outcomes", backfill_outcomes)
    app.router.add_post("/resolve", resolve_ended_markets)
    app.router.add_get("/reconcile", reconcile_positions)
    app.router.add_post("/update-prices", trigger_price_update)
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
    tracker = MultiWalletTracker(portfolio, auto_withdrawal)

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
