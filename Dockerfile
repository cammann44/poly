FROM python:3.12-slim

WORKDIR /app

# Force cache invalidation - v7 real trading
COPY .build-version /tmp/.build-version 2>/dev/null || true

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY scripts/track_multi_wallets.py .
RUN echo "Build timestamp: $(date)" > /app/.buildinfo
COPY config/ ./config/

# Copy trade history for state restore (if exists)
COPY logs/poly_trades.json ./logs/ 2>/dev/null || true

# Expose ports (metrics + health)
EXPOSE 9091 9092

# Run tracker with real trading support
CMD ["python", "track_multi_wallets.py"]
