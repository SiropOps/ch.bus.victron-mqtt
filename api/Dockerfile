FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV API_PORT=8013

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        python3-dev \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apt-get purge -y --auto-remove build-essential python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY app.py /app/app.py

EXPOSE 8013

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${API_PORT:-8013}"]
