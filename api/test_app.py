import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import app


class VictronApiTests(unittest.TestCase):
    def setUp(self):
        app.latest_metrics = None
        self.client = TestClient(app.app)

    def test_waiting_response_stays_503(self):
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json(),{"status":"waiting_for_mqtt_data"})

    def test_gateway_json_keeps_existing_http_contract(self):
        data = {"timestamp":"2026-09-07T08:00:00+00:00","name":"SmartSolar Pyleas",
                "battery_voltage":12.58,"battery_charging_current":0.6,
                "charge_state":"bulk","solar_power":9,"yield_today":20,"rssi":-42}
        app.on_message(None,None,SimpleNamespace(topic=app.MQTT_TOPIC,payload=json.dumps(data).encode()))
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{key:data[key] for key in app.METRIC_FIELDS})
        self.assertEqual(self.client.get("/api/health").json()["last_message_timestamp"],data["timestamp"])

    def test_api_subscribes_to_configured_full_json_topic(self):
        client = Mock()
        app.on_connect(client,None,None,0,None)
        client.subscribe.assert_called_once_with(app.MQTT_TOPIC)

    def test_invalid_json_does_not_replace_reading(self):
        app.latest_metrics = {"solar_power":9}
        for raw in (b"bad-json",b"[]",b"online"):
            app.on_message(None,None,SimpleNamespace(topic=app.MQTT_TOPIC,payload=raw))
        self.assertEqual(app.latest_metrics,{"solar_power":9})


if __name__ == "__main__":
    unittest.main()
