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
Its `MQTT_VICTRON_BASE_TOPIC` can retain an older topic root. The API discovers
the full JSON device topics below `van/victron/+`, or subscribes only to the names
listed in `MQTT_DEVICES`. Scalar topics such as
`van/victron/<device>/<metric>` are deliberately not subscribed.

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

`ch.bus.bluetooth-mqtt` publishes Victron telemetry to MQTT. The lightweight API
service discovers the full JSON device topics and exposes the latest value of each
device over HTTP on port `8013`.

This is useful for Inkplate, dashboards, scripts, or other devices that prefer HTTP JSON instead of MQTT.

By default, the API subscribes to `van/victron/+`. To restrict it to the three
known devices, pass only their names, separated by commas:

```text
MQTT_DEVICES=smartsolar_pyleas,batteryprotec_pyleas,ve_direct_pyleas
```

`MQTT_BASE_TOPIC` defaults to `van/victron`. The obsolete single-topic
`MQTT_TOPIC=van/victron-mppt/smartsolar_pyleas` setting is no longer needed.

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
  -e MQTT_BASE_TOPIC="van/victron" \
  -e MQTT_DEVICES="smartsolar_pyleas,batteryprotec_pyleas,ve_direct_pyleas" \
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
  "subscribed_topics": [
    "van/victron/smartsolar_pyleas",
    "van/victron/batteryprotec_pyleas",
    "van/victron/ve_direct_pyleas"
  ],
  "last_message_timestamps": {
    "smartsolar_pyleas": "2026-09-07T13:21:25.712026+00:00",
    "batteryprotec_pyleas": "2026-09-07T13:21:25.706156+00:00",
    "ve_direct_pyleas": "2026-09-07T13:21:25.710403+00:00"
  }
}
```

Test all configured/discovered metrics:

```sh
curl http://127.0.0.1:8012/api/metrics
```

Select the same three devices explicitly (the singular `name` alias is also
accepted):

```sh
curl 'http://127.0.0.1:8012/api/metrics?names=smartsolar_pyleas,batteryprotec_pyleas,ve_direct_pyleas'
```

The response is keyed by the configured device name and retains all fields from
the gateway payload:

```json
{
  "smartsolar_pyleas": {
    "timestamp": "2026-09-07T13:21:25.712026+00:00",
    "name": "smartsolar_pyleas",
    "battery_voltage": 13.35,
    "solar_power": 69
  },
  "batteryprotec_pyleas": {
    "timestamp": "2026-09-07T13:21:25.706156+00:00",
    "name": "batteryprotec_pyleas",
    "input_voltage": 13.28
  },
  "ve_direct_pyleas": {
    "timestamp": "2026-09-07T13:21:25.710403+00:00",
    "name": "ve_direct_pyleas",
    "battery_voltage": 13.29
  }
}
```

If no valid MQTT data has been received yet, `/api/metrics` returns HTTP `503`:

```json
{
  "status": "waiting_for_mqtt_data",
  "missing": ["ve_direct_pyleas"]
}
```

The API never lets an older retained MQTT payload replace a newer reading for
the same device. `/api/health` reports the subscribed topics and the last
timestamp held for every device, which makes a stale retained value visible.

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
