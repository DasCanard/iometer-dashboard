import os
import unittest
from unittest.mock import patch

from app import EndpointResult, IOmeterCollector, build_base_url


READING = {
    "__typename": "iometer.reading.v1",
    "installationId": "installation-1",
    "meter": {
        "number": "meter-1",
        "reading": {
            "time": "2025-01-01T12:00:00Z",
            "registers": [
                {"obis": "01-00:01.08.00*ff", "value": 1234567.8, "unit": "Wh"},
                {"obis": "01-00:02.08.00*ff", "value": 12345.6, "unit": "Wh"},
                {"obis": "01-00:10.07.00*ff", "value": 420, "unit": "W"},
            ],
        },
    },
}

SIMPLE = {
    "power": 422,
    "powerAvg": 418,
    "agePower": 4000,
    "energyCounterIn": 1234567.8,
    "energyCounterOut": 12345.6,
}

STATUS = {
    "device": {
        "id": "device-1",
        "bridge": {"rssi": -61, "version": "bridge-1"},
        "core": {
            "connectionStatus": "connected",
            "rssi": -72,
            "version": "core-1",
            "powerStatus": "battery",
            "batteryLevel": 88,
            "attachmentStatus": "attached",
            "pinStatus": "entered",
        },
    }
}


class FakeAPI:
    def __init__(self, results):
        self.results = results

    def fetch_all(self):
        return self.results


class ExporterTests(unittest.TestCase):
    def test_live_payload_wird_auf_kanonische_metriken_abgebildet(self):
        collector = IOmeterCollector(
            FakeAPI(
                {
                    "reading": EndpointResult("reading", 200, READING),
                    "simple": EndpointResult("simple", 200, SIMPLE),
                    "status": EndpointResult("status", 404, error="HTTP 404"),
                }
            )
        )

        metrics = collector.collect()

        self.assertIn("iometer_up 1", metrics)
        self.assertIn("iometer_power_watts 420", metrics)
        self.assertIn("iometer_power_average_watts 418", metrics)
        self.assertIn("iometer_power_sample_age_seconds 4", metrics)
        self.assertIn("iometer_energy_import_watthours_total 1.23457e+06", metrics)
        self.assertIn('iometer_endpoint_http_status{endpoint="status"} 404', metrics)
        self.assertIn('iometer_scrape_errors_total{endpoint="status"} 0', metrics)

    def test_status_payload_wird_bei_verfuegbarkeit_exportiert(self):
        collector = IOmeterCollector(
            FakeAPI(
                {
                    "reading": EndpointResult("reading", 200, READING),
                    "simple": EndpointResult("simple", 200, SIMPLE),
                    "status": EndpointResult("status", 200, STATUS),
                }
            )
        )

        metrics = collector.collect()

        self.assertIn("iometer_status_available 1", metrics)
        self.assertIn("iometer_core_connected 1", metrics)
        self.assertIn("iometer_core_attached 1", metrics)
        self.assertIn("iometer_core_pin_entered 1", metrics)
        self.assertIn("iometer_core_battery_percent 88", metrics)
        self.assertIn('iometer_core_power_source_info{source="battery"} 1', metrics)

    def test_json_endpunkt_haelt_geraet_bei_lesefehler_online(self):
        collector = IOmeterCollector(
            FakeAPI(
                {
                    "reading": EndpointResult("reading", 0, error="timeout"),
                    "simple": EndpointResult("simple", 200, SIMPLE),
                    "status": EndpointResult("status", 404, error="HTTP 404"),
                }
            )
        )

        metrics = collector.collect()

        self.assertIn("iometer_up 1", metrics)
        self.assertIn("iometer_power_watts 422", metrics)
        self.assertIn('iometer_scrape_errors_total{endpoint="reading"} 1', metrics)

    def test_basis_url_akzeptiert_hostname_ip_oder_origin(self):
        with patch.dict(
            os.environ,
            {"IOMETER_HOST": "iometer.local", "IOMETER_SCHEME": "http", "IOMETER_PORT": "8080"},
            clear=False,
        ):
            self.assertEqual(build_base_url(), "http://iometer.local:8080")

        with patch.dict(
            os.environ,
            {"IOMETER_HOST": "https://iometer.example:8443"},
            clear=False,
        ):
            self.assertEqual(build_base_url(), "https://iometer.example:8443")


if __name__ == "__main__":
    unittest.main()
