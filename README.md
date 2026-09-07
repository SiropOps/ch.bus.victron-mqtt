# ch.bus.victron-mqtt

MQTT-driven Victron HTTP application. Bluetooth acquisition now belongs entirely
to `ch.bus.bluetooth-mqtt`, together with the environmental BLE sensors.

```text
Victron BLE -> ch.bus.bluetooth-mqtt -> Mosquitto -> api/ (FastAPI)
```

The `mppt/app.py`, `mppt/Dockerfile` and subprocess scanner tests were removed.
This repository no longer needs `victron-ble`, Bleak, BlueZ, a Bluetooth DBus
mount or privileged containers. Build and deploy only the API from this project.

Transfer the existing `VICTRON_DEVICES` and collector MQTT configuration to the
central gateway, stop the old `victron-ble` container, and follow
[the migration procedure](../ch.bus.bluetooth-mqtt/MIGRATION.md). Keep encryption
keys exclusively in the gateway's local environment file; the API needs only
MQTT credentials and a full JSON topic subscription.

## Preserved MQTT contract

The gateway publishes the existing full JSON documents, model-name aliases and
scalar topics, including:

```text
van/victron/smartsolar_pyleas
van/victron/ve_direct_pyleas
van/victron/batteryprotec_pyleas
van/victron/<device>/<metric>
van/victron/<model>
```

Device names can be fixed by a MAC-to-name `VICTRON_NAMES` mapping in the gateway.
Its `MQTT_VICTRON_BASE_TOPIC` can retain an older topic root. The API's application
default remains `van/victron-mppt/smartsolar_pyleas` for existing deployments;
set `MQTT_TOPIC=van/victron/smartsolar_pyleas` when using the current topic family.
The API still exposes the same six SmartSolar metrics. Other devices remain
available through their existing MQTT topics.

Use `van/bluetooth/status` for the gateway's retained status and Last Will, and
`van/bluetooth/health` for scanner health. `van/victron/status` remains a lifecycle
alias; its value can be stale after an abrupt gateway crash until reconnect.

Both APIs default to HTTP port 8013. The example below sets Victron to 8012 to
coexist with the temperature API on one host. Keep the existing port when
upgrading an already configured deployment.

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

## Victron Metrics API

`ch.bus.bluetooth-mqtt` publishes Victron telemetry to MQTT. The lightweight API service subscribes to one full JSON topic and exposes the latest values over HTTP on port `8013`.

This is useful for Inkplate, dashboards, scripts, or other devices that prefer HTTP JSON instead of MQTT.

For compatibility with existing API deployments, its application default remains the legacy topic `van/victron-mppt/smartsolar_pyleas`. New deployments should explicitly configure:

```text
van/victron/smartsolar_pyleas
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
  -e MQTT_TOPIC="van/victron/smartsolar_pyleas" \
  -e API_PORT="8012" \
  ch.bus.victron-mqtt/api:latest
```

Check API logs:

```sh
docker logs -f victron-metrics-api
```

Test health:

```sh
curl http://127.0.0.1:8012/api/health
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
curl http://127.0.0.1:8012/api/metrics
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

## Tests and logs

```sh
python -m pip install -r requirements-test.txt
python -m pytest -q api

docker logs --tail 100 victron-metrics-api
mosquitto_sub -h 127.0.0.1 -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" -t 'van/victron/#' -v
```

API tests require no Bluetooth hardware. The decoder and scanner lifecycle tests
now live in `ch.bus.bluetooth-mqtt`. Do not run a separate BLE discovery process
while that gateway is active. Reuse the existing Mosquitto instance rather than
starting another broker on the same host port.
