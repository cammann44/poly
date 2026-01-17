#!/bin/bash
# Polymarket Copy-Trading Bot Startup Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=========================================="
echo "  Polymarket Copy-Trading Bot"
echo "=========================================="
echo ""

# Check if .env exists
if [ ! -f "config/.env" ]; then
    echo "[!] config/.env not found. Copying from .env.example..."
    cp config/.env.example config/.env
    echo "[!] Please edit config/.env with your credentials"
    echo ""
fi

# Parse arguments
MODE=${1:-"paper"}  # paper, live, or monitor

case $MODE in
    "paper")
        echo "[*] Starting in PAPER TRADING mode..."
        echo ""

        # Start monitoring stack
        docker-compose up -d prometheus grafana
        echo "[+] Monitoring started: http://localhost:3001"

        # Run paper trading
        cd scripts
        python3 paper_trade.py
        ;;

    "live")
        echo "[*] Starting in LIVE mode..."
        echo "[!] WARNING: Real trades will be executed!"
        echo ""
        read -p "Are you sure? (yes/no): " confirm

        if [ "$confirm" != "yes" ]; then
            echo "Aborted."
            exit 0
        fi

        # Start all services
        docker-compose up -d
        echo ""
        echo "[+] All services started"
        echo "[+] Grafana: http://localhost:3001"
        echo "[+] Prometheus: http://localhost:9090"
        echo ""
        echo "Logs: docker-compose logs -f"
        ;;

    "monitor")
        echo "[*] Starting monitoring only..."
        docker-compose up -d prometheus grafana
        echo ""
        echo "[+] Grafana: http://localhost:3001"
        echo "[+] Prometheus: http://localhost:9090"
        ;;

    "stop")
        echo "[*] Stopping all services..."
        docker-compose down
        echo "[+] Stopped"
        ;;

    "status")
        echo "[*] Service status:"
        docker-compose ps
        ;;

    "health")
        echo "[*] Running health check..."
        cd scripts
        python3 health_check.py
        ;;

    *)
        echo "Usage: $0 [paper|live|monitor|stop|status|health]"
        echo ""
        echo "  paper   - Start paper trading mode (default)"
        echo "  live    - Start live trading (requires confirmation)"
        echo "  monitor - Start Grafana + Prometheus only"
        echo "  stop    - Stop all services"
        echo "  status  - Show service status"
        echo "  health  - Run health checks"
        ;;
esac
