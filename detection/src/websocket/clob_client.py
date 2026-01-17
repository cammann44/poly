"""CLOB WebSocket Client for real-time trade detection.

Connects to Polymarket CLOB WebSocket to monitor trades from target wallets.
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import orjson
import structlog
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

logger = structlog.get_logger(__name__)


@dataclass
class DetectedTrade:
    """Represents a detected trade from a target wallet."""
    timestamp: float
    wallet: str
    market_id: str
    token_id: str
    side: str  # BUY or SELL
    size: float
    price: float
    order_id: str
    source: str  # CLOB or ONCHAIN


class CLOBWebSocketClient:
    """WebSocket client for Polymarket CLOB trade monitoring."""

    def __init__(
        self,
        wss_url: str,
        target_wallets: list[str],
        publisher,
        config: dict
    ):
        self.wss_url = wss_url
        self.target_wallets = set(w.lower() for w in target_wallets)
        self.publisher = publisher
        self.config = config

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False
        self.reconnect_attempts = 0

        # Deduplication cache: (wallet, market_id, order_id) -> timestamp
        self._seen_trades: dict[tuple, float] = {}
        self._dedup_window_ms = config.get("dedup_window_ms", 500)

        # Subscribed markets
        self._subscribed_markets: set[str] = set()

        # Metrics
        self.metrics = {
            "messages_received": 0,
            "trades_detected": 0,
            "reconnections": 0,
        }

    async def run(self):
        """Main run loop with automatic reconnection."""
        self.running = True

        while self.running:
            try:
                await self._connect_and_listen()
            except (ConnectionClosedError, ConnectionClosedOK) as e:
                logger.warning("websocket_connection_closed", error=str(e))
            except Exception as e:
                logger.error("websocket_error", error=str(e))

            if self.running:
                await self._handle_reconnect()

    async def _connect_and_listen(self):
        """Connect to WebSocket and process messages."""
        logger.info("connecting_to_clob_websocket", url=self.wss_url)

        async with websockets.connect(
            self.wss_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self.ws = ws
            self.reconnect_attempts = 0
            logger.info("clob_websocket_connected")

            # Subscribe to trade channel
            await self._subscribe_trades()

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat())

            try:
                async for message in ws:
                    await self._handle_message(message)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _subscribe_trades(self):
        """Subscribe to trade events."""
        # Subscribe to user trades channel
        subscribe_msg = {
            "type": "subscribe",
            "channel": "user",
            "assets_ids": [],  # Empty = all markets
        }
        await self.ws.send(orjson.dumps(subscribe_msg).decode())
        logger.info("subscribed_to_trades")

    async def _handle_message(self, raw_message: str):
        """Process incoming WebSocket message."""
        self.metrics["messages_received"] += 1

        try:
            message = orjson.loads(raw_message)
        except orjson.JSONDecodeError:
            logger.warning("invalid_json_message", message=raw_message[:100])
            return

        msg_type = message.get("type") or message.get("event_type")

        if msg_type == "trade":
            await self._handle_trade(message)
        elif msg_type == "order":
            await self._handle_order(message)
        elif msg_type == "subscribed":
            logger.debug("subscription_confirmed", channel=message.get("channel"))
        elif msg_type == "error":
            logger.error("websocket_error_message", error=message)

    async def _handle_trade(self, trade: dict):
        """Process trade message and check if from target wallet."""
        # Extract trade details
        maker = trade.get("maker", "").lower()
        taker = trade.get("taker", "").lower()

        # Check if either party is a target wallet
        target_wallet = None
        if maker in self.target_wallets:
            target_wallet = maker
            side = "SELL" if trade.get("side") == "BUY" else "BUY"  # Maker is counterparty
        elif taker in self.target_wallets:
            target_wallet = taker
            side = trade.get("side", "BUY")

        if not target_wallet:
            return

        # Deduplication check
        trade_key = (target_wallet, trade.get("market"), trade.get("id"))
        now = time.time() * 1000

        if trade_key in self._seen_trades:
            if now - self._seen_trades[trade_key] < self._dedup_window_ms:
                return
        self._seen_trades[trade_key] = now

        # Clean old entries from dedup cache
        self._cleanup_dedup_cache(now)

        # Create detected trade
        detected = DetectedTrade(
            timestamp=now,
            wallet=target_wallet,
            market_id=trade.get("market", ""),
            token_id=trade.get("asset_id", ""),
            side=side,
            size=float(trade.get("size", 0)),
            price=float(trade.get("price", 0)),
            order_id=trade.get("id", ""),
            source="CLOB"
        )

        logger.info(
            "trade_detected",
            wallet=target_wallet[:10],
            market=detected.market_id[:10],
            side=side,
            size=detected.size,
            price=detected.price
        )

        self.metrics["trades_detected"] += 1

        # Publish to decision service
        await self.publisher.publish_trade(detected)

    async def _handle_order(self, order: dict):
        """Process order message (placement/cancellation)."""
        owner = order.get("owner", "").lower()

        if owner not in self.target_wallets:
            return

        order_type = order.get("type", "")
        logger.debug(
            "target_wallet_order",
            wallet=owner[:10],
            order_type=order_type,
            market=order.get("market", "")[:10]
        )

    def _cleanup_dedup_cache(self, now: float):
        """Remove old entries from deduplication cache."""
        cutoff = now - (self._dedup_window_ms * 2)
        keys_to_remove = [
            k for k, v in self._seen_trades.items() if v < cutoff
        ]
        for k in keys_to_remove:
            del self._seen_trades[k]

    async def _heartbeat(self):
        """Send periodic heartbeat to keep connection alive."""
        interval = self.config.get("heartbeat_interval_ms", 30000) / 1000

        while self.running and self.ws:
            await asyncio.sleep(interval)
            try:
                await self.ws.ping()
            except Exception:
                break

    async def _handle_reconnect(self):
        """Handle reconnection with exponential backoff."""
        max_attempts = self.config.get("max_reconnect_attempts", 10)
        base_delay = self.config.get("reconnect_delay_ms", 1000) / 1000

        self.reconnect_attempts += 1
        self.metrics["reconnections"] += 1

        if self.reconnect_attempts > max_attempts:
            logger.error("max_reconnect_attempts_exceeded")
            self.running = False
            return

        delay = min(base_delay * (2 ** (self.reconnect_attempts - 1)), 60)
        logger.info(
            "reconnecting",
            attempt=self.reconnect_attempts,
            delay_seconds=delay
        )
        await asyncio.sleep(delay)

    async def stop(self):
        """Stop the WebSocket client."""
        logger.info("stopping_clob_websocket")
        self.running = False

        if self.ws:
            await self.ws.close()
            self.ws = None
