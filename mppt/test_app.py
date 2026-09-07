import io
import json
import sys
import threading
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock


# Keep unit tests independent from the container-only paho-mqtt installation.
if "paho.mqtt.client" not in sys.modules:
    mqtt_client = types.ModuleType("paho.mqtt.client")
    mqtt_client.Client = object
    mqtt_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mqtt_package = types.ModuleType("paho.mqtt")
    mqtt_package.client = mqtt_client
    paho_package = types.ModuleType("paho")
    paho_package.mqtt = mqtt_package
    sys.modules["paho"] = paho_package
    sys.modules["paho.mqtt"] = mqtt_package
    sys.modules["paho.mqtt.client"] = mqtt_client

import app


class FakeMqttClient:
    def __init__(self):
        self.publications = []

    def publish(self, topic, payload, retain=False):
        self.publications.append((topic, payload, retain))


class FakeProcess:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(f"{line}\n" for line in lines))
        self.return_code = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = -15

    def wait(self, timeout=None):
        return self.return_code

    def kill(self):
        self.killed = True
        self.return_code = -9


class BlockingStdout:
    def __init__(self, first_line):
        self.first_line = first_line
        self.delivered = False
        self.release = threading.Event()

    def readline(self):
        if not self.delivered:
            self.delivered = True
            return self.first_line + "\n"
        self.release.wait(1)
        return ""


class BlockingProcess(FakeProcess):
    def __init__(self, first_line):
        super().__init__([])
        self.stdout = BlockingStdout(first_line)

    def terminate(self):
        super().terminate()
        self.stdout.release.set()


class DeviceConfigurationTests(unittest.TestCase):
    def test_parse_multiple_devices_and_extract_addresses_without_keys(self):
        devices = app.parse_victron_devices(
            " AA:BB:CC:DD:EE:01@secret-one,CC:DD:EE:FF:00:02@secret-two "
        )

        self.assertEqual(2, len(devices))
        self.assertEqual("AA:BB:CC:DD:EE:01", app.device_address(devices[0]))
        self.assertEqual(
            {"aa:bb:cc:dd:ee:01", "cc:dd:ee:ff:00:02"},
            app.configured_addresses(devices),
        )
        self.assertNotIn("secret", app.device_address(devices[0]))

    def test_invalid_configuration_error_does_not_expose_key(self):
        secret = "must-not-leak"
        with self.assertRaises(ValueError) as raised:
            app.parse_victron_devices(f"@{secret}")
        self.assertNotIn(secret, str(raised.exception))

    def test_normalize_address_is_case_insensitive(self):
        self.assertEqual(
            "aa:bb:cc:dd:ee:ff", app.normalize_address(" AA:BB:CC:DD:EE:FF ")
        )


class VictronCycleTests(unittest.TestCase):
    def test_cycle_receives_all_configured_devices(self):
        devices = ["AA:AA:AA:AA:AA:01@key-one", "BB:BB:BB:BB:BB:02@key-two"]
        lines = [
            "INFO scanning",
            "{invalid json",
            json.dumps({"address": "CC:CC:CC:CC:CC:03", "name": "Other", "payload": {}}),
            json.dumps({"address": "aa:aa:aa:aa:aa:01", "name": "Solar", "payload": {"battery_voltage": 12.5}}),
            json.dumps({"address": "BB:BB:BB:BB:BB:02", "name": "Inverter", "payload": {"device_state": "off"}}),
        ]
        process = FakeProcess(lines)
        client = FakeMqttClient()

        with mock.patch.object(app, "VICTRON_DEVICES", devices), mock.patch.object(
            app.subprocess, "Popen", return_value=process
        ) as popen, redirect_stdout(io.StringIO()) as output:
            received = app.read_victron_cycle(client)

        self.assertEqual({"aa:aa:aa:aa:aa:01", "bb:bb:bb:bb:bb:02"}, received)
        self.assertEqual(
            ["victron-ble", "read", *devices], popen.call_args.args[0]
        )
        popen.assert_called_once()
        self.assertTrue(process.terminated)
        self.assertIn("Victron read cycle complete: 2/2 devices", output.getvalue())
        self.assertNotIn("key-one", output.getvalue())
        self.assertNotIn("key-two", output.getvalue())

    def test_timeout_keeps_received_data_and_reports_only_missing_mac(self):
        devices = ["AA:AA:AA:AA:AA:01@key-one", "BB:BB:BB:BB:BB:02@key-two"]
        first_payload = json.dumps(
            {"address": "AA:AA:AA:AA:AA:01", "name": "Solar", "payload": {"solar_power": 9}}
        )
        process = BlockingProcess(first_payload)
        client = FakeMqttClient()

        with mock.patch.object(app, "VICTRON_DEVICES", devices), mock.patch.object(
            app, "READ_TIMEOUT_SECONDS", 0.05
        ), mock.patch.object(app.subprocess, "Popen", return_value=process), redirect_stdout(
            io.StringIO()
        ) as output:
            received = app.read_victron_cycle(client)

        log = output.getvalue()
        self.assertEqual({"aa:aa:aa:aa:aa:01"}, received)
        self.assertIn(
            "WARNING: Victron read timeout; missing devices: BB:BB:BB:BB:BB:02",
            log,
        )
        self.assertNotIn("key-one", log)
        self.assertNotIn("key-two", log)
        self.assertTrue(any(topic == "van/victron/solar" for topic, _, _ in client.publications))


class PublishingTests(unittest.TestCase):
    PAYLOADS = (
        (
            "SmartSolar Pyleas",
            "SmartSolar Charger MPPT 100/30",
            {"battery_voltage": 12.58, "solar_power": 9, "charge_state": "bulk"},
            ("battery_voltage", "solar_power"),
        ),
        (
            "VE.Direct Pyleas",
            "Phoenix Inverter 12V 500VA 230V",
            {"device_state": "off", "ac_voltage": 0.08, "ac_current": 0.0, "ac_apparent_power": 0},
            ("device_state", "ac_voltage", "ac_current", "ac_apparent_power"),
        ),
        (
            "BatteryProtec Pyleas",
            "Smart BatteryProtect 12/24V-65A",
            {"input_voltage": 12.56, "output_voltage": 12.56, "output_state": "on", "device_state": "active"},
            ("input_voltage", "output_voltage", "output_state", "device_state"),
        ),
    )

    def test_supported_payloads_publish_json_model_and_scalar_topics(self):
        for name, model_name, payload, scalar_fields in self.PAYLOADS:
            with self.subTest(name=name):
                client = FakeMqttClient()
                app.publish_device(
                    client,
                    {
                        "name": name,
                        "address": "AA:BB:CC:DD:EE:FF",
                        "rssi": -42,
                        "payload": {"model_name": model_name, **payload},
                    },
                )

                device_topic = f"van/victron/{app.topic_safe(name)}"
                topics = {topic for topic, _, _ in client.publications}
                self.assertIn(device_topic, topics)
                self.assertIn(f"van/victron/{app.topic_safe(model_name)}", topics)
                for field in scalar_fields:
                    self.assertIn(f"{device_topic}/{field}", topics)
                self.assertTrue(all(retain for _, _, retain in client.publications))


if __name__ == "__main__":
    unittest.main()
