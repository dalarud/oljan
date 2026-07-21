FROM python:3.11-slim

# System deps for matplotlib fonts etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    TZ=UTC \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data (sqlite, charts, logs) persists via a mounted volume.
VOLUME ["/app/data"]

# config.yaml and .env are expected to be mounted at runtime.
CMD ["python", "-m", "oiltrader", "--config", "config.yaml"]
