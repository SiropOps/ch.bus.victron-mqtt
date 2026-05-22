FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bluetooth \
        bluez \
        gcc \
        python3-dev \
        build-essential \
    && pip install --no-cache-dir victron-ble bleak paho-mqtt \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY victron_mqtt.py /app/victron_mqtt.py

CMD ["python", "victron_mqtt.py"]
