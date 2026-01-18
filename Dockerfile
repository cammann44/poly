FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application - force fresh copy
ARG CACHEBUST=1
COPY scripts/track_cigarettes.py .
COPY config/ ./config/

# Create logs directory
RUN mkdir -p logs

# Expose ports
EXPOSE 9091 9092

# Run tracker
CMD ["python", "track_cigarettes.py"]
