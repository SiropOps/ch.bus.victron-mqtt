# ch.bus.victron-mqtt

Docker-based Victron BLE to MQTT bridge for a Raspberry Pi van setup.

The MPPT service reads Victron Instant Readout BLE advertisements with the `victron-ble` CLI and publishes telemetry to a Mosquitto MQTT broker using `paho-mqtt`.

## Architecture

```text
Victron BLE -> Raspberry Pi Docker container -> Mosquitto MQTT -> Home Assistant / Node-RED / Grafana
```

## Prerequisites

- Docker
- Docker Compose v2
- Bluetooth enabled on the Raspberry Pi
- Victron Instant Readout enabled
- Victron encryption key from VictronConnect

## Security

Do not commit real secrets to this repository. Keep these values out of Git:

- Victron encryption keys
- MQTT passwords
- Database passwords
- RabbitMQ passwords

Pass credentials with environment variables or local `.env` files. This repository ignores `.env`, `*.env`, and the real Mosquitto password file at `docker/mosquitto/config/passwords`.

## Docker Network

Create the private MQTT Docker network once:

```sh
docker network create van-mqtt-net
```

The Mosquitto compose file attaches the broker to this external network:

```yaml
networks:
  van-mqtt-net:
    external: true
```

Containers started with normal bridge networking can join this network with `--network van-mqtt-net`.

## Mosquitto Credentials

The broker is configured for username/password authentication. The example placeholders are:

```text
MQTT_USERNAME=victron
MQTT_PASSWORD=change-me
```

Create the real password file locally:

```sh
docker run --rm -it -v "$PWD/docker/mosquitto/config:/mosquitto/config" eclipse-mosquitto:2 mosquitto_passwd -c /mosquitto/config/passwords victron
```

Do not commit `docker/mosquitto/config/passwords`.

## Start Mosquitto

```sh
cd docker/mosquitto
docker compose up -d
```

The compose file publishes host port `1883` so host-networked containers and LAN clients can reach the broker. If every MQTT client is attached only to `van-mqtt-net`, you can remove the `ports` section and use the broker name `van-mqtt` from containers on that network.

## Build the MPPT Image

Run from the repository root:

```sh
docker build -t ch.bus.victron-mqtt/mppt:latest ./mppt
```

## Run MPPT on the Raspberry Pi

BLE scanning usually requires host networking and privileged access to Bluetooth/DBus on Raspberry Pi. This is why the container uses `--net=host`, `--privileged`, and the `/var/run/dbus` mount.

Important limitation: Docker host networking does not behave like a normal bridge network. If `--net=host` is used, the `--network van-mqtt-net` option may not be effective at runtime. In that case, set `MQTT_HOST` to `127.0.0.1` when Mosquitto publishes port `1883` on the same Raspberry Pi, or use the Raspberry Pi host IP address.

```sh
docker run -d \
  --restart=always \
  --name victron-mppt \
  --network van-mqtt-net \
  --privileged \
  --net=host \
  -v /var/run/dbus:/var/run/dbus \
  -e VICTRON_DEVICES="E1:EA:0C:89:CC:C5@CHANGE_ME_ENCRYPTION_KEY" \
  -e MQTT_HOST="127.0.0.1" \
  -e MQTT_PORT="1883" \
  -e MQTT_USERNAME="victron" \
  -e MQTT_PASSWORD="CHANGE_ME_MQTT_PASSWORD" \
  -e MQTT_BASE_TOPIC="van/victron" \
  -e READ_INTERVAL_SECONDS="30" \
  ch.bus.victron-mqtt/mppt:latest
```

## Safer Remote MQTT Example

Use this when Mosquitto runs on another host, such as `192.168.8.200`:

```sh
docker run -d \
  --restart=always \
  --name victron-mppt \
  --net=host \
  --privileged \
  -v /var/run/dbus:/var/run/dbus \
  -e VICTRON_DEVICES="E1:EA:0C:89:CC:C5@CHANGE_ME_ENCRYPTION_KEY" \
  -e MQTT_HOST="192.168.8.200" \
  -e MQTT_PORT="1883" \
  -e MQTT_USERNAME="victron" \
  -e MQTT_PASSWORD="CHANGE_ME_MQTT_PASSWORD" \
  -e MQTT_BASE_TOPIC="van/victron" \
  -e READ_INTERVAL_SECONDS="30" \
  ch.bus.victron-mqtt/mppt:latest
```

## MQTT Topics

Telemetry is published retained under `van/victron/<device>` and `van/victron/<model>`. Simple scalar values are also published under `van/victron/<device>/<field>`.

The bridge publishes a retained status topic:

```text
van/victron/status
```

Payloads are `online` and `offline`.

## Victron Metrics API

The MPPT bridge publishes Victron telemetry to MQTT. The lightweight API service subscribes to the full JSON MPPT topic and exposes the latest values over HTTP on port `8013`.

This is useful for Inkplate, dashboards, scripts, or other devices that prefer HTTP JSON instead of MQTT.

By default, the API subscribes to:

```text
van/victron-mppt/smartsolar_pyleas
```

It exposes:

```text
GET /api/health
GET /api/metrics
```

Build the API image from the repository root:

```sh
docker build -t ch.bus.victron-mqtt/api:latest ./api
```

Run it on the same Raspberry Pi as Mosquitto using host networking:

```sh
docker run -d \
  --restart=always \
  --name victron-metrics-api \
  --net=host \
  -e MQTT_HOST="127.0.0.1" \
  -e MQTT_PORT="1883" \
  -e MQTT_USERNAME="victron" \
  -e MQTT_PASSWORD="CHANGE_ME_MQTT_PASSWORD" \
  -e MQTT_TOPIC="van/victron-mppt/smartsolar_pyleas" \
  -e API_PORT="8013" \
  ch.bus.victron-mqtt/api:latest
```

Check API logs:

```sh
docker logs -f victron-metrics-api
```

Test health:

```sh
curl http://127.0.0.1:8013/api/health
```

Expected health response:

```json
{
  "status": "ok",
  "mqtt_connected": true,
  "last_message_timestamp": "2026-05-22T08:32:11.912940+00:00"
}
```

Test metrics:

```sh
curl http://127.0.0.1:8013/api/metrics
```

Expected metrics response:

```json
{
  "timestamp": "2026-05-22T08:32:11.912940+00:00",
  "battery_charging_current": 0.6,
  "battery_voltage": 12.6,
  "charge_state": "bulk",
  "solar_power": 8,
  "yield_today": 20
}
```

If no valid MQTT data has been received yet, `/api/metrics` returns HTTP `503`:

```json
{
  "status": "waiting_for_mqtt_data"
}
```

Do not commit real MQTT passwords. Keep local `.env` files and other secrets out of Git.

## Testing and Logs

Check Mosquitto logs:

```sh
docker logs -f van-mqtt
```

Subscribe to all van topics:

```sh
docker exec -it van-mqtt mosquitto_sub -u victron -P 'CHANGE_ME_MQTT_PASSWORD' -t 'van/#' -v
```

Check MPPT logs:

```sh
docker logs -f victron-mppt
```

Test Victron discovery manually:

```sh
docker run --rm --net=host --privileged -v /var/run/dbus:/var/run/dbus ch.bus.victron-mqtt/mppt:latest victron-ble discover
```
