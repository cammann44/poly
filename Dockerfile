FROM python:3.12-slim

WORKDIR /app

# Force cache invalidation - v6
COPY .build-version /tmp/.build-version

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application with unrealised_pnl fix
COPY scripts/track_cigarettes.py .
RUN echo "Build timestamp: $(date)" > /app/.buildinfo
COPY config/ ./config/

# Copy trade history for state restore
COPY logs/cigarettes_trades.json ./logs/

# Expose ports
EXPOSE 9091 9092

# Run tracker
CMD ["python", "track_cigarettes.py"]
