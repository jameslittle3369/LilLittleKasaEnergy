"""Poll every TP-Link Kasa device on the LAN for on/off state and energy usage.

Reports power state plus voltage (V), power (W) and current (A) for each device
that has an energy meter, along with energy consumed today, over the rolling
last 7 and 30 days, and for the calendar month to date. Devices without a meter
(plain plugs, most bulbs) are still listed with their on/off state.

Usage:
    python kasa_energy.py
    python kasa_energy.py --format json
    python kasa_energy.py --host 192.168.1.50
    python kasa_energy.py --sendemail
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import smtplib
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
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


@dataclass
class SmtpConfig:
    """Office 365 (or any STARTTLS) mail settings, read from the same .env."""

    server: str
    port: int
    sender: str
    password: str
    recipients: list[str]


def env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip() or default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")


def load_smtp() -> SmtpConfig:
    """Read the mail settings, exiting if any required one is missing.

    Called before the poll so a misconfigured mailbox fails in a moment rather
    than after the several seconds discovery takes.
    """
    server = os.getenv("SMTP_SERVER", "").strip()
    sender = os.getenv("SENDER_EMAIL", "").strip()
    # Google shows app passwords as four space-separated groups; the spaces are
    # display only and must not reach the AUTH command.
    password = os.getenv("APP_PASSWORD", "").replace(" ", "").strip()
    # One address or several, comma-separated.
    recipients = [a.strip() for a in os.getenv("RECEIVER_EMAIL", "").split(",") if a.strip()]

    missing = [
        name
        for name, value in (
            ("SMTP_SERVER", server),
            ("SENDER_EMAIL", sender),
            ("APP_PASSWORD", password),
            ("RECEIVER_EMAIL", recipients),
        )
        if not value
    ]
    if missing:
        sys.exit(f"--sendemail needs these set in .env: {', '.join(missing)}")

    return SmtpConfig(
        server=server,
        port=env_int("SMTP_PORT", "587"),
        sender=sender,
        password=password,
        recipients=recipients,
    )


def load_config() -> Config:
    # Anchor to the script's own directory so the .env is found no matter
    # which working directory the script is invoked from.
    load_dotenv(Path(__file__).resolve().parent / ".env")

    timeout = env_int("KASA_DISCOVERY_TIMEOUT", "5")

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


def site_watts(leaves: list[Reading]) -> float:
    """Total live draw, skipping the rows whose meter contradicts itself."""
    return sum(r.watts for r in leaves if r.watts is not None and not is_suspect(r))


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


# --- Email -----------------------------------------------------------------
#
# Everything below builds the --sendemail message. The console path above is
# untouched: this renders the same readings independently, as HTML.
#
# Mail clients are a hostile rendering target - Outlook draws with Word, which
# has no flexbox, no grid and no CSS variables, and remote images are blocked by
# default. So the charts are built the only way that survives: nested tables,
# inline styles, and bar widths as percentages. Nothing is fetched at open time.

# Palette from the dataviz skill's reference instance, checked with its
# validator against the #fcfcfb surface (lightness, chroma, CVD separation and
# contrast all pass). Bars are a single series, so every bar wears the same
# blue - shading them by value would double-encode the length. Red is the
# reserved "critical" status step, used only to mark an impossible reading.
SURFACE = "#fcfcfb"
PAGE = "#f9f9f7"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
INK_MUTED = "#898781"
HAIRLINE = "#e1e0d9"
TRACK = "#f0efec"
SERIES = "#2a78d6"
CRITICAL = "#d03b3b"
FONT = "-apple-system,'Segoe UI',system-ui,Roboto,Helvetica,Arial,sans-serif"

BAR_H = 14  # Bar thickness, px. The spec caps marks at 24px - thin reads calm.


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def chart_rows(leaves: list[Reading], attr: str, skip_suspect: bool) -> list[tuple[str, float]]:
    """Pull one metric into (label, value) pairs, largest first."""
    rows = [
        (r.alias, value)
        for r in leaves
        if (value := getattr(r, attr)) is not None and not (skip_suspect and is_suspect(r))
    ]
    return sorted(rows, key=lambda row: row[1], reverse=True)


def html_bar(pct: float) -> str:
    """One horizontal bar: filled cell + track, drawn as a 2-cell table.

    Percentage widths are the only sizing an email client reliably honours, and
    a table cell is the only element it reliably paints a background on.
    """
    cells = ""
    if pct > 0:
        # Rounded at the data end, square at the baseline it grows from.
        cells += (
            f'<td width="{pct:.2f}%" height="{BAR_H}" style="width:{pct:.2f}%;'
            f"background-color:{SERIES};border-radius:0 4px 4px 0;font-size:1px;"
            f'line-height:{BAR_H}px;">&nbsp;</td>'
        )
    if pct < 100:
        cells += (
            f'<td height="{BAR_H}" style="background-color:{TRACK};font-size:1px;'
            f'line-height:{BAR_H}px;">&nbsp;</td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{cells}</tr></table>"
    )


def html_chart(title: str, note: str, rows: list[tuple[str, float]], unit: str, places: int) -> str:
    """A labelled horizontal bar chart. Returns "" when there is nothing to plot."""
    if not rows:
        return ""

    peak = max(value for _, value in rows)
    body = []
    for label, value in rows:
        # A tiny non-zero value still gets a sliver, so "on but idle" is
        # visibly different from "off".
        pct = 0.0 if peak <= 0 else max(value / peak * 100, 0.8 if value > 0 else 0.0)
        body.append(
            "<tr>"
            f'<td style="width:34%;padding:3px 12px 3px 0;font-family:{FONT};font-size:12px;'
            f'color:{INK_SOFT};">{esc(label)}</td>'
            f'<td style="padding:3px 0;">{html_bar(pct)}</td>'
            f'<td style="width:78px;padding:3px 0 3px 10px;text-align:right;font-family:{FONT};'
            f"font-size:12px;font-weight:600;color:{INK};font-variant-numeric:tabular-nums;"
            f'white-space:nowrap;">{value:.{places}f} {esc(unit)}</td>'
            "</tr>"
        )

    return (
        f'<tr><td style="padding:22px 24px 0 24px;">'
        f'<div style="font-family:{FONT};font-size:14px;font-weight:600;color:{INK};">'
        f"{esc(title)}</div>"
        f'<div style="font-family:{FONT};font-size:12px;color:{INK_MUTED};padding-top:2px;">'
        f"{esc(note)}</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="padding-top:10px;">{"".join(body)}</table>'
        "</td></tr>"
    )


def html_tile(label: str, value: str, unit: str) -> str:
    return (
        f'<td width="25%" style="width:25%;padding:12px 14px;background-color:#ffffff;'
        f'border:1px solid {HAIRLINE};">'
        f'<div style="font-family:{FONT};font-size:11px;color:{INK_MUTED};'
        f'text-transform:uppercase;letter-spacing:0.04em;">{esc(label)}</div>'
        f'<div style="font-family:{FONT};font-size:20px;font-weight:600;color:{INK};'
        f'padding-top:4px;white-space:nowrap;">{esc(value)}'
        f'<span style="font-size:12px;font-weight:400;color:{INK_SOFT};"> {esc(unit)}</span>'
        "</div></td>"
    )


def html_device_table(readings: list[Reading]) -> str:
    """The table view - every value in the charts is also readable here."""
    headers = ["Device", "IP", "Model", "State", "Volts", "Watts", "Amps", "Today", "7d", "30d", "MTD"]
    head = "".join(
        f'<th align="{"left" if i < 4 else "right"}" style="padding:6px 8px;font-family:{FONT};'
        f"font-size:11px;font-weight:600;color:{INK_MUTED};text-transform:uppercase;"
        f'letter-spacing:0.04em;border-bottom:1px solid {HAIRLINE};">{esc(h)}</th>'
        for i, h in enumerate(headers)
    )

    body = []
    for r in readings:
        if r.error:
            state = f'<span style="color:{CRITICAL};font-weight:600;">ERROR</span>'
        else:
            state = "On" if r.is_on else f'<span style="color:{INK_MUTED};">Off</span>'
        watts = fmt(r.watts, 1)
        if is_suspect(r):
            watts = f'<span style="color:{CRITICAL};">{watts} !</span>'

        cells = [
            esc(r.alias) + (" *" if r.is_aggregate else ""),
            esc(r.host),
            esc(r.model),
            state,
            fmt(r.volts, 1),
            watts,
            fmt(r.amps, 3),
            fmt(r.kwh_today, 3),
            fmt(r.kwh_week, 3),
            fmt(r.kwh_last_30d, 3),
            fmt(r.kwh_month_to_date, 3),
        ]
        body.append(
            "<tr>"
            + "".join(
                f'<td align="{"left" if i < 4 else "right"}" style="padding:6px 8px;'
                f"font-family:{FONT};font-size:12px;color:{INK_SOFT};"
                f"font-variant-numeric:tabular-nums;border-bottom:1px solid {HAIRLINE};"
                # Only the device name is allowed to wrap; a split number reads
                # as two numbers.
                f'{"" if i == 0 else "white-space:nowrap;"}">{cell}</td>'
                for i, cell in enumerate(cells)
            )
            + "</tr>"
        )

    return (
        f'<tr><td style="padding:22px 24px 0 24px;">'
        f'<div style="font-family:{FONT};font-size:14px;font-weight:600;color:{INK};'
        f'padding-bottom:8px;">All devices</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{head}</tr>{''.join(body)}</table></td></tr>"
    )


def summary_notes(readings: list[Reading], leaves: list[Reading]) -> list[str]:
    """The same caveats the console footer carries, as plain sentences."""
    notes = []
    if any(r.is_aggregate for r in readings):
        notes.append(
            "* Strip totals are shown but excluded from the sums, since their outlets "
            "are already counted."
        )
    for r in readings:
        if is_suspect(r):
            notes.append(
                f"! {r.alias} reported {r.watts:.1f} W against {r.volts:.1f} V x "
                f"{r.amps:.3f} A, which is impossible - left out of the charts and totals."
            )
    no_history = [r for r in leaves if r.has_energy_meter and r.kwh_week is None]
    if no_history:
        names = ", ".join(r.alias for r in no_history)
        notes.append(
            f"{len(no_history)} metered device(s) keep no per-day history, so they add "
            f"nothing to the 7- and 30-day figures: {names}."
        )
    for r in readings:
        if r.error:
            notes.append(f"{r.alias} ({r.host}) did not respond: {r.error}")
    return notes


def render_summary_html(readings: list[Reading], when: datetime) -> str:
    leaves = [r for r in readings if not r.is_aggregate]
    metered = sum(1 for r in leaves if r.has_energy_meter)
    watts_now = site_watts(leaves)

    tiles = "".join(
        html_tile(label, f"{leaf_total(leaves, attr):.3f}", "kWh")
        for label, attr in (
            ("Today", "kwh_today"),
            (f"Last {WEEK_DAYS} days", "kwh_week"),
            (f"Last {ROLLING_DAYS} days", "kwh_last_30d"),
            ("Month to date", "kwh_month_to_date"),
        )
    )

    charts = (
        html_chart(
            "Live draw by device",
            "Watts at the moment of the poll.",
            chart_rows(leaves, "watts", skip_suspect=True),
            "W",
            1,
        )
        + html_chart(
            "Energy today by device",
            "kWh since midnight.",
            chart_rows(leaves, "kwh_today", skip_suspect=False),
            "kWh",
            3,
        )
        + html_chart(
            f"Last {ROLLING_DAYS} days by device",
            f"kWh over the rolling {ROLLING_DAYS}-day window, for the devices that keep a "
            "per-day history.",
            chart_rows(leaves, "kwh_last_30d", skip_suspect=False),
            "kWh",
            3,
        )
    )

    notes = summary_notes(readings, leaves)
    notes_html = ""
    if notes:
        notes_html = (
            f'<tr><td style="padding:22px 24px 0 24px;">'
            + "".join(
                f'<div style="font-family:{FONT};font-size:12px;color:{INK_MUTED};'
                f'padding-top:4px;">{esc(n)}</div>'
                for n in notes
            )
            + "</td></tr>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
</head>
<body style="margin:0;padding:0;background-color:{PAGE};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background-color:{PAGE};padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="760" cellpadding="0" cellspacing="0" border="0"
       style="width:760px;max-width:100%;background-color:{SURFACE};border:1px solid {HAIRLINE};">

  <tr><td style="padding:24px 24px 0 24px;">
    <div style="font-family:{FONT};font-size:16px;font-weight:600;color:{INK};">
      Kasa energy summary</div>
    <div style="font-family:{FONT};font-size:12px;color:{INK_MUTED};padding-top:2px;">
      {esc(when.strftime('%A %d %B %Y, %H:%M'))}</div>
  </td></tr>

  <tr><td style="padding:18px 24px 0 24px;">
    <div style="font-family:{FONT};font-size:48px;font-weight:600;color:{INK};line-height:1.1;">
      {watts_now:.1f}<span style="font-size:20px;font-weight:400;color:{INK_SOFT};"> W</span></div>
    <div style="font-family:{FONT};font-size:12px;color:{INK_MUTED};padding-top:2px;">
      drawn now across {len(leaves)} device(s), {metered} of them metered</div>
  </td></tr>

  <tr><td style="padding:18px 24px 0 24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="4" border="0">
      <tr>{tiles}</tr>
    </table>
  </td></tr>

  {charts}
  {html_device_table(readings)}
  {notes_html}

  <tr><td style="padding:20px 24px 24px 24px;">
    <div style="font-family:{FONT};font-size:11px;color:{INK_MUTED};
                border-top:1px solid {HAIRLINE};padding-top:12px;">
      Sent by kasa_energy.py --sendemail</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def render_summary_text(readings: list[Reading], when: datetime) -> str:
    """Plain-text alternative, for clients that will not render the HTML part."""
    leaves = [r for r in readings if not r.is_aggregate]
    lines = [
        f"Kasa energy summary - {when:%Y-%m-%d %H:%M}",
        "",
        f"{site_watts(leaves):.1f} W drawn now across {len(leaves)} device(s).",
        f"kWh: {leaf_total(leaves, 'kwh_today'):.3f} today, "
        f"{leaf_total(leaves, 'kwh_week'):.3f} last {WEEK_DAYS} days, "
        f"{leaf_total(leaves, 'kwh_last_30d'):.3f} last {ROLLING_DAYS} days, "
        f"{leaf_total(leaves, 'kwh_month_to_date'):.3f} month to date.",
        "",
    ]
    for r in readings:
        state = "ERROR" if r.error else ("ON " if r.is_on else "OFF")
        lines.append(
            f"  {state}  {r.alias}{' *' if r.is_aggregate else ''}: "
            f"{fmt(r.watts, 1)} W, {fmt(r.kwh_today, 3)} kWh today, "
            f"{fmt(r.kwh_last_30d, 3)} kWh last {ROLLING_DAYS} days"
        )
    notes = summary_notes(readings, leaves)
    if notes:
        lines.append("")
        lines += notes
    return "\n".join(lines)


def send_email(smtp: SmtpConfig, readings: list[Reading], when: datetime) -> None:
    leaves = [r for r in readings if not r.is_aggregate]

    msg = EmailMessage()
    msg["Subject"] = (
        f"Kasa energy: {site_watts(leaves):.1f} W now, "
        f"{leaf_total(leaves, 'kwh_today'):.3f} kWh today - {when:%Y-%m-%d %H:%M}"
    )
    # Office 365 rejects a From that is not the mailbox that authenticated.
    msg["From"] = smtp.sender
    msg["To"] = ", ".join(smtp.recipients)
    msg.set_content(render_summary_text(readings, when))
    msg.add_alternative(render_summary_html(readings, when), subtype="html")

    # smtplib greets with the local hostname, and a bare Windows machine name
    # ("JamesDesktop") is not a domain, which Office 365 rejects outright:
    #   501 5.5.4 Invalid domain name
    # The sender's own domain is a valid FQDN and is what the session
    # authenticates as anyway, so greet with that.
    _, _, domain = smtp.sender.rpartition("@")
    with smtplib.SMTP(
        smtp.server, smtp.port, timeout=30, local_hostname=domain or None
    ) as server:
        server.starttls()
        server.login(smtp.sender, smtp.password)
        server.send_message(msg)


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
    parser.add_argument(
        "--sendemail",
        action="store_true",
        help="Also email the summary to RECEIVER_EMAIL. Needs the SMTP settings in .env.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    cfg = load_config()
    # Read the mail settings up front: a typo in .env should fail now, not
    # after the poll has spent several seconds talking to the LAN.
    smtp = load_smtp() if args.sendemail else None
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

    failed = any(r.error for r in readings)

    if smtp:
        try:
            send_email(smtp, readings, datetime.now())
        except Exception as exc:  # noqa: BLE001 - any mail failure is worth reporting
            print(f"error: could not send mail: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"emailed summary to {', '.join(smtp.recipients)}")

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
