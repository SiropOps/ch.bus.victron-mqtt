from __future__ import annotations

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


def parse_victron_devices(value: str) -> list[str]:
    """Parse device arguments while keeping their encryption keys opaque."""
    devices = [item.strip() for item in value.split(",") if item.strip()]
    for index, device in enumerate(devices, start=1):
        address, separator, encryption_key = device.partition("@")
        if not separator or not address.strip() or not encryption_key.strip():
            raise ValueError(
                f"Invalid VICTRON_DEVICES entry at position {index}; expected MAC@ENCRYPTION_KEY"
            )
    return devices


def normalize_address(address: str) -> str:
    return address.strip().lower()


def device_address(device: str) -> str:
    """Return only the non-secret address part of a configured device."""
    return device.partition("@")[0].strip()


def configured_addresses(devices: list[str] | None = None) -> set[str]:
    source = VICTRON_DEVICES if devices is None else devices
    return {normalize_address(device_address(device)) for device in source}


VICTRON_DEVICES = parse_victron_devices(env("VICTRON_DEVICES", ""))
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
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    name = data.get("name") if isinstance(data.get("name"), str) else "device"
    address = (
        data.get("address") if isinstance(data.get("address"), str) else "unknown"
    )
    model_name = (
        payload.get("model_name")
        if isinstance(payload.get("model_name"), str)
        else name
    )

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

    # Individual scalar values support simple dashboards and Node-RED flows.
    for key, value in enriched.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            client.publish(f"{device_topic}/{key}", json.dumps(value), retain=True)


def _read_process_output(
    process: subprocess.Popen, output: queue.Queue[str | BaseException | None]
) -> None:
    """Feed one queue from stdout so the cycle deadline cannot be blocked."""
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                output.put(None)
                return
            output.put(line)
    except BaseException as exc:
        output.put(exc)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _display_addresses(devices: list[str]) -> dict[str, str]:
    return {
        normalize_address(device_address(device)): device_address(device)
        for device in devices
    }


def _format_addresses(addresses: set[str], display: dict[str, str]) -> str:
    return ", ".join(display[address] for address in sorted(addresses))


def read_victron_cycle(client: mqtt.Client) -> set[str]:
    if not VICTRON_DEVICES:
        raise RuntimeError("VICTRON_DEVICES is empty; expected MAC@ENCRYPTION_KEY")

    expected = configured_addresses()
    display = _display_addresses(VICTRON_DEVICES)
    received: set[str] = set()
    output: queue.Queue[str | BaseException | None] = queue.Queue()

    print(
        f"Victron read cycle started: expecting {len(expected)} devices", flush=True
    )
    process = subprocess.Popen(
        ["victron-ble", "read", *VICTRON_DEVICES],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    threading.Thread(
        target=_read_process_output, args=(process, output), daemon=True
    ).start()
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS

    try:
        while received != expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing = expected - received
                print(
                    "WARNING: Victron read timeout; missing devices: "
                    f"{_format_addresses(missing, display)}",
                    flush=True,
                )
                break

            try:
                line = output.get(timeout=remaining)
            except queue.Empty:
                missing = expected - received
                print(
                    "WARNING: Victron read timeout; missing devices: "
                    f"{_format_addresses(missing, display)}",
                    flush=True,
                )
                break

            if isinstance(line, BaseException):
                raise line
            if line is None:
                missing = expected - received
                print(
                    "WARNING: victron-ble exited before cycle complete "
                    f"(code {process.poll()}); missing devices: "
                    f"{_format_addresses(missing, display)}",
                    flush=True,
                )
                break

            line = line.strip()
            if not line.startswith("{"):
                continue

            try:
                data = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue

            if (
                not isinstance(data, dict)
                or not isinstance(data.get("address"), str)
                or not isinstance(data.get("payload"), dict)
            ):
                continue

            address = normalize_address(data["address"])
            if address not in expected:
                print(
                    f"WARNING: Ignoring unconfigured Victron device {data['address']}",
                    flush=True,
                )
                continue

            publish_device(client, data)
            if address not in received:
                received.add(address)
                print(
                    f"Received {display[address]} - {data.get('name', 'device')}",
                    flush=True,
                )

        if received == expected:
            print(
                f"Victron read cycle complete: {len(received)}/{len(expected)} devices",
                flush=True,
            )
        return received
    finally:
        stop_process(process)


# Backward-compatible name for callers of the original single-device function.
read_victron_once = read_victron_cycle


def on_connect(client: mqtt.Client, userdata, flags, reason_code, properties) -> None:
    if reason_code == 0:
        client.publish(MQTT_STATUS_TOPIC, "online", retain=True)
        print(f"MQTT connected; published {MQTT_STATUS_TOPIC}=online", flush=True)
    else:
        print(f"MQTT connection failed: {reason_code}", flush=True)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties) -> None:
    if reason_code != 0:
        print(f"WARNING: MQTT disconnected unexpectedly: {reason_code}", flush=True)


def connect_mqtt(client: mqtt.Client) -> None:
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, 60)
            return
        except Exception as exc:
            print(f"ERROR: MQTT connect failed: {exc}", flush=True)
            time.sleep(READ_INTERVAL_SECONDS)


def main() -> None:
    print("Starting Victron BLE multi-device -> MQTT bridge", flush=True)
    print(f"MQTT: {MQTT_HOST}:{MQTT_PORT}", flush=True)
    print(f"Topic base: {MQTT_BASE_TOPIC}", flush=True)
    print(f"Devices configured: {len(VICTRON_DEVICES)}", flush=True)
    for device in VICTRON_DEVICES:
        print(f"- {device_address(device)}", flush=True)
    print(f"Read timeout: {READ_TIMEOUT_SECONDS}s", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.will_set(MQTT_STATUS_TOPIC, "offline", retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=READ_INTERVAL_SECONDS)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    connect_mqtt(client)
    client.loop_start()

    try:
        while True:
            try:
                read_victron_cycle(client)
            except Exception as exc:
                print(f"ERROR: Victron read cycle failed: {exc}", flush=True)
            time.sleep(READ_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("Stopping Victron BLE -> MQTT bridge", flush=True)
    finally:
        client.publish(MQTT_STATUS_TOPIC, "offline", retain=True)
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
