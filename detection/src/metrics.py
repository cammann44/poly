"""Prometheus Metrics for Detection Service."""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import structlog

logger = structlog.get_logger(__name__)

# Counters
TRADES_DETECTED = Counter(
    'poly_trades_detected_total',
    'Total number of trades detected from target wallets',
    ['source', 'wallet']
)

WEBSOCKET_MESSAGES = Counter(
    'poly_websocket_messages_total',
    'Total WebSocket messages received'
)

WEBSOCKET_RECONNECTIONS = Counter(
    'poly_websocket_reconnections_total',
    'Number of WebSocket reconnections'
)

ONCHAIN_TRANSFERS = Counter(
    'poly_onchain_transfers_total',
    'Total on-chain transfers detected'
)

IPC_MESSAGES_SENT = Counter(
    'poly_ipc_messages_sent_total',
    'Total IPC messages sent to decision service'
)

# Gauges
WEBSOCKET_CONNECTED = Gauge(
    'poly_websocket_connected',
    'WebSocket connection status (1=connected, 0=disconnected)'
)

ONCHAIN_BLOCKS_PROCESSED = Gauge(
    'poly_onchain_blocks_processed',
    'Latest block number processed'
)

IPC_CLIENTS_CONNECTED = Gauge(
    'poly_ipc_clients_connected',
    'Number of IPC clients connected'
)

# Histograms
DETECTION_LATENCY = Histogram(
    'poly_detection_latency_ms',
    'Time from event to detection in milliseconds',
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000]
)


def start_metrics_server(port: int = 9091):
    """Start Prometheus metrics HTTP server."""
    start_http_server(port)
    logger.info("metrics_server_started", port=port)
