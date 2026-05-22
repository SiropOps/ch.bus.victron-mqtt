import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI
from fastapi.responses import JSONResponse


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("victron-metrics-api")


def env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


MQTT_HOST = env("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USERNAME = env("MQTT_USERNAME", "victron")
MQTT_PASSWORD = env("MQTT_PASSWORD", "change-me")
MQTT_TOPIC = env("MQTT_TOPIC", "van/victron-mppt/smartsolar_pyleas")

METRIC_FIELDS = (
    "timestamp",
    "battery_charging_current",
    "battery_voltage",
    "charge_state",
    "solar_power",
    "yield_today",
)

state_lock = threading.Lock()
latest_metrics: dict[str, Any] | None = None
mqtt_connected = False
mqtt_client: mqtt.Client | None = None


def clean_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    return {field: payload.get(field) for field in METRIC_FIELDS}


def mqtt_reason_is_success(reason_code: Any) -> bool:
    if hasattr(reason_code, "is_failure"):
        return not reason_code.is_failure
    return reason_code == 0


def on_connect(
    client: mqtt.Client,
    userdata: Any,
    flags: Any,
    reason_code: Any,
    properties: Any,
) -> None:
    global mqtt_connected

    if mqtt_reason_is_success(reason_code):
        with state_lock:
            mqtt_connected = True
        client.subscribe(MQTT_TOPIC)
        logger.info("MQTT connected to %s:%s; subscribed to %s", MQTT_HOST, MQTT_PORT, MQTT_TOPIC)
    else:
        with state_lock:
            mqtt_connected = False
        logger.error("MQTT connection failed: %s", reason_code)


def on_disconnect(
    client: mqtt.Client,
    userdata: Any,
    disconnect_flags: Any,
    reason_code: Any,
    properties: Any,
) -> None:
    global mqtt_connected

    with state_lock:
        mqtt_connected = False
    logger.warning("MQTT disconnected: %s", reason_code)


def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
    global latest_metrics

    try:
        decoded = message.payload.decode("utf-8")
        payload = json.loads(decoded)
    except UnicodeDecodeError as exc:
        logger.warning("Ignoring non-UTF-8 MQTT payload on %s: %s", message.topic, exc)
        return
    except json.JSONDecodeError as exc:
        logger.warning("Ignoring invalid JSON payload on %s: %s", message.topic, exc)
        return

    if not isinstance(payload, dict):
        logger.warning("Ignoring MQTT payload on %s because JSON root is not an object", message.topic)
        return

    with state_lock:
        latest_metrics = clean_metrics(payload)


def create_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="victron-metrics-api")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    return client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client

    logger.info("Starting Victron Metrics API")
    logger.info("MQTT: %s:%s", MQTT_HOST, MQTT_PORT)
    logger.info("Topic: %s", MQTT_TOPIC)

    mqtt_client = create_mqtt_client()
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    try:
        yield
    finally:
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


app = FastAPI(title="Victron Metrics API", lifespan=lifespan)


@app.get("/api/metrics")
def get_metrics() -> JSONResponse:
    with state_lock:
        metrics = latest_metrics.copy() if latest_metrics is not None else None

    if metrics is None:
        return JSONResponse({"status": "waiting_for_mqtt_data"}, status_code=503)

    return JSONResponse(metrics)


@app.get("/api/health")
def get_health() -> dict[str, Any]:
    with state_lock:
        connected = mqtt_connected
        last_message_timestamp = latest_metrics.get("timestamp") if latest_metrics else None

    return {
        "status": "ok",
        "mqtt_connected": connected,
        "last_message_timestamp": last_message_timestamp,
    }
