FROM python:3.12-slim

# System deps (no build tools needed — all wheels available for these packages)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache — only invalidated when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot/        ./bot/
COPY dashboard/  ./dashboard/
COPY main.py     .

# Copy Streamlit theme config
COPY .streamlit/ ./.streamlit/

# Runtime data directory (mounted as volume in production)
RUN mkdir -p /app/data /app/logs

ENV DB_PATH=/app/data/trading_bot.db \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default: run the bot. Override in docker-compose for the dashboard service.
CMD ["python", "main.py"]
