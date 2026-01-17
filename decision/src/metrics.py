"""Prometheus Metrics for Decision Service."""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import structlog

logger = structlog.get_logger(__name__)

# Counters
SIGNALS_RECEIVED = Counter(
    'poly_signals_received_total',
    'Total trade signals received from detection'
)

ORDERS_SENT = Counter(
    'poly_orders_sent_total',
    'Total order requests sent to execution'
)

ORDERS_REJECTED = Counter(
    'poly_orders_rejected_total',
    'Orders rejected by risk filters',
    ['reason']
)

# Gauges
OPEN_POSITIONS = Gauge(
    'poly_open_positions',
    'Number of open positions'
)

TOTAL_EXPOSURE = Gauge(
    'poly_total_exposure_usd',
    'Total USD exposure across all positions'
)

DAILY_VOLUME = Gauge(
    'poly_daily_volume_usd',
    'Daily trading volume in USD'
)

DAILY_VOLUME_REMAINING = Gauge(
    'poly_daily_volume_remaining_usd',
    'Remaining daily volume capacity'
)

REALISED_PNL = Gauge(
    'poly_realised_pnl_usd',
    'Total realised profit/loss in USD'
)

BALANCE = Gauge(
    'poly_balance_usd',
    'Current cash balance in USD'
)

PORTFOLIO_VALUE = Gauge(
    'poly_portfolio_value_usd',
    'Total portfolio value including positions'
)

# Histograms
POSITION_SIZE = Histogram(
    'poly_position_size_usd',
    'Distribution of position sizes',
    buckets=[5, 10, 25, 50, 100, 200, 500]
)

SIGNAL_PROCESSING_TIME = Histogram(
    'poly_signal_processing_ms',
    'Time to process signals in milliseconds',
    buckets=[1, 5, 10, 25, 50, 100]
)


def start_metrics_server(port: int = 9092):
    """Start Prometheus metrics HTTP server."""
    start_http_server(port)
    logger.info("metrics_server_started", port=port)
