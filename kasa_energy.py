"""Poll every TP-Link Kasa device on the LAN for on/off state and energy usage.

Reports power state plus voltage (V), power (W) and current (A) for each device
that has an energy meter, along with energy consumed today, over the rolling
last 7 and 30 days, and for the calendar month to date. Devices without a meter
(plain plugs, most bulbs) are still listed with their on/off state.

Usage:
    python kasa_energy.py
    python kasa_energy.py --format json
    python kasa_energy.py --host 192.168.1.50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from kasa import Credentials, Device, Discover, Module
from kasa.interfaces.energy import Energy

# Spans of the two rolling windows, in days, counting today as day one.
WEEK_DAYS = 7
ROLLING_DAYS = 30


@dataclass
class Reading:
    """Flattened view of one device (or one outlet of a power strip)."""

    alias: str
    host: str
    model: str
    device_type: str
    is_on: bool | None
    has_energy_meter: bool
    # True for a power-strip parent whose reading is the sum of its outlets.
    # Kept in the table for visibility but excluded from the site-wide total.
    is_aggregate: bool = False
    volts: float | None = None
    watts: float | None = None
    amps: float | None = None
    kwh_today: float | None = None
    # Rolling windows ending today; None when the hardware keeps no day history.
    kwh_week: float | None = None
    kwh_last_30d: float | None = None
    # Calendar month to date, i.e. the 1st through today - not a rolling window.
    kwh_month_to_date: float | None = None
    kwh_total: float | None = None
    error: str | None = None
    # A failed history query leaves the live readings above usable, so it is
    # reported separately from `error` rather than voiding the whole row.
    history_error: str | None = None


@dataclass
class Config:
    username: str | None
    password: str | None
    target: str
    timeout: int
    hosts: list[str] = field(default_factory=list)
    output_format: str = "table"

    @property
    def credentials(self) -> Credentials | None:
        if self.username and self.password:
            return Credentials(self.username, self.password)
        return None


def load_config() -> Config:
    # Anchor to the script's own directory so the .env is found no matter
    # which working directory the script is invoked from.
    load_dotenv(Path(__file__).resolve().parent / ".env")

    raw_timeout = os.getenv("KASA_DISCOVERY_TIMEOUT", "5").strip() or "5"
    try:
        timeout = int(raw_timeout)
    except ValueError:
        sys.exit(f"KASA_DISCOVERY_TIMEOUT must be an integer, got {raw_timeout!r}")

    hosts = [h.strip() for h in os.getenv("KASA_HOSTS", "").split(",") if h.strip()]

    return Config(
        username=os.getenv("KASA_USERNAME", "").strip() or None,
        password=os.getenv("KASA_PASSWORD", "").strip() or None,
        target=os.getenv("KASA_DISCOVERY_TARGET", "255.255.255.255").strip(),
        timeout=timeout,
        hosts=hosts,
        output_format=os.getenv("KASA_OUTPUT_FORMAT", "table").strip().lower(),
    )


async def find_devices(cfg: Config) -> list[Device]:
    """Return devices from explicit hosts if configured, else via broadcast."""
    if cfg.hosts:
        results = await asyncio.gather(
            *(
                Discover.discover_single(
                    host,
                    credentials=cfg.credentials,
                    discovery_timeout=cfg.timeout,
                )
                for host in cfg.hosts
            ),
            return_exceptions=True,
        )
        devices: list[Device] = []
        for host, result in zip(cfg.hosts, results):
            if isinstance(result, BaseException):
                print(f"warning: {host}: {result}", file=sys.stderr)
            elif result is not None:
                devices.append(result)
        return devices

    discovered = await Discover.discover(
        target=cfg.target,
        credentials=cfg.credentials,
        discovery_timeout=cfg.timeout,
    )
    return list(discovered.values())


def day_kwh(entry: dict) -> float:
    """Read one raw day_list entry, which reports either watt-hours or kWh."""
    if "energy_wh" in entry:
        return entry["energy_wh"] / 1000
    return float(entry.get("energy", 0.0))


def as_date(year: int, month: int, day: int) -> date | None:
    """Build a date, or None if the device reported one that cannot exist.

    This hardware is known to emit corrupt registers (see `is_suspect`), so one
    junk entry should cost its own day rather than the whole device's history.
    """
    try:
        return date(int(year), int(month), int(day))
    except (ValueError, TypeError):
        return None


def months_spanned(start: date, end: date) -> list[tuple[int, int]]:
    """List the (year, month) pairs the range touches, oldest first."""
    months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        # Step to the 1st of the next month without overshooting a short one.
        cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
    return months


async def add_month(energy: Energy, days: dict[date, float], year: int, month: int) -> None:
    """Query one calendar month of daily totals into `days`."""
    for day, kwh in (await energy.get_daily_stats(year=year, month=month)).items():
        if when := as_date(year, month, day):
            days[when] = float(kwh)


async def daily_history(energy: Energy, today: date) -> dict[date, float]:
    """Map date -> kWh per day, covering every day the rolling windows need.

    A 30-day window usually spans two calendar months, but three when it reaches
    back over a February, so the months are enumerated rather than assumed.
    """
    days: dict[date, float] = {}

    # An outlet's own meter already has the current month's day list from the
    # regular device update, so reuse it rather than asking again. A strip parent
    # aggregates its outlets and publishes no such list, so it has to be asked.
    raw = getattr(energy, "daily_data", None)
    for entry in raw or []:
        if when := as_date(entry["year"], entry["month"], entry["day"]):
            days[when] = day_kwh(entry)

    for year, month in months_spanned(today - timedelta(days=ROLLING_DAYS - 1), today):
        if raw and (year, month) == (today.year, today.month):
            continue
        await add_month(energy, days, year, month)

    return days


def window_sum(days: dict[date, float], end: date, span: int) -> float:
    """Total the `span` days ending on `end` inclusive.

    Days the device reports nothing for count as zero, which is right for an
    idle outlet but does silently undercount a stretch the meter never saw
    (unplugged, rebooted, or stats erased).
    """
    start = end - timedelta(days=span - 1)
    return sum(kwh for day, kwh in days.items() if start <= day <= end)


async def read_energy(dev: Device, reading: Reading) -> None:
    """Fill the energy fields of `reading` from the device's Energy module."""
    energy = dev.modules.get(Module.Energy)
    if energy is None:
        return

    reading.has_energy_meter = True
    reading.watts = energy.current_consumption
    reading.kwh_today = energy.consumption_today
    reading.kwh_month_to_date = energy.consumption_this_month

    # Voltage/current are only exposed by metered hardware such as the KP115,
    # HS110 and the per-outlet meters in an HS300 strip.
    if energy.supports(Energy.ModuleFeature.VOLTAGE_CURRENT):
        reading.volts = energy.voltage
        reading.amps = energy.current

    if energy.supports(Energy.ModuleFeature.CONSUMPTION_TOTAL):
        reading.kwh_total = energy.consumption_total

    # The rolling windows have to be summed from the per-day history, which only
    # the older emeter hardware keeps. Newer devices report today and the
    # calendar month only.
    if energy.supports(Energy.ModuleFeature.PERIODIC_STATS):
        today = date.today()
        try:
            days = await daily_history(energy, today)
        except Exception as exc:  # noqa: BLE001 - history is a bonus, not fatal
            reading.history_error = f"{type(exc).__name__}: {exc}"
        else:
            reading.kwh_week = window_sum(days, today, WEEK_DAYS)
            reading.kwh_last_30d = window_sum(days, today, ROLLING_DAYS)


async def to_reading(dev: Device, alias_prefix: str = "") -> Reading:
    reading = Reading(
        alias=f"{alias_prefix}{dev.alias or '(unnamed)'}",
        host=dev.host,
        model=dev.model,
        device_type=dev.device_type.value,
        is_on=dev.is_on,
        has_energy_meter=False,
    )
    await read_energy(dev, reading)
    return reading


async def poll(dev: Device) -> list[Reading]:
    """Update one device and flatten it (plus any child outlets) to readings."""
    try:
        await dev.update()
    except Exception as exc:  # noqa: BLE001 - surface any per-device failure
        return [
            Reading(
                alias=dev.alias or dev.host,
                host=dev.host,
                model=dev.model or "?",
                device_type="unknown",
                is_on=None,
                has_energy_meter=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        ]

    # Read the parent and its outlets one at a time: a strip's outlets all share
    # the parent's single connection, so fanning out history queries across them
    # only queues up on the same socket.
    parent = await to_reading(dev)
    # Power strips expose each outlet as a child with its own state and meter.
    children = [await to_reading(child, alias_prefix=f"{dev.alias} / ") for child in dev.children]

    # An HS300 parent reports the sum of its outlets, so counting both would
    # double the total. Prefer the per-outlet numbers when they are metered.
    if any(c.has_energy_meter for c in children):
        parent.is_aggregate = True

    return [parent, *children]


def fmt(value: float | None, places: int) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def leaf_total(leaves: list[Reading], attr: str) -> float:
    """Sum one energy field across the readings that report it."""
    return sum(value for r in leaves if (value := getattr(r, attr)) is not None)


def is_suspect(r: Reading) -> bool:
    """True when reported watts contradict volts x amps by a wide margin.

    Some HS300 outlets intermittently return a corrupt power register (e.g.
    11437 W at 2.6 V / 1.3 A). The raw value is still reported, but counting it
    in the site total would make the total meaningless.
    """
    if r.volts is None or r.amps is None or r.watts is None:
        return False
    implied = r.volts * r.amps
    return r.watts > 10 and r.watts > implied * 3 + 10


def print_table(readings: list[Reading]) -> None:
    headers = [
        "DEVICE",
        "IP",
        "MODEL",
        "TYPE",
        "STATE",
        "VOLTS",
        "WATTS",
        "AMPS",
        "KWH TODAY",
        "KWH WEEK",
        "KWH LAST 30D",
        "KWH MONTH TO DATE",
    ]
    rows = [
        [
            f"{r.alias} *" if r.is_aggregate else r.alias,
            r.host,
            r.model,
            r.device_type,
            "ERROR" if r.error else ("ON" if r.is_on else "OFF"),
            fmt(r.volts, 1),
            fmt(r.watts, 1) + ("!" if is_suspect(r) else ""),
            fmt(r.amps, 3),
            fmt(r.kwh_today, 3),
            fmt(r.kwh_week, 3),
            fmt(r.kwh_last_30d, 3),
            fmt(r.kwh_month_to_date, 3),
        ]
        for r in readings
    ]

    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    sep = "  "
    print(sep.join(h.ljust(w) for h, w in zip(headers, widths)))
    print(sep.join("-" * w for w in widths))
    for row in rows:
        print(sep.join(cell.ljust(w) for cell, w in zip(row, widths)))

    leaves = [r for r in readings if not r.is_aggregate]
    total = sum(r.watts for r in leaves if r.watts is not None and not is_suspect(r))
    metered = sum(1 for r in leaves if r.has_energy_meter)
    print(f"\n{len(readings)} device(s), {metered} metered, {total:.1f} W total")
    print(
        f"site kWh: {leaf_total(leaves, 'kwh_today'):.3f} today, "
        f"{leaf_total(leaves, 'kwh_week'):.3f} last {WEEK_DAYS} days, "
        f"{leaf_total(leaves, 'kwh_last_30d'):.3f} last {ROLLING_DAYS} days, "
        f"{leaf_total(leaves, 'kwh_month_to_date'):.3f} month to date"
    )
    if any(r.is_aggregate for r in readings):
        print("* strip total, already counted via its outlets - excluded from the sum")

    no_history = [r for r in leaves if r.has_energy_meter and r.kwh_week is None]
    if no_history:
        print(
            f"{len(no_history)} metered device(s) keep no per-day history, so they "
            "contribute nothing to the rolling columns:"
        )
        for r in no_history:
            print(f"    {r.alias}: {r.history_error or 'not supported by this device'}")

    suspect = [r for r in readings if is_suspect(r)]
    if suspect:
        print("! reported watts contradict volts x amps - excluded from the sum:")
        for r in suspect:
            print(f"    {r.alias}: {r.watts:.1f} W vs {r.volts:.1f} V x {r.amps:.3f} A")

    for r in readings:
        if r.error:
            print(f"error: {r.alias} ({r.host}): {r.error}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host",
        action="append",
        dest="hosts",
        metavar="IP",
        help="Poll this IP directly instead of discovering. Repeatable.",
    )
    parser.add_argument(
        "--target",
        metavar="BROADCAST",
        help="Override KASA_DISCOVERY_TARGET, e.g. 192.168.1.255.",
    )
    parser.add_argument("--format", choices=["table", "json"], help="Override KASA_OUTPUT_FORMAT.")
    parser.add_argument("--timeout", type=int, help="Override KASA_DISCOVERY_TIMEOUT.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    cfg = load_config()
    if args.hosts:
        cfg.hosts = args.hosts
    if args.target:
        cfg.target = args.target
    if args.format:
        cfg.output_format = args.format
    if args.timeout:
        cfg.timeout = args.timeout

    devices = await find_devices(cfg)
    if not devices:
        print(
            "No devices found.\n"
            "  - Set KASA_DISCOVERY_TARGET to your subnet broadcast (e.g. 192.168.1.255).\n"
            "  - Or set KASA_HOSTS / --host to poll known IPs directly.\n"
            "  - Newer devices need KASA_USERNAME and KASA_PASSWORD.",
            file=sys.stderr,
        )
        return 1

    results = await asyncio.gather(*(poll(dev) for dev in devices))
    readings = sorted((r for group in results for r in group), key=lambda r: r.alias.lower())

    if cfg.output_format == "json":
        print(json.dumps([{**asdict(r), "suspect": is_suspect(r)} for r in readings], indent=2))
    else:
        print_table(readings)

    return 1 if any(r.error for r in readings) else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
