import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import app


class VictronApiTests(unittest.TestCase):
    def setUp(self):
        app.latest_metrics.clear()
        self.client = TestClient(app.app)

    def test_waiting_response_stays_503(self):
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json(),{"status":"waiting_for_mqtt_data","missing":[]})

    def test_gateway_json_is_indexed_by_device_name(self):
        data = {"timestamp":"2026-09-07T08:00:00+00:00","name":"smartsolar_pyleas",
                "battery_voltage":12.58,"battery_charging_current":0.6,
                "charge_state":"bulk","solar_power":9,"yield_today":20,"rssi":-42}
        app.on_message(None,None,SimpleNamespace(topic=app.MQTT_TOPIC,payload=json.dumps(data).encode()))
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{"smartsolar_pyleas":data})
        self.assertEqual(
            self.client.get("/api/health").json()["last_message_timestamps"],
            {"smartsolar_pyleas":data["timestamp"]},
        )

    def test_api_subscribes_to_discovery_topic(self):
        client = Mock()
        app.on_connect(client,None,None,0,None)
        client.subscribe.assert_called_once_with(app.MQTT_TOPIC)

    def test_comma_separated_configuration_subscribes_to_exact_topics(self):
        original_devices = app.MQTT_DEVICES
        app.MQTT_DEVICES = ("smartsolar_pyleas","batteryprotec_pyleas","ve_direct_pyleas")
        try:
            client = Mock()
            app.on_connect(client,None,None,0,None)
            self.assertEqual(
                [call.args[0] for call in client.subscribe.call_args_list],
                [f"van/victron/{name}" for name in app.MQTT_DEVICES],
            )
        finally:
            app.MQTT_DEVICES = original_devices

    def test_comma_separated_names_return_three_devices(self):
        names = ("smartsolar_pyleas","batteryprotec_pyleas","ve_direct_pyleas")
        for index, device_name in enumerate(names):
            data = {"timestamp":f"2026-09-07T08:00:0{index}+00:00","name":device_name,"value":index}
            app.on_message(None,None,SimpleNamespace(topic=f"van/victron/{device_name}",payload=json.dumps(data).encode()))

        response = self.client.get("/api/metrics",params={"names":",".join(names)})
        self.assertEqual(response.status_code,200)
        self.assertEqual(list(response.json()),list(names))
        self.assertEqual(response.json()["batteryprotec_pyleas"]["value"],1)

    def test_requested_missing_device_returns_503(self):
        response = self.client.get("/api/metrics?name=unknown")
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json()["missing"],["unknown"])

    def test_older_retained_message_does_not_replace_new_reading(self):
        new = {"timestamp":"2026-09-07T13:21:25+00:00","name":"smartsolar_pyleas","solar_power":69}
        old = {"timestamp":"2026-09-05T16:27:31+00:00","name":"smartsolar_pyleas","solar_power":20}
        for data in (new,old):
            app.on_message(None,None,SimpleNamespace(topic=app.MQTT_TOPIC,payload=json.dumps(data).encode()))
        self.assertEqual(app.latest_metrics["smartsolar_pyleas"]["solar_power"],69)

    def test_invalid_json_does_not_replace_reading(self):
        app.latest_metrics["smartsolar_pyleas"] = {"name":"smartsolar_pyleas","solar_power":9}
        for raw in (b"bad-json",b"[]",b"online"):
            app.on_message(None,None,SimpleNamespace(topic=app.MQTT_TOPIC,payload=raw))
        self.assertEqual(app.latest_metrics["smartsolar_pyleas"]["solar_power"],9)


if __name__ == "__main__":
    unittest.main()
