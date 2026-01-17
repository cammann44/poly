"""On-chain Monitor for ERC1155 Transfer Events.

Monitors Polygon blockchain for Conditional Token transfers from target wallets.
Acts as a secondary detection mechanism complementing CLOB WebSocket.
"""

import asyncio
import time
from typing import Optional

import structlog
from web3 import AsyncWeb3, AsyncHTTPProvider
from web3.contract import AsyncContract
from web3.types import LogReceipt

logger = structlog.get_logger(__name__)

# ERC1155 Transfer Event Signatures
TRANSFER_SINGLE_TOPIC = AsyncWeb3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()
TRANSFER_BATCH_TOPIC = AsyncWeb3.keccak(
    text="TransferBatch(address,address,address,uint256[],uint256[])"
).hex()

# Minimal ERC1155 ABI for event decoding
ERC1155_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "operator", "type": "address"},
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "id", "type": "uint256"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "TransferSingle",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "operator", "type": "address"},
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "ids", "type": "uint256[]"},
            {"indexed": False, "name": "values", "type": "uint256[]"},
        ],
        "name": "TransferBatch",
        "type": "event",
    },
]


class OnChainMonitor:
    """Monitor on-chain ERC1155 transfers for target wallets."""

    def __init__(
        self,
        rpc_url: str,
        wss_url: str,
        target_wallets: list[str],
        publisher,
        contracts: dict
    ):
        self.rpc_url = rpc_url
        self.wss_url = wss_url
        self.target_wallets = set(w.lower() for w in target_wallets)
        self.publisher = publisher

        self.conditional_tokens_address = contracts["conditional_tokens"]
        self.ctf_exchange_address = contracts["ctf_exchange"]

        self.w3: Optional[AsyncWeb3] = None
        self.contract: Optional[AsyncContract] = None
        self.running = False

        # Track processed transactions to avoid duplicates
        self._processed_txs: dict[str, float] = {}

        # Metrics
        self.metrics = {
            "blocks_processed": 0,
            "transfers_detected": 0,
            "errors": 0,
        }

    async def run(self):
        """Main monitoring loop."""
        self.running = True

        # Initialize Web3 connection
        self.w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))

        # Verify connection
        if not await self.w3.is_connected():
            logger.error("failed_to_connect_to_polygon_rpc")
            return

        chain_id = await self.w3.eth.chain_id
        logger.info("connected_to_polygon", chain_id=chain_id)

        # Create contract instance
        self.contract = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.conditional_tokens_address),
            abi=ERC1155_ABI
        )

        # Start block polling (fallback for when WebSocket not available)
        await self._poll_blocks()

    async def _poll_blocks(self):
        """Poll for new blocks and check for relevant transfers."""
        last_block = await self.w3.eth.block_number
        logger.info("starting_block_polling", from_block=last_block)

        while self.running:
            try:
                current_block = await self.w3.eth.block_number

                if current_block > last_block:
                    # Process new blocks
                    for block_num in range(last_block + 1, current_block + 1):
                        await self._process_block(block_num)
                        self.metrics["blocks_processed"] += 1

                    last_block = current_block

                # Poll interval (~1 second, faster than block time)
                await asyncio.sleep(1)

            except Exception as e:
                logger.error("block_polling_error", error=str(e))
                self.metrics["errors"] += 1
                await asyncio.sleep(5)  # Back off on error

    async def _process_block(self, block_number: int):
        """Process a single block for relevant transfers."""
        try:
            # Get transfer events from Conditional Tokens contract
            logs = await self.w3.eth.get_logs({
                "fromBlock": block_number,
                "toBlock": block_number,
                "address": AsyncWeb3.to_checksum_address(self.conditional_tokens_address),
                "topics": [[TRANSFER_SINGLE_TOPIC, TRANSFER_BATCH_TOPIC]]
            })

            for log in logs:
                await self._process_transfer_log(log)

        except Exception as e:
            logger.error("block_processing_error", block=block_number, error=str(e))

    async def _process_transfer_log(self, log: LogReceipt):
        """Process a transfer event log."""
        tx_hash = log["transactionHash"].hex()

        # Deduplication
        now = time.time()
        if tx_hash in self._processed_txs:
            return
        self._processed_txs[tx_hash] = now

        # Clean old entries
        self._cleanup_processed_txs(now)

        try:
            # Decode event
            topic = log["topics"][0].hex()

            if topic == TRANSFER_SINGLE_TOPIC:
                await self._handle_transfer_single(log, tx_hash)
            elif topic == TRANSFER_BATCH_TOPIC:
                await self._handle_transfer_batch(log, tx_hash)

        except Exception as e:
            logger.error("transfer_log_processing_error", tx=tx_hash[:10], error=str(e))

    async def _handle_transfer_single(self, log: LogReceipt, tx_hash: str):
        """Handle TransferSingle event."""
        # Decode indexed parameters from topics
        # topics[1] = operator, topics[2] = from, topics[3] = to
        from_addr = self._decode_address(log["topics"][2])
        to_addr = self._decode_address(log["topics"][3])

        # Decode non-indexed parameters from data
        token_id, value = self._decode_transfer_single_data(log["data"])

        await self._check_and_publish_transfer(
            tx_hash=tx_hash,
            from_addr=from_addr,
            to_addr=to_addr,
            token_id=token_id,
            value=value,
            block_number=log["blockNumber"]
        )

    async def _handle_transfer_batch(self, log: LogReceipt, tx_hash: str):
        """Handle TransferBatch event."""
        from_addr = self._decode_address(log["topics"][2])
        to_addr = self._decode_address(log["topics"][3])

        # Decode arrays from data
        token_ids, values = self._decode_transfer_batch_data(log["data"])

        for token_id, value in zip(token_ids, values):
            await self._check_and_publish_transfer(
                tx_hash=tx_hash,
                from_addr=from_addr,
                to_addr=to_addr,
                token_id=token_id,
                value=value,
                block_number=log["blockNumber"]
            )

    async def _check_and_publish_transfer(
        self,
        tx_hash: str,
        from_addr: str,
        to_addr: str,
        token_id: int,
        value: int,
        block_number: int
    ):
        """Check if transfer involves target wallet and publish."""
        from_lower = from_addr.lower()
        to_lower = to_addr.lower()

        target_wallet = None
        side = None

        # Check if FROM is a target wallet (selling)
        if from_lower in self.target_wallets:
            target_wallet = from_lower
            side = "SELL"
        # Check if TO is a target wallet (buying)
        elif to_lower in self.target_wallets:
            target_wallet = to_lower
            side = "BUY"

        if not target_wallet:
            return

        self.metrics["transfers_detected"] += 1

        logger.info(
            "onchain_transfer_detected",
            wallet=target_wallet[:10],
            side=side,
            token_id=str(token_id)[:20],
            value=value,
            block=block_number
        )

        # Create and publish trade signal
        from websocket.clob_client import DetectedTrade

        detected = DetectedTrade(
            timestamp=time.time() * 1000,
            wallet=target_wallet,
            market_id="",  # Will be resolved from token_id
            token_id=str(token_id),
            side=side,
            size=value / 1e6,  # Assuming USDC decimals
            price=0,  # Price unknown from on-chain data
            order_id=tx_hash,
            source="ONCHAIN"
        )

        await self.publisher.publish_trade(detected)

    def _decode_address(self, topic: bytes) -> str:
        """Decode address from 32-byte topic."""
        return "0x" + topic.hex()[-40:]

    def _decode_transfer_single_data(self, data: bytes) -> tuple[int, int]:
        """Decode TransferSingle data (id, value)."""
        # data is 64 bytes: 32 for id, 32 for value
        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        token_id = int.from_bytes(data[:32], "big")
        value = int.from_bytes(data[32:64], "big")
        return token_id, value

    def _decode_transfer_batch_data(self, data: bytes) -> tuple[list[int], list[int]]:
        """Decode TransferBatch data (ids[], values[])."""
        if isinstance(data, str):
            data = bytes.fromhex(data[2:] if data.startswith("0x") else data)

        # ABI-encoded dynamic arrays
        # Offset to ids array (32 bytes)
        # Offset to values array (32 bytes)
        # Then array data

        ids_offset = int.from_bytes(data[:32], "big")
        values_offset = int.from_bytes(data[32:64], "big")

        def decode_array(offset: int) -> list[int]:
            length = int.from_bytes(data[offset:offset + 32], "big")
            items = []
            for i in range(length):
                start = offset + 32 + (i * 32)
                items.append(int.from_bytes(data[start:start + 32], "big"))
            return items

        token_ids = decode_array(ids_offset)
        values = decode_array(values_offset)

        return token_ids, values

    def _cleanup_processed_txs(self, now: float):
        """Remove old transaction hashes from cache."""
        cutoff = now - 300  # 5 minutes
        keys_to_remove = [k for k, v in self._processed_txs.items() if v < cutoff]
        for k in keys_to_remove:
            del self._processed_txs[k]

    async def stop(self):
        """Stop the on-chain monitor."""
        logger.info("stopping_onchain_monitor")
        self.running = False
