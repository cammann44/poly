#!/usr/bin/env python3
"""Decision Engine Entry Point.

Receives trade signals from Detection service, applies sizing and risk filters,
and sends order requests to Execution service.
"""

import asyncio
import signal
import sys
from pathlib import Path

import structlog
import yaml
from dotenv import load_dotenv

from sizing.strategies import PositionSizer
from risk.filters import RiskManager
from state.positions import PositionTracker

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class DecisionEngine:
    """Main decision engine orchestrator."""

    def __init__(self, config_path: str = "../config/config.yaml"):
        self.config = self._load_config(config_path)
        self.wallets = self._load_wallets()
        self.running = False

        # Initialize components
        self.position_sizer = PositionSizer(self.config["decision"])
        self.risk_manager = RiskManager(self.config["risk"])
        self.position_tracker = PositionTracker()

        # IPC sockets
        self.detection_socket = self.config["ipc"]["detection_socket"]
        self.execution_socket = self.config["ipc"]["execution_socket"]

        # Execution connection
        self._exec_writer = None
        self._exec_reader = None

        # Metrics
        self.metrics = {
            "signals_received": 0,
            "orders_sent": 0,
            "orders_rejected": 0,
        }

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file."""
        config_file = Path(__file__).parent.parent.parent / "config" / "config.yaml"
        if not config_file.exists():
            config_file = Path(config_path)

        with open(config_file) as f:
            return yaml.safe_load(f)

    def _load_wallets(self) -> dict:
        """Load target wallets configuration."""
        wallets_file = Path(__file__).parent.parent.parent / "config" / "wallets.json"
        import json
        with open(wallets_file) as f:
            return json.load(f)

    def _get_wallet_config(self, address: str) -> dict:
        """Get configuration for a specific wallet."""
        for wallet in self.wallets["wallets"]:
            if wallet["address"].lower() == address.lower():
                return wallet
        return {}

    async def start(self):
        """Start the decision engine."""
        logger.info("starting_decision_engine")
        self.running = True

        # Handle shutdown signals
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        # Connect to execution service
        await self._connect_to_execution()

        # Start listening for detection signals
        await self._listen_for_signals()

    async def _connect_to_execution(self):
        """Connect to the execution service IPC socket."""
        max_retries = 10
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                self._exec_reader, self._exec_writer = await asyncio.open_unix_connection(
                    self.execution_socket
                )
                logger.info("connected_to_execution_service")
                return
            except (FileNotFoundError, ConnectionRefusedError):
                logger.warning(
                    "execution_service_not_available",
                    attempt=attempt + 1,
                    max_retries=max_retries
                )
                await asyncio.sleep(retry_delay)

        logger.error("failed_to_connect_to_execution_service")

    async def _listen_for_signals(self):
        """Listen for trade signals from detection service."""
        import orjson

        while self.running:
            try:
                reader, writer = await asyncio.open_unix_connection(
                    self.detection_socket
                )
                logger.info("connected_to_detection_service")

                while self.running:
                    line = await reader.readline()
                    if not line:
                        logger.warning("detection_service_disconnected")
                        break

                    # Skip heartbeat messages
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        signal = orjson.loads(line)
                        await self._process_signal(signal)
                    except orjson.JSONDecodeError:
                        logger.warning("invalid_signal_json", data=line[:100])

            except (FileNotFoundError, ConnectionRefusedError):
                logger.warning("detection_service_not_available")
                await asyncio.sleep(2)
            except Exception as e:
                logger.error("signal_listener_error", error=str(e))
                await asyncio.sleep(2)

    async def _process_signal(self, signal: dict):
        """Process incoming trade signal."""
        self.metrics["signals_received"] += 1

        if signal.get("type") != "TRADE":
            return

        logger.info(
            "processing_signal",
            wallet=signal.get("wallet", "")[:10],
            market=signal.get("market_id", "")[:10],
            side=signal.get("side"),
            size=signal.get("size")
        )

        # Get wallet-specific configuration
        wallet_config = self._get_wallet_config(signal.get("wallet", ""))

        # Calculate position size
        target_size = signal.get("size", 0)
        copy_ratio = wallet_config.get("copy_ratio", 1.0)
        max_copy = wallet_config.get("max_copy_usd", float("inf"))

        sized_amount = self.position_sizer.calculate_size(
            target_size=target_size,
            copy_ratio=copy_ratio,
            price=signal.get("price", 0.5)  # Default to 0.5 if unknown
        )

        # Apply wallet-specific max
        sized_amount = min(sized_amount, max_copy)

        # Risk checks
        risk_result = await self.risk_manager.check_order(
            market_id=signal.get("market_id", ""),
            side=signal.get("side", ""),
            size=sized_amount,
            price=signal.get("price", 0.5),
            position_tracker=self.position_tracker
        )

        if not risk_result["approved"]:
            self.metrics["orders_rejected"] += 1
            logger.info(
                "order_rejected",
                reason=risk_result["reason"],
                market=signal.get("market_id", "")[:10]
            )
            return

        # Apply risk-adjusted size
        final_size = risk_result.get("adjusted_size", sized_amount)

        # Build order request
        order_request = {
            "type": "ORDER_REQUEST",
            "timestamp": signal.get("timestamp"),
            "market_id": signal.get("market_id"),
            "token_id": signal.get("token_id"),
            "side": signal.get("side"),
            "size": final_size,
            "price": signal.get("price"),
            "original_signal": {
                "wallet": signal.get("wallet"),
                "size": signal.get("size"),
                "source": signal.get("source")
            }
        }

        # Send to execution service
        await self._send_to_execution(order_request)

    async def _send_to_execution(self, order_request: dict):
        """Send order request to execution service."""
        import orjson

        if not self._exec_writer:
            logger.error("not_connected_to_execution_service")
            # Try to reconnect
            await self._connect_to_execution()
            if not self._exec_writer:
                return

        try:
            data = orjson.dumps(order_request) + b"\n"
            self._exec_writer.write(data)
            await self._exec_writer.drain()
            self.metrics["orders_sent"] += 1

            logger.info(
                "order_request_sent",
                market=order_request.get("market_id", "")[:10],
                side=order_request.get("side"),
                size=order_request.get("size")
            )
        except Exception as e:
            logger.error("failed_to_send_order", error=str(e))
            self._exec_writer = None

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("shutting_down_decision_engine")
        self.running = False

        if self._exec_writer:
            self._exec_writer.close()
            await self._exec_writer.wait_closed()

        logger.info("decision_engine_stopped")


async def main():
    """Main entry point."""
    load_dotenv()

    engine = DecisionEngine()
    await engine.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("decision_engine_interrupted")
        sys.exit(0)
