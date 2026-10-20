FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.yaml .
COPY logai2/ logai2/
COPY scripts/ scripts/

CMD ["python", "scripts/run_engine.py"]
