import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    return value.strip() if value else default


VICTRON_DEVICES = [d.strip() for d in env("VICTRON_DEVICES", "").split(",") if d.strip()]
MQTT_HOST = env("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(env("MQTT_PORT", "1883"))
MQTT_USERNAME = env("MQTT_USERNAME", "")
MQTT_PASSWORD = env("MQTT_PASSWORD", "")
MQTT_BASE_TOPIC = env("MQTT_BASE_TOPIC", "van/victron").rstrip("/")
MQTT_STATUS_TOPIC = f"{MQTT_BASE_TOPIC}/status"
READ_INTERVAL_SECONDS = int(env("READ_INTERVAL_SECONDS", "30"))
VICTRON_NUM_READINGS = int(env("VICTRON_NUM_READINGS", "1"))


def topic_safe(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9_-]+", "_", value)
    return value.strip("_") or "device"


def publish_device(client: mqtt.Client, data: dict) -> None:
    payload = data.get("payload", {}) or {}
    name = data.get("name", "device")
    address = data.get("address", "unknown")
    model_name = payload.get("model_name", name)

    enriched = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "address": address,
        "rssi": data.get("rssi"),
        "model_name": model_name,
        **payload,
    }

    device_topic = f"{MQTT_BASE_TOPIC}/{topic_safe(name)}"
    model_topic = f"{MQTT_BASE_TOPIC}/{topic_safe(model_name)}"

    message = json.dumps(enriched, ensure_ascii=False)
    client.publish(device_topic, message, retain=True)
    client.publish(model_topic, message, retain=True)

    # Optional individual values for simple dashboards / Node-RED flows.
    for key, value in enriched.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            client.publish(f"{device_topic}/{key}", json.dumps(value), retain=True)

    print(message, flush=True)


def read_victron_once(client: mqtt.Client) -> None:
    if not VICTRON_DEVICES:
        raise RuntimeError("VICTRON_DEVICES is empty. Example: E1:EA:0C:89:CC:C5@your_key")

    cmd = ["victron-ble", "read", *VICTRON_DEVICES, "--num-readings", str(VICTRON_NUM_READINGS)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)

    if result.stderr.strip():
        print(result.stderr.strip(), flush=True)

    if result.returncode != 0:
        raise RuntimeError(f"victron-ble failed with code {result.returncode}: {result.stderr}")

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            publish_device(client, json.loads(line))
        except json.JSONDecodeError:
            print(f"Invalid JSON ignored: {line}", flush=True)


def on_connect(client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
    if reason_code == 0:
        client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
        print(f"MQTT connected; published {MQTT_STATUS_TOPIC}=online", flush=True)
    else:
        print(f"MQTT connection failed: {reason_code}", flush=True)


def connect_mqtt(client: mqtt.Client) -> None:
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            return
        except Exception as exc:
            print(f"ERROR: MQTT connect failed: {exc}", flush=True)
            time.sleep(READ_INTERVAL_SECONDS)


def main() -> None:
    print("Starting Victron BLE -> MQTT bridge", flush=True)
    print(f"MQTT: {MQTT_HOST}:{MQTT_PORT}", flush=True)
    print(f"Topic base: {MQTT_BASE_TOPIC}", flush=True)
    print(f"Devices: {', '.join([d.split('@')[0] for d in VICTRON_DEVICES])}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    connect_mqtt(client)
    client.loop_start()

    while True:
        try:
            read_victron_once(client)
        except Exception as exc:
            print(f"ERROR: {exc}", flush=True)
        time.sleep(READ_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
