# kasaenergy

Polls every TP-Link Kasa device on the LAN and reports power state plus
voltage, watts and amps for each one, along with energy consumed today, over
the rolling last 7 and 30 days, and for the calendar month to date.

## Setup

Create from scratch:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # then edit .env
```

## Configuration

Edit `.env` (see `.env.example` for the full annotated list):

| Variable | Purpose |
| --- | --- |
| `KASA_USERNAME` / `KASA_PASSWORD` | TP-Link cloud login. Required only for newer KLAP/AES devices; the current fleet here works without them. |
| `KASA_DISCOVERY_TARGET` | Subnet broadcast address. Set to `10.0.0.255` — the devices live on the wired `10.0.0.0/24` network. |
| `KASA_DISCOVERY_TIMEOUT` | Seconds to listen for replies. Default `5`; `8` is more reliable here. |
| `KASA_HOSTS` | Optional comma-separated IPs to poll directly, skipping broadcast. |
| `KASA_OUTPUT_FORMAT` | `table` or `json`. |
| `SMTP_SERVER` / `SMTP_PORT` | Mail host for `--sendemail`. Gmail is `smtp.gmail.com` on port `587` (STARTTLS). |
| `SENDER_EMAIL` / `APP_PASSWORD` | The sending mailbox and its 16-character app password, not the account password. For Gmail, enable 2-Step Verification and generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords); the spaces Google displays are stripped for you. |
| `RECEIVER_EMAIL` | Who gets the summary. One address, or several comma-separated. |

> If the machine has several NICs (Wi-Fi `192.168.1.0/24`, wired `10.0.0.0/24`,
> WSL, Tailscale). The default `255.255.255.255` broadcast may leave via the wrong
> interface and find nothing, so the discovery target can be set such as
> `10.0.0.255`.

## Usage

```powershell
.\.venv\Scripts\python.exe kasa_energy.py
.\.venv\Scripts\python.exe kasa_energy.py --format json
.\.venv\Scripts\python.exe kasa_energy.py --host 10.0.0.23
.\.venv\Scripts\python.exe kasa_energy.py --target 192.168.1.255 --timeout 10
.\.venv\Scripts\python.exe kasa_energy.py --sendemail
```

CLI flags override `.env`. Exit code is `1` if any device failed to respond.

## Emailing the summary

`--sendemail` prints the table as usual and then mails the same readings to
`RECEIVER_EMAIL`. The mail settings are read *before* the poll starts, so a
missing one fails immediately instead of after discovery:

```
--sendemail needs these set in .env: SENDER_EMAIL, APP_PASSWORD
```

The message is sent over STARTTLS and carries two parts: plain text, and an
HTML version with the numbers charted — a headline figure for live draw, four
kWh tiles (today / 7d / 30d / month to date), horizontal bar charts for watts,
kWh today and the 30-day window, then the full device table and the same
footnotes the console prints. It is built from nested tables with inline styles
and no images, so it renders in Outlook and Office 365 webmail without
downloading anything. Devices flagged `!` are left out of the charts — an
11,437 W bar would flatten every real one — and named in the footnotes instead.

Exit code is `1` if the mail fails to send, with the SMTP error on stderr.

Microsoft mailboxes are not an option for the sender. Both `smtp.office365.com`
and `smtp-mail.outlook.com` reject an app password with `535 5.7.139
Authentication unsuccessful, basic authentication is disabled` — on personal
Hotmail/Outlook.com addresses and tenant mailboxes alike. A tenant admin can
re-enable it per mailbox (*Authenticated SMTP*); a personal account cannot, and
would need OAuth2. Gmail is the path of least resistance, which is what the
example config uses.

One Windows-specific wrinkle is handled in the code: `smtplib` greets the server
with the local hostname, and a bare machine name like `JamesDesktop` is not a
domain, which Office 365 rejects outright (`501 5.5.4 Invalid domain name`). The
greeting uses the sender's own domain instead.

## Reading the output

Each power strip appears as a parent row plus one row per outlet.

- `*` — the strip's own aggregate reading. It already equals the sum of its
  outlets, so it is shown but **excluded** from the site-wide total to avoid
  double counting.
- `!` — the device reported a wattage that contradicts its own volts x amps.
  The raw value is still printed (and kept in JSON output, alongside a
  `suspect` field), but is excluded from the total.
- `-` — no energy meter on that device. Non-metered hardware here: HS103,
  HS220, KP200, KP303, KP400, EP40. Metered: HS110, HS300.

### Energy columns

| Column | Span |
| --- | --- |
| `KWH TODAY` | Midnight to now. |
| `KWH WEEK` | Rolling: today plus the previous 6 days. |
| `KWH LAST 30D` | Rolling: today plus the previous 29 days. |
| `KWH MONTH TO DATE` | Calendar: the 1st of this month through now. Resets on the 1st. |

The two rolling columns are summed from the device's own per-day history, so
they are only as good as that history: a day the meter never recorded (outlet
unplugged, strip rebooted, stats erased) counts as zero rather than as a gap.
The calendar column comes straight off the device's month counter instead.

Cost: the current month's day list already arrives with the regular poll, so
only the months *before* it cost an extra query per metered endpoint — normally
one, or two in the first days of March, since a 30-day window reaching back over
a short February touches three calendar months. Late in a long month it reaches
back no further than the 1st and costs nothing. Strip *parents* pay one query
more than their outlets, because the aggregate meter publishes no day list of
its own. Total runtime here is about 7 seconds.

Only the older emeter hardware (HS110, HS300) keeps per-day history. Newer
KLAP/SMART devices report today and the calendar month only; on those the two
rolling columns show `-` and the footer names them.

### Known hardware fault

`TP-LINK_Power Strip_47EA` (10.0.0.9) has a flaky energy meter. Outlets
*Garden Flower Feed* and *Garden Propagation Heater* intermittently report
~11,400 W while switched off, at ~2.5 V and a stuck 1.316 A. That is physically
impossible (2.6 V x 1.3 A is about 3.4 W) and it also corrupts the strip's
aggregate row. This is a device-side fault, not a reporting bug; the `!` filter
keeps it from poisoning the total. A power-cycle of that strip may clear it.
