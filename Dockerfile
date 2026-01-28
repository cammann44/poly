# PYTHON TRACKER v8 - REAL TRADING SUPPORT
FROM python:3.12-slim

# Unique build marker to invalidate cache
ARG BUILD_DATE=unknown
RUN echo "Build: ${BUILD_DATE}" > /build-marker

WORKDIR /app

# Install Python dependencies first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy wallet config
COPY config/ ./config/

# Copy main tracker script
COPY scripts/track_multi_wallets.py .

# Create logs directory
RUN mkdir -p logs

# Health and metrics ports
EXPOSE 9091 9092

# Start Python tracker
ENTRYPOINT ["python"]
CMD ["track_multi_wallets.py"]
