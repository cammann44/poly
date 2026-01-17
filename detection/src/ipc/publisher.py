"""IPC Publisher for sending detected trades to Decision service.

Uses Unix domain sockets for low-latency inter-process communication.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

import orjson
import structlog

logger = structlog.get_logger(__name__)


class IPCPublisher:
    """Publishes trade signals via Unix domain socket."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.server: Optional[asyncio.Server] = None
        self.clients: list[asyncio.StreamWriter] = []
        self.running = False

        # Message queue for when no clients connected
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        # Metrics
        self.metrics = {
            "messages_published": 0,
            "messages_dropped": 0,
            "clients_connected": 0,
        }

    async def start(self):
        """Start the IPC server."""
        # Remove existing socket file if present
        socket_file = Path(self.socket_path)
        if socket_file.exists():
            socket_file.unlink()

        # Ensure directory exists
        socket_file.parent.mkdir(parents=True, exist_ok=True)

        # Start Unix socket server
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path
        )

        # Set socket permissions
        os.chmod(self.socket_path, 0o660)

        self.running = True
        logger.info("ipc_publisher_started", socket=self.socket_path)

        # Start message dispatcher
        asyncio.create_task(self._dispatch_messages())

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter
    ):
        """Handle new client connection."""
        self.clients.append(writer)
        self.metrics["clients_connected"] = len(self.clients)

        peer = writer.get_extra_info("peername")
        logger.info("ipc_client_connected", peer=str(peer))

        try:
            # Keep connection open until client disconnects
            while self.running:
                # Check if client is still connected
                try:
                    data = await asyncio.wait_for(reader.read(1), timeout=30)
                    if not data:
                        break
                except asyncio.TimeoutError:
                    # Send heartbeat
                    try:
                        writer.write(b"\n")
                        await writer.drain()
                    except Exception:
                        break
        except Exception as e:
            logger.error("ipc_client_error", error=str(e))
        finally:
            self.clients.remove(writer)
            self.metrics["clients_connected"] = len(self.clients)
            logger.info("ipc_client_disconnected")

            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def publish_trade(self, trade) -> bool:
        """Publish a detected trade to all connected clients.

        Args:
            trade: DetectedTrade dataclass instance

        Returns:
            True if message was queued/sent, False if dropped
        """
        message = {
            "type": "TRADE",
            "timestamp": trade.timestamp,
            "wallet": trade.wallet,
            "market_id": trade.market_id,
            "token_id": trade.token_id,
            "side": trade.side,
            "size": trade.size,
            "price": trade.price,
            "order_id": trade.order_id,
            "source": trade.source
        }

        try:
            self._message_queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            self.metrics["messages_dropped"] += 1
            logger.warning("message_queue_full_dropping_trade")
            return False

    async def _dispatch_messages(self):
        """Dispatch messages from queue to all clients."""
        while self.running:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            if not self.clients:
                # No clients connected, message is lost
                logger.debug("no_clients_connected_message_discarded")
                continue

            # Serialize message
            data = orjson.dumps(message) + b"\n"

            # Send to all clients
            disconnected = []
            for client in self.clients:
                try:
                    client.write(data)
                    await client.drain()
                    self.metrics["messages_published"] += 1
                except Exception as e:
                    logger.warning("failed_to_send_to_client", error=str(e))
                    disconnected.append(client)

            # Remove disconnected clients
            for client in disconnected:
                if client in self.clients:
                    self.clients.remove(client)
                    self.metrics["clients_connected"] = len(self.clients)

    async def stop(self):
        """Stop the IPC publisher."""
        logger.info("stopping_ipc_publisher")
        self.running = False

        # Close all client connections
        for client in self.clients:
            try:
                client.close()
                await client.wait_closed()
            except Exception:
                pass

        self.clients.clear()

        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        # Remove socket file
        socket_file = Path(self.socket_path)
        if socket_file.exists():
            socket_file.unlink()

        logger.info("ipc_publisher_stopped")
