FROM python:3.12-slim

# Cache bust arg
ARG CACHEBUST=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application - v5 with unrealised_pnl
COPY scripts/track_cigarettes.py .
RUN echo "Build timestamp: $(date)" > /app/.buildinfo
COPY config/ ./config/

# Copy trade history for state restore
COPY logs/cigarettes_trades.json ./logs/

# Expose ports
EXPOSE 9091 9092

# Run tracker
CMD ["python", "track_cigarettes.py"]
