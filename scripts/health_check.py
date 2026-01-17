#!/usr/bin/env python3
"""Health Check Script.

Validates all system connections and configurations.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
import yaml

# Colour codes for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def success(msg: str):
    print(f"{GREEN}✓{RESET} {msg}")


def failure(msg: str):
    print(f"{RED}✗{RESET} {msg}")


def warning(msg: str):
    print(f"{YELLOW}!{RESET} {msg}")


class HealthChecker:
    """System health checker."""

    def __init__(self, config_path: str = "../config/config.yaml"):
        self.config = self._load_config(config_path)
        self.results = []

    def _load_config(self, config_path: str) -> dict:
        config_file = Path(__file__).parent.parent / "config" / "config.yaml"
        if not config_file.exists():
            config_file = Path(config_path)
        with open(config_file) as f:
            return yaml.safe_load(f)

    async def run_all_checks(self) -> bool:
        """Run all health checks."""
        print("\n" + "=" * 50)
        print("POLYMARKET COPY-TRADING BOT HEALTH CHECK")
        print("=" * 50 + "\n")

        checks = [
            ("Configuration Files", self.check_config_files),
            ("Environment Variables", self.check_env_vars),
            ("CLOB REST API", self.check_clob_rest),
            ("CLOB WebSocket", self.check_clob_websocket),
            ("Polygon RPC", self.check_polygon_rpc),
            ("IPC Sockets", self.check_ipc_sockets),
            ("Target Wallets", self.check_wallets),
        ]

        all_passed = True

        for name, check_fn in checks:
            print(f"\n[{name}]")
            try:
                passed = await check_fn()
                if not passed:
                    all_passed = False
            except Exception as e:
                failure(f"Check failed with error: {e}")
                all_passed = False

        print("\n" + "=" * 50)
        if all_passed:
            success("All health checks passed!")
        else:
            failure("Some health checks failed. Review the output above.")
        print("=" * 50 + "\n")

        return all_passed

    async def check_config_files(self) -> bool:
        """Check configuration files exist and are valid."""
        passed = True

        # Check config.yaml
        config_file = Path(__file__).parent.parent / "config" / "config.yaml"
        if config_file.exists():
            success(f"config.yaml found at {config_file}")
        else:
            failure(f"config.yaml not found at {config_file}")
            passed = False

        # Check wallets.json
        wallets_file = Path(__file__).parent.parent / "config" / "wallets.json"
        if wallets_file.exists():
            success(f"wallets.json found at {wallets_file}")
            with open(wallets_file) as f:
                wallets = json.load(f)
                wallet_count = len(wallets.get("wallets", []))
                enabled_count = sum(1 for w in wallets.get("wallets", []) if w.get("enabled", True))
                success(f"Found {wallet_count} wallets ({enabled_count} enabled)")
        else:
            failure(f"wallets.json not found at {wallets_file}")
            passed = False

        # Check .env
        env_file = Path(__file__).parent.parent / "config" / ".env"
        if env_file.exists():
            success(f".env found at {env_file}")
        else:
            warning(f".env not found at {env_file} (using .env.example or environment)")

        return passed

    async def check_env_vars(self) -> bool:
        """Check required environment variables."""
        passed = True

        required_vars = [
            "PRIVATE_KEY",
            "WALLET_ADDRESS",
        ]

        optional_vars = [
            "POLY_API_KEY",
            "POLY_API_SECRET",
            "POLY_API_PASSPHRASE",
            "POLYGON_RPC_URL",
        ]

        for var in required_vars:
            value = os.environ.get(var)
            if value and len(value) > 10:
                success(f"{var} is set")
            elif value:
                warning(f"{var} is set but looks short (might be placeholder)")
            else:
                failure(f"{var} is not set")
                passed = False

        for var in optional_vars:
            value = os.environ.get(var)
            if value:
                success(f"{var} is set")
            else:
                warning(f"{var} is not set (optional)")

        return passed

    async def check_clob_rest(self) -> bool:
        """Check CLOB REST API connectivity."""
        url = self.config["clob"]["rest_url"]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        success(f"CLOB REST API reachable at {url}")
                        return True
                    else:
                        warning(f"CLOB REST API returned status {response.status}")
                        return True  # Still reachable
        except Exception as e:
            failure(f"CLOB REST API unreachable: {e}")
            return False

    async def check_clob_websocket(self) -> bool:
        """Check CLOB WebSocket connectivity."""
        import websockets

        url = self.config["clob"]["wss_url"]

        try:
            async with websockets.connect(url, close_timeout=5) as ws:
                success(f"CLOB WebSocket connected at {url}")
                await ws.close()
                return True
        except Exception as e:
            failure(f"CLOB WebSocket unreachable: {e}")
            return False

    async def check_polygon_rpc(self) -> bool:
        """Check Polygon RPC connectivity."""
        url = os.environ.get("POLYGON_RPC_URL", self.config["network"]["polygon_rpc"])

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_chainId",
            "params": [],
            "id": 1
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        chain_id = int(data.get("result", "0x0"), 16)
                        if chain_id == 137:
                            success(f"Polygon RPC connected (chain ID: {chain_id})")
                            return True
                        else:
                            warning(f"RPC connected but unexpected chain ID: {chain_id}")
                            return True
                    else:
                        failure(f"Polygon RPC returned status {response.status}")
                        return False
        except Exception as e:
            failure(f"Polygon RPC unreachable: {e}")
            return False

    async def check_ipc_sockets(self) -> bool:
        """Check IPC socket paths are writable."""
        passed = True

        sockets = [
            ("detection_socket", self.config["ipc"]["detection_socket"]),
            ("decision_socket", self.config["ipc"]["decision_socket"]),
            ("execution_socket", self.config["ipc"]["execution_socket"]),
        ]

        for name, path in sockets:
            socket_dir = Path(path).parent
            if socket_dir.exists():
                if os.access(socket_dir, os.W_OK):
                    success(f"{name} directory writable: {socket_dir}")
                else:
                    failure(f"{name} directory not writable: {socket_dir}")
                    passed = False
            else:
                warning(f"{name} directory doesn't exist (will be created): {socket_dir}")

            # Check if socket already exists (service running)
            if Path(path).exists():
                success(f"{name} socket exists (service may be running)")

        return passed

    async def check_wallets(self) -> bool:
        """Validate target wallet addresses."""
        wallets_file = Path(__file__).parent.parent / "config" / "wallets.json"

        try:
            with open(wallets_file) as f:
                data = json.load(f)

            wallets = data.get("wallets", [])
            valid_count = 0

            for wallet in wallets:
                addr = wallet.get("address", "")
                if addr.startswith("0x") and len(addr) == 42:
                    valid_count += 1
                else:
                    warning(f"Invalid wallet address format: {addr[:20]}...")

            if valid_count > 0:
                success(f"{valid_count} valid wallet addresses configured")
                return True
            else:
                failure("No valid wallet addresses found")
                return False

        except Exception as e:
            failure(f"Failed to load wallets: {e}")
            return False


async def main():
    """Run health checks."""
    from dotenv import load_dotenv

    # Load .env file
    env_file = Path(__file__).parent.parent / "config" / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    checker = HealthChecker()
    passed = await checker.run_all_checks()

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
