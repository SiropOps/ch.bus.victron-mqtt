import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI, Query
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
MQTT_BASE_TOPIC = env("MQTT_BASE_TOPIC", "van/victron").rstrip("/")
MQTT_DEVICES = tuple(
    name.strip() for name in env("MQTT_DEVICES", "").split(",") if name.strip()
)
MQTT_TOPIC = f"{MQTT_BASE_TOPIC}/+"
DEVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

state_lock = threading.Lock()
latest_metrics: dict[str, dict[str, Any]] = {}
mqtt_connected = False
mqtt_client: mqtt.Client | None = None


def configured_topics() -> tuple[str, ...]:
    """Return exact device topics, or a wildcard used to discover every device."""
    if MQTT_DEVICES:
        invalid = [name for name in MQTT_DEVICES if not DEVICE_NAME_PATTERN.fullmatch(name)]
        if invalid:
            raise ValueError(f"Invalid MQTT device name(s): {', '.join(invalid)}")
        return tuple(f"{MQTT_BASE_TOPIC}/{name}" for name in MQTT_DEVICES)
    return (MQTT_TOPIC,)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def should_replace(previous: dict[str, Any] | None, payload: dict[str, Any]) -> bool:
    """Do not let an older retained/alias message replace a newer reading."""
    if previous is None:
        return True
    previous_timestamp = parse_timestamp(previous.get("timestamp"))
    payload_timestamp = parse_timestamp(payload.get("timestamp"))
    if previous_timestamp is None or payload_timestamp is None:
        return True
    try:
        return payload_timestamp >= previous_timestamp
    except TypeError:
        logger.warning("Replacing reading with an incompatible timestamp timezone")
        return True


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
        topics = configured_topics()
        for topic in topics:
            client.subscribe(topic)
        logger.info(
            "MQTT connected to %s:%s; subscribed to %s",
            MQTT_HOST,
            MQTT_PORT,
            ", ".join(topics),
        )
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

    name = payload.get("name")
    if not isinstance(name, str) or not DEVICE_NAME_PATTERN.fullmatch(name):
        logger.warning("Ignoring MQTT payload on %s because it has no valid device name", message.topic)
        return

    # When exact names are configured, an alias payload must not populate an
    # unexpected device. The topic check also makes tests and broker ACLs safer.
    if MQTT_DEVICES and name not in MQTT_DEVICES:
        logger.warning("Ignoring unconfigured Victron device %s on %s", name, message.topic)
        return

    with state_lock:
        if should_replace(latest_metrics.get(name), payload):
            latest_metrics[name] = payload.copy()


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
    logger.info("Topics: %s", ", ".join(configured_topics()))

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
def get_metrics(
    names: str | None = Query(
        default=None,
        description="Comma-separated Victron device names",
    ),
    name: str | None = Query(
        default=None,
        description="Backward-compatible alias for names",
    ),
) -> JSONResponse:
    requested_value = names if names is not None else name
    requested = (
        tuple(dict.fromkeys(item.strip() for item in requested_value.split(",") if item.strip()))
        if requested_value is not None
        else MQTT_DEVICES
    )

    invalid = [item for item in requested if not DEVICE_NAME_PATTERN.fullmatch(item)]
    if invalid:
        return JSONResponse(
            {"status": "invalid_device_names", "names": invalid}, status_code=422
        )

    with state_lock:
        selected_names = requested or tuple(latest_metrics)
        metrics = {
            device_name: latest_metrics[device_name].copy()
            for device_name in selected_names
            if device_name in latest_metrics
        }

    missing = [device_name for device_name in selected_names if device_name not in metrics]
    if not metrics or missing:
        return JSONResponse(
            {"status": "waiting_for_mqtt_data", "missing": missing}, status_code=503
        )

    return JSONResponse(metrics)


@app.get("/api/health")
def get_health() -> dict[str, Any]:
    with state_lock:
        connected = mqtt_connected
        last_message_timestamps = {
            name: metrics.get("timestamp") for name, metrics in latest_metrics.items()
        }

    return {
        "status": "ok",
        "mqtt_connected": connected,
        "subscribed_topics": list(configured_topics()),
        "last_message_timestamps": last_message_timestamps,
    }
