#!/usr/bin/env python3
"""Detection Service Entry Point.

Monitors CLOB WebSocket and on-chain transfers for target wallet activity.
Publishes detected trades via IPC to the Decision service.
"""

import asyncio
import signal
import sys
from pathlib import Path

import structlog
import yaml
from dotenv import load_dotenv

from websocket.clob_client import CLOBWebSocketClient
from onchain.monitor import OnChainMonitor
from ipc.publisher import IPCPublisher

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


class DetectionService:
    """Main detection service orchestrator."""

    def __init__(self, config_path: str = "../config/config.yaml"):
        self.config = self._load_config(config_path)
        self.wallets = self._load_wallets()
        self.running = False

        # Initialize components
        self.ipc_publisher = IPCPublisher(
            socket_path=self.config["ipc"]["detection_socket"]
        )
        self.clob_client = CLOBWebSocketClient(
            wss_url=self.config["clob"]["wss_url"],
            target_wallets=self._get_enabled_wallets(),
            publisher=self.ipc_publisher,
            config=self.config["detection"]
        )
        self.onchain_monitor = OnChainMonitor(
            rpc_url=self.config["network"]["polygon_rpc"],
            wss_url=self.config["network"]["polygon_wss"],
            target_wallets=self._get_enabled_wallets(),
            publisher=self.ipc_publisher,
            contracts=self.config["contracts"]
        )

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from YAML file."""
        config_file = Path(__file__).parent.parent.parent / "config" / "config.yaml"
        if not config_file.exists():
            config_file = Path(config_path)

        with open(config_file) as f:
            return yaml.safe_load(f)

    def _load_wallets(self) -> dict:
        """Load target wallets from JSON file."""
        wallets_file = Path(__file__).parent.parent.parent / "config" / "wallets.json"
        import json
        with open(wallets_file) as f:
            return json.load(f)

    def _get_enabled_wallets(self) -> list[str]:
        """Get list of enabled wallet addresses."""
        return [
            w["address"].lower()
            for w in self.wallets["wallets"]
            if w.get("enabled", True)
        ]

    async def start(self):
        """Start all detection components."""
        logger.info("starting_detection_service",
                    target_wallets=len(self._get_enabled_wallets()))

        self.running = True

        # Start IPC publisher
        await self.ipc_publisher.start()

        # Start detection tasks
        tasks = [
            asyncio.create_task(self.clob_client.run(), name="clob_websocket"),
            asyncio.create_task(self.onchain_monitor.run(), name="onchain_monitor"),
        ]

        # Handle shutdown signals
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("detection_service_cancelled")

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("shutting_down_detection_service")
        self.running = False

        await self.clob_client.stop()
        await self.onchain_monitor.stop()
        await self.ipc_publisher.stop()

        # Cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()

        logger.info("detection_service_stopped")


async def main():
    """Main entry point."""
    load_dotenv()

    service = DetectionService()
    await service.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("detection_service_interrupted")
        sys.exit(0)
