import json
import os
import queue
import re
import subprocess
import threading
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
READ_TIMEOUT_SECONDS = int(env("READ_TIMEOUT_SECONDS", "60"))


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


def read_process_line(process: subprocess.Popen, timeout: float) -> str:
    """Read one line without letting a silent child process block forever."""
    lines: queue.Queue[str | BaseException | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            line = process.stdout.readline()
            lines.put(line if line else None)
        except BaseException as exc:
            lines.put(exc)

    threading.Thread(target=read_stdout, daemon=True).start()

    try:
        result = lines.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(
            f"No Victron output received within {READ_TIMEOUT_SECONDS} seconds"
        ) from exc

    if isinstance(result, BaseException):
        raise result
    if result is None:
        return_code = process.poll()
        raise RuntimeError(f"victron-ble exited before producing data (code {return_code})")
    return result


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def read_victron_once(client: mqtt.Client) -> None:
    if not VICTRON_DEVICES:
        raise RuntimeError("VICTRON_DEVICES is empty. Example: E1:EA:0C:89:CC:C5@your_key")

    cmd = ["victron-ble", "read", *VICTRON_DEVICES]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    deadline = time.monotonic() + READ_TIMEOUT_SECONDS

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No Victron JSON received within {READ_TIMEOUT_SECONDS} seconds"
                )

            line = read_process_line(process, remaining)

            line = line.strip()

            if not line.startswith("{"):
                continue

            publish_device(client, json.loads(line))
            return

    finally:
        stop_process(process)


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
    print(f"Read timeout: {READ_TIMEOUT_SECONDS}s", flush=True)

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
