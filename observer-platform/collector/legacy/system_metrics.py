"""Read-only Linux metrics collected from psutil, /proc, and /sys."""
from __future__ import annotations

import re
import socket
import time
from pathlib import Path
from typing import Any

import psutil

from .truth import Status, finite_number, metric

THERMAL_ROOT = Path("/sys/class/thermal")
NPU_DEVFREQ = Path("/sys/class/devfreq/fdab0000.npu")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def collect_thermal_zones(root: Path = THERMAL_ROOT) -> list[dict[str, Any]]:
    zones = []
    try:
        paths = sorted(root.glob("thermal_zone*"), key=lambda p: p.name)
    except OSError:
        paths = []
    for index, path in enumerate(paths):
        zone_type = _read_text(path / "type")
        raw = _read_text(path / "temp")
        value = None
        if raw is not None:
            try:
                parsed = float(raw)
                value = parsed / 1000.0 if abs(parsed) >= 1000 else parsed
            except ValueError:
                pass
        zones.append({
            "zone": path.name,
            "label": zone_type or f"Thermal Zone {index}",
            "temperatureC": finite_number(value),
            "source": str(path / "temp"),
            "status": Status.AVAILABLE.value if finite_number(value) is not None else Status.UNAVAILABLE.value,
        })
    return zones


def collect_network() -> list[dict[str, Any]]:
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return []
    try:
        counters = psutil.net_io_counters(pernic=True)
    except (OSError, RuntimeError):
        counters = {}
    output = []
    for name in sorted(set(addresses) | set(stats)):
        ips = [item.address for item in addresses.get(name, [])
               if item.family in (socket.AF_INET, socket.AF_INET6)]
        stat = stats.get(name)
        counter = counters.get(name)
        output.append({
            "name": name,
            "up": stat.isup if stat else None,
            "speedMbps": stat.speed if stat and stat.speed >= 0 else None,
            "addresses": ips,
            "rxBytes": counter.bytes_recv if counter else None,
            "txBytes": counter.bytes_sent if counter else None,
        })
    return output


class NetworkRateSampler:
    """Rates from successive real counters; resets and missing samples stay null."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = {}
        self.sampled_at = None

    def apply(self, interfaces):
        now = self.clock()
        elapsed = None if self.sampled_at is None else now - self.sampled_at
        current = {}
        for row in interfaces:
            row.update(rxBytesPerSec=None, txBytesPerSec=None)
            values = (row.get('rxBytes'), row.get('txBytes'))
            if row.get('up') is not True or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in values):
                continue
            current[row['name']] = values
            old = self.previous.get(row['name'])
            if old is None or elapsed is None or not 0 < elapsed <= 5:
                continue
            deltas = [value - before for value, before in zip(values, old)]
            if all(delta >= 0 for delta in deltas):
                row.update(rxBytesPerSec=deltas[0] / elapsed, txBytesPerSec=deltas[1] / elapsed)
        self.previous, self.sampled_at = current, now
        return interfaces


def primary_ips(interfaces: list[dict[str, Any]]) -> list[str]:
    values = []
    for interface in interfaces:
        if interface.get("name") == "lo":
            continue
        for address in interface.get("addresses", []):
            clean = address.split("%", 1)[0]
            if not clean.startswith("127.") and clean != "::1" and not clean.lower().startswith("fe80:"):
                values.append(clean)
    return values


def collect_npu(devfreq: Path = NPU_DEVFREQ, thermal: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    load_raw = _read_text(devfreq / "load")
    current_raw = _read_text(devfreq / "cur_freq")
    utilization = None
    if load_raw:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)@\d+Hz\s*", load_raw)
        if match:
            candidate = float(match.group(1))
            if 0 <= candidate <= 100:
                utilization = candidate
    frequency_hz = None
    if current_raw:
        try:
            frequency_hz = finite_number(float(current_raw))
        except ValueError:
            pass
    npu_zone = next((zone for zone in (thermal or [])
                     if "npu" in str(zone.get("label", "")).lower()), None)
    available = utilization is not None or frequency_hz is not None or npu_zone is not None
    return {
        "status": Status.AVAILABLE.value if available else Status.UNAVAILABLE.value,
        "utilization": metric(utilization,
            Status.AVAILABLE if utilization is not None else Status.UNAVAILABLE,
            str(devfreq / "load"), unit="%"),
        "frequencyHz": metric(frequency_hz,
            Status.AVAILABLE if frequency_hz is not None else Status.UNAVAILABLE,
            str(devfreq / "cur_freq"), unit="Hz"),
        "temperatureC": metric(
            None if npu_zone is None else npu_zone.get("temperatureC"),
            Status.AVAILABLE if npu_zone and npu_zone.get("temperatureC") is not None else Status.UNAVAILABLE,
            None if npu_zone is None else npu_zone.get("source", ""), unit="°C"),
        "modelName": None,
        "modelVersion": None,
        "inferenceFps": None,
        "inferenceLatencyMs": None,
    }


class SystemCollector:
    def __init__(self):
        psutil.cpu_percent(interval=None)
        self.network_rates = NetworkRateSampler()

    def collect(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        interfaces = self.network_rates.apply(collect_network())
        thermal = collect_thermal_zones()
        try:
            load = list(psutil.getloadavg())
        except (AttributeError, OSError):
            load = [None, None, None]
        try:
            uptime = max(0.0, psutil.boot_time())
            uptime = max(0.0, __import__("time").time() - uptime)
        except (OSError, ValueError):
            uptime = None
        temperatures = [z for z in thermal if z.get("temperatureC") is not None]
        primary = temperatures[0] if temperatures else None
        return {
            "status": Status.AVAILABLE.value,
            "hostname": socket.gethostname(),
            "ips": primary_ips(interfaces),
            "cpu": metric(psutil.cpu_percent(interval=None), Status.AVAILABLE,
                          "psutil.cpu_percent", unit="%"),
            "ram": {
                "usage": metric(memory.percent, Status.AVAILABLE, "psutil.virtual_memory", unit="%"),
                "usedBytes": memory.used,
                "totalBytes": memory.total,
            },
            "disk": {
                "usage": metric(disk.percent, Status.AVAILABLE, "psutil.disk_usage('/')", unit="%"),
                "usedBytes": disk.used,
                "totalBytes": disk.total,
            },
            "loadAverage": load,
            "uptimeS": finite_number(uptime),
            "network": interfaces,
            "thermalZones": thermal,
            "primaryThermal": primary,
            "npu": collect_npu(thermal=thermal),
        }
