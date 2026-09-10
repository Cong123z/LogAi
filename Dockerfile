FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# Single layer: install build tools -> compile packages -> remove build tools & all caches.
# Because it's all one RUN, the removed files never persist in any layer, keeping the
# final image size close to what it'd be without build-essential at all.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir --upgrade pip \
    # CPU-only torch -- avoids pulling multi-GB CUDA/cuDNN packages
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    # strip test/cache bloat from installed packages
    && find /usr/local/lib/python3.11 -type d -name "tests" -exec rm -rf {} + \
    && find /usr/local/lib/python3.11 -type d -name "__pycache__" -exec rm -rf {} + \
    && find /usr/local/lib/python3.11 -name "*.pyc" -delete \
    # remove build tools now that native extensions (e.g. hdbscan) are compiled
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY . .

RUN mkdir -p /app/data/models

EXPOSE 9108

# Default: run the realtime pipeline. Training is a one-off job, run via:
#   docker compose run --rm logai-engine python scripts/run_training.py
CMD ["python", "scripts/run_realtime.py"]
