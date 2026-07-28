#!/usr/bin/env python3
"""Abhängigkeitsfreier Prometheus-Exporter für die lokale IOmeter-HTTP-API."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


EXPORTER_VERSION = os.getenv("EXPORTER_VERSION", "dev")
OBIS_IMPORT_TOTAL = "01-00:01.08.00*ff"
OBIS_IMPORT_TARIFF_1 = "01-00:01.08.01*ff"
OBIS_IMPORT_TARIFF_2 = "01-00:01.08.02*ff"
OBIS_EXPORT_TOTAL = "01-00:02.08.00*ff"
OBIS_POWER = "01-00:10.07.00*ff"
ENDPOINTS = {
    "reading": "/v1/reading",
    "simple": "/v1/json",
    "status": "/v1/status",
}


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} muss eine Zahl sein") from exc
    if value < minimum:
        raise ValueError(f"{name} muss mindestens {minimum} sein")
    return value


def build_base_url() -> str:
    """API-Ursprung aus Host-, Schema- und Port-Umgebungsvariablen bilden."""
    raw_host = os.getenv("IOMETER_HOST", "").strip()
    if not raw_host:
        raise ValueError("IOMETER_HOST darf nicht leer sein")

    default_scheme = os.getenv("IOMETER_SCHEME", "http").strip().lower()
    default_port = os.getenv("IOMETER_PORT", "80").strip()
    candidate = raw_host if "://" in raw_host else f"{default_scheme}://{raw_host}"
    parsed = urlsplit(candidate)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("IOMETER_SCHEME muss http oder https sein")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("IOMETER_HOST muss ein Hostname oder eine IP ohne Pfad sein")
    if not parsed.hostname:
        raise ValueError("IOMETER_HOST enthält keinen gültigen Hostnamen und keine gültige IP")

    port = parsed.port
    if port is None and "://" not in raw_host:
        try:
            port = int(default_port)
        except ValueError as exc:
            raise ValueError("IOMETER_PORT muss eine ganze Zahl sein") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("IOMETER_PORT muss zwischen 1 und 65535 liegen")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_for_scheme = 80 if parsed.scheme == "http" else 443
    port_part = f":{port}" if port is not None and port != default_for_scheme else ""
    return f"{parsed.scheme}://{host}{port_part}"


@dataclass(frozen=True)
class EndpointResult:
    name: str
    status_code: int
    data: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None and 200 <= self.status_code < 300


class IOmeterAPI:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch(self, name: str, path: str) -> EndpointResult:
        request = Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "application/json",
                "User-Agent": f"iometer-prometheus-exporter/{EXPORTER_VERSION}",
            },
        )
        status_code = 0
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status_code = response.status
                raw = response.read()
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Die JSON-Antwort ist kein Objekt")
            return EndpointResult(name=name, status_code=status_code, data=data)
        except HTTPError as exc:
            return EndpointResult(name=name, status_code=exc.code, error=f"HTTP {exc.code}")
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            return EndpointResult(name=name, status_code=status_code, error=str(exc))

    def fetch_all(self) -> dict[str, EndpointResult]:
        with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
            futures = {
                name: pool.submit(self.fetch, name, path)
                for name, path in ENDPOINTS.items()
            }
            return {name: future.result() for name, future in futures.items()}


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ""
    body = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())
    )
    return "{" + body + "}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _state(value: Any, expected: str) -> float:
    return 1.0 if str(value or "").lower() == expected else 0.0


class Metrics:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.declared: set[str] = set()

    def sample(
        self,
        name: str,
        value: float | int,
        *,
        help_text: str,
        metric_type: str = "gauge",
        labels: dict[str, Any] | None = None,
    ) -> None:
        if name not in self.declared:
            self.lines.extend(
                [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"]
            )
            self.declared.add(name)
        self.lines.append(f"{name}{_labels(labels)} {float(value):g}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


class IOmeterCollector:
    def __init__(self, api: IOmeterAPI) -> None:
        self.api = api
        self.lock = threading.Lock()
        self.scrapes_total = 0
        self.errors_total: Counter[str] = Counter()

    def collect(self) -> str:
        started = time.monotonic()
        results = self.api.fetch_all()
        duration = time.monotonic() - started
        now = time.time()

        with self.lock:
            self.scrapes_total += 1
            for name, result in results.items():
                # Status 404 bedeutet, dass diese optionale Gerätefunktion fehlt.
                if not result.ok and not (name == "status" and result.status_code == 404):
                    self.errors_total[name] += 1
            return self._render(results, duration, now)

    def _render(
        self,
        results: dict[str, EndpointResult],
        duration: float,
        now: float,
    ) -> str:
        metrics = Metrics()
        reading_result = results["reading"]
        simple_result = results["simple"]
        status_result = results["status"]

        metrics.sample(
            "iometer_exporter_info",
            1,
            help_text="Statische Informationen über den IOmeter-Exporter.",
            labels={"version": EXPORTER_VERSION},
        )
        metrics.sample(
            "iometer_up",
            int(reading_result.ok or simple_result.ok),
            help_text="Ob mindestens ein IOmeter-Messwertendpunkt erreichbar ist.",
        )
        metrics.sample(
            "iometer_scrapes_total",
            self.scrapes_total,
            help_text="Anzahl der IOmeter-Erfassungsversuche.",
            metric_type="counter",
        )
        metrics.sample(
            "iometer_scrape_duration_seconds",
            duration,
            help_text="Dauer der letzten IOmeter-Erfassung.",
        )

        for name in ENDPOINTS:
            result = results[name]
            metrics.sample(
                "iometer_endpoint_up",
                int(result.ok),
                help_text="Ob ein IOmeter-API-Endpunkt gültiges JSON geliefert hat.",
                labels={"endpoint": name},
            )
            metrics.sample(
                "iometer_endpoint_http_status",
                result.status_code,
                help_text="HTTP-Status eines IOmeter-API-Endpunkts; 0 bedeutet keine Antwort.",
                labels={"endpoint": name},
            )
            metrics.sample(
                "iometer_scrape_errors_total",
                self.errors_total[name],
                help_text="Anzahl fehlgeschlagener IOmeter-Endpunktabfragen.",
                metric_type="counter",
                labels={"endpoint": name},
            )

        reading = reading_result.data or {}
        meter = reading.get("meter") if isinstance(reading.get("meter"), dict) else {}
        meter_reading = (
            meter.get("reading") if isinstance(meter.get("reading"), dict) else {}
        )
        registers_raw = meter_reading.get("registers", [])
        registers = registers_raw if isinstance(registers_raw, list) else []
        by_obis: dict[str, tuple[float, str]] = {}

        for register in registers:
            if not isinstance(register, dict):
                continue
            obis = register.get("obis")
            value = _number(register.get("value"))
            unit = str(register.get("unit", ""))
            if not isinstance(obis, str) or value is None:
                continue
            by_obis[obis] = (value, unit)
            metrics.sample(
                "iometer_register_value",
                value,
                help_text="IOmeter-Rohregisterwert mit OBIS-Code und Quelleinheit.",
                labels={"obis": obis, "unit": unit},
            )

        installation_id = reading.get("installationId")
        meter_number = meter.get("number")
        if installation_id or meter_number:
            metrics.sample(
                "iometer_meter_info",
                1,
                help_text="Informationen zur IOmeter-Installation und zum Stromzähler.",
                labels={
                    "installation_id": installation_id or "",
                    "meter_number": meter_number or "",
                },
            )

        simple = simple_result.data or {}
        canonical_values = {
            "iometer_energy_import_watthours_total": (
                by_obis.get(OBIS_IMPORT_TOTAL, (None, ""))[0]
                if OBIS_IMPORT_TOTAL in by_obis
                else _number(simple.get("energyCounterIn"))
            ),
            "iometer_energy_export_watthours_total": (
                by_obis.get(OBIS_EXPORT_TOTAL, (None, ""))[0]
                if OBIS_EXPORT_TOTAL in by_obis
                else _number(simple.get("energyCounterOut"))
            ),
            "iometer_power_watts": (
                by_obis.get(OBIS_POWER, (None, ""))[0]
                if OBIS_POWER in by_obis
                else _number(simple.get("power"))
            ),
        }
        canonical_help = {
            "iometer_energy_import_watthours_total": "Kumulierte Netzbezugsenergie in Wattstunden.",
            "iometer_energy_export_watthours_total": "Kumulierte Einspeiseenergie in Wattstunden.",
            "iometer_power_watts": "Aktuelle Nettoleistung in Watt; positiv ist Netzbezug, negativ Einspeisung.",
        }
        canonical_types = {
            "iometer_energy_import_watthours_total": "counter",
            "iometer_energy_export_watthours_total": "counter",
            "iometer_power_watts": "gauge",
        }
        for name, value in canonical_values.items():
            if value is not None:
                metrics.sample(
                    name,
                    value,
                    help_text=canonical_help[name],
                    metric_type=canonical_types[name],
                )

        for obis, metric_name, tariff in (
            (OBIS_IMPORT_TARIFF_1, "iometer_energy_import_tariff_watthours_total", "1"),
            (OBIS_IMPORT_TARIFF_2, "iometer_energy_import_tariff_watthours_total", "2"),
        ):
            if obis in by_obis:
                metrics.sample(
                    metric_name,
                    by_obis[obis][0],
                    help_text="Kumulierte Netzbezugsenergie eines Stromtarifs in Wattstunden.",
                    metric_type="counter",
                    labels={"tariff": tariff},
                )

        average_power = _number(simple.get("powerAvg"))
        if average_power is not None:
            metrics.sample(
                "iometer_power_average_watts",
                average_power,
                help_text="Vom IOmeter gemeldete kurzzeitige mittlere Nettoleistung in Watt.",
            )
        power_age_ms = _number(simple.get("agePower"))
        if power_age_ms is not None:
            metrics.sample(
                "iometer_power_sample_age_seconds",
                power_age_ms / 1000.0,
                help_text="Alter des Leistungswerts vom einfachen Endpunkt in Sekunden.",
            )

        reading_timestamp = _timestamp(meter_reading.get("time"))
        if reading_timestamp is not None:
            metrics.sample(
                "iometer_reading_timestamp_seconds",
                reading_timestamp,
                help_text="Unix-Zeitstempel des letzten IOmeter-Messwerts.",
            )
            metrics.sample(
                "iometer_reading_age_seconds",
                max(0, now - reading_timestamp),
                help_text="Alter des letzten IOmeter-Messwerts in Sekunden.",
            )

        metrics.sample(
            "iometer_status_available",
            int(status_result.ok),
            help_text="Ob der optionale IOmeter-Statusendpunkt verfügbar ist.",
        )
        status = status_result.data or {}
        device = status.get("device") if isinstance(status.get("device"), dict) else {}
        bridge = device.get("bridge") if isinstance(device.get("bridge"), dict) else {}
        core = device.get("core") if isinstance(device.get("core"), dict) else {}

        for name, value, help_text in (
            (
                "iometer_bridge_wifi_rssi_dbm",
                _number(bridge.get("rssi")),
                "WLAN-Signalstärke der IOmeter-Bridge in dBm.",
            ),
            (
                "iometer_core_wifi_rssi_dbm",
                _number(core.get("rssi")),
                "Funksignalstärke des IOmeter-Core in dBm.",
            ),
            (
                "iometer_core_battery_percent",
                _number(core.get("batteryLevel")),
                "Akkustand des IOmeter-Core in Prozent.",
            ),
        ):
            if value is not None:
                metrics.sample(name, value, help_text=help_text)

        if status_result.ok:
            metrics.sample(
                "iometer_core_connected",
                _state(core.get("connectionStatus"), "connected"),
                help_text="Ob der IOmeter-Core den Zustand verbunden meldet.",
            )
            metrics.sample(
                "iometer_core_attached",
                _state(core.get("attachmentStatus"), "attached"),
                help_text="Ob der IOmeter-Core den Zustand montiert meldet.",
            )
            metrics.sample(
                "iometer_core_pin_entered",
                _state(core.get("pinStatus"), "entered"),
                help_text="Ob die PIN des Stromzählers eingegeben wurde.",
            )
            metrics.sample(
                "iometer_device_info",
                1,
                help_text="Informationen über IOmeter-Bridge und Core-Firmware.",
                labels={
                    "device_id": device.get("id", ""),
                    "bridge_version": bridge.get("version", ""),
                    "core_version": core.get("version", ""),
                },
            )
            if core.get("powerStatus"):
                metrics.sample(
                    "iometer_core_power_source_info",
                    1,
                    help_text="Vom IOmeter-Core gemeldete aktuelle Stromquelle.",
                    labels={"source": core["powerStatus"]},
                )

        return metrics.render()


class ExporterHandler(BaseHTTPRequestHandler):
    collector: IOmeterCollector

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            body = self.collector.collect().encode()
            self._respond(HTTPStatus.OK, body, "text/plain; version=0.0.4; charset=utf-8")
        elif path == "/healthz":
            self._respond(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/":
            body = json.dumps(
                {
                    "name": "IOmeter Prometheus Exporter",
                    "version": EXPORTER_VERSION,
                    "endpoints": ["/metrics", "/healthz"],
                }
            ).encode()
            self._respond(HTTPStatus.OK, body, "application/json")
        else:
            self._respond(HTTPStatus.NOT_FOUND, b"nicht gefunden\n", "text/plain")

    def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        if os.getenv("EXPORTER_ACCESS_LOG", "").lower() in {"1", "true", "yes"}:
            super().log_message(format_string, *args)


def main() -> None:
    base_url = build_base_url()
    timeout = _env_float("IOMETER_TIMEOUT_SECONDS", 3.0)
    port = int(os.getenv("EXPORTER_PORT", "9786"))
    if not 1 <= port <= 65535:
        raise ValueError("EXPORTER_PORT muss zwischen 1 und 65535 liegen")

    ExporterHandler.collector = IOmeterCollector(IOmeterAPI(base_url, timeout))
    server = ThreadingHTTPServer(("0.0.0.0", port), ExporterHandler)
    print(
        f"IOmeter-Exporter {EXPORTER_VERSION} lauscht auf :{port}; Ziel={base_url}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
