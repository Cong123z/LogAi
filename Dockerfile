FROM python:3.11-slim

WORKDIR /app

# build deps for hdbscan (needs a C compiler)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data/models

EXPOSE 9108

# Default: run the realtime pipeline. Training is a one-off job, run via:
#   docker compose run --rm logai-engine python scripts/run_training.py
CMD ["python", "scripts/run_realtime.py"]
