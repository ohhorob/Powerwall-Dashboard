#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["paramiko"]
# ///
"""
MikroTik metrics collector for Powerwall Dashboard.

Polls MikroTik router via SSH and writes metrics to InfluxDB:
  - Interface throughput (rx/tx bps) for wlan1 and ether1
  - WiFi signal strength, SNR, CCQ for wlan1 (Powerwall link)
  - WiFi connection status and rates
  - Interface error and drop counters

Usage:
  uv run mikrotik-metrics.py           Run once and exit
  uv run mikrotik-metrics.py --loop    Run every MIKROTIK_INTERVAL seconds (default: 30)
  uv run mikrotik-metrics.py --dry-run Print metrics without writing to InfluxDB

Config via env vars or .env file (same directory):
  MIKROTIK_HOST      Router IP (default: 192.168.88.1)
  MIKROTIK_USER      SSH username (default: admin)
  MIKROTIK_PASSWORD  SSH password
  MIKROTIK_PORT      SSH port (default: 22)
  MIKROTIK_WIFI_IF   WiFi interface to monitor (default: wlan1)
  MIKROTIK_IFACES    Comma-separated interfaces for traffic (default: wlan1,ether1)
  MIKROTIK_INTERVAL  Loop interval in seconds (default: 30)
  INFLUXDB_HOST      InfluxDB host (default: localhost)
  INFLUXDB_PORT      InfluxDB port (default: 8086)
  INFLUXDB_DB        InfluxDB database (default: powerwall)
  INFLUXDB_USER      InfluxDB username (default: empty)
  INFLUXDB_PASS      InfluxDB password (default: empty)
"""

import os
import re
import sys
import time
import argparse
import urllib.request
import urllib.parse
import base64
from pathlib import Path

import paramiko

ENV_FILE = Path(__file__).parent / ".env"
VERSION = "1.0"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_config():
    load_env()
    return {
        "host":     os.environ.get("MIKROTIK_HOST", "192.168.88.1"),
        "port":     int(os.environ.get("MIKROTIK_PORT", "22")),
        "username": os.environ.get("MIKROTIK_USER", "admin"),
        "password": os.environ.get("MIKROTIK_PASSWORD", ""),
        "wifi_if":  os.environ.get("MIKROTIK_WIFI_IF", "wlan1"),
        "ifaces":   os.environ.get("MIKROTIK_IFACES", "wlan1,ether1").split(","),
        "interval": int(os.environ.get("MIKROTIK_INTERVAL", "30")),
        "influx": {
            "host":     os.environ.get("INFLUXDB_HOST", "localhost"),
            "port":     int(os.environ.get("INFLUXDB_PORT", "8086")),
            "db":       os.environ.get("INFLUXDB_DB", "powerwall"),
            "user":     os.environ.get("INFLUXDB_USER", ""),
            "password": os.environ.get("INFLUXDB_PASS", ""),
        },
    }


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def ssh_connect(cfg):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        look_for_keys=False,
        allow_agent=False,
        timeout=10,
    )
    return client


def ssh_run(client, command):
    stdin, stdout, stderr = client.exec_command(command)
    return stdout.read().decode(errors="replace").strip()


# ---------------------------------------------------------------------------
# RouterOS output parsers
# ---------------------------------------------------------------------------

def parse_kv(output):
    """Parse RouterOS key: value output into a dict, stripping ANSI/control chars."""
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKHF]')
    result = {}
    for line in output.splitlines():
        line = ansi_escape.sub('', line).strip()
        if ': ' in line:
            key, _, value = line.partition(': ')
            result[key.strip()] = value.strip()
    return result


def parse_bps(s):
    """Parse RouterOS bps string like '0bps', '1234bps', '1.5Mbps' to int bits/sec."""
    s = s.strip()
    m = re.match(r'([\d.]+)\s*(([kKMGT]?)bps)', s, re.IGNORECASE)
    if not m:
        try:
            return int(s)
        except ValueError:
            return 0
    val = float(m.group(1))
    prefix = m.group(3).upper()
    multipliers = {'': 1, 'K': 1_000, 'M': 1_000_000, 'G': 1_000_000_000, 'T': 1_000_000_000_000}
    return int(val * multipliers.get(prefix, 1))


def parse_monitor_traffic(output):
    """Parse RouterOS monitor-traffic columnar output.

    RouterOS outputs all interfaces in a single table:
      name:  wlan1 ether1
        rx-bits-per-second:   0bps   0bps
        ...

    Returns dict keyed by interface name, each value a dict of fields.
    """
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKHF]')
    lines = []
    for line in output.splitlines():
        line = ansi_escape.sub('', line).strip()
        if line:
            lines.append(line)

    if not lines:
        return {}

    # First line: "name:  wlan1 ether1 ..."
    if not lines[0].startswith("name:"):
        return {}
    iface_names = lines[0][5:].split()
    result = {name: {} for name in iface_names}

    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, values_str = line.partition(":")
        key = key.strip()
        values = values_str.split()
        for i, name in enumerate(iface_names):
            if i < len(values):
                result[name][key] = parse_bps(values[i])

    return result


def parse_interface_stats(output):
    """Parse pipe-delimited interface stats output from RouterOS script.

    Each line: name|rx_byte|tx_byte|rx_error|tx_error|rx_drop|tx_drop|running
    """
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKHF]')
    result = {}
    for line in output.splitlines():
        line = ansi_escape.sub('', line).strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 8:
            continue
        name = parts[0]
        try:
            result[name] = {
                "rx_bytes":  int(parts[1]),
                "tx_bytes":  int(parts[2]),
                "rx_errors": int(parts[3]),
                "tx_errors": int(parts[4]),
                "rx_drops":  int(parts[5]),
                "tx_drops":  int(parts[6]),
                "running":   parts[7].lower() == "true",
            }
        except (ValueError, IndexError):
            continue
    return result


def parse_signal(s):
    """Extract dBm value from strings like '-65dBm@2.4GHz'."""
    m = re.match(r'(-?\d+)dBm', s)
    return int(m.group(1)) if m else None


def parse_rate_mbps(s):
    """Extract Mbps float from strings like '72.2Mbps-HT20/2' or '54Mbps'."""
    m = re.match(r'([\d.]+)(Mbps|Kbps|Gbps|bps)', s, re.IGNORECASE)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    if unit == "kbps":
        val /= 1000
    elif unit == "gbps":
        val *= 1000
    elif unit == "bps":
        val /= 1_000_000
    return val


# ---------------------------------------------------------------------------
# RouterOS commands
# ---------------------------------------------------------------------------

# Collect cumulative byte/error counters for all interfaces via RouterOS scripting
STATS_SCRIPT = (
    "{"
    ":foreach i in=[/interface find] do={"
    ":local n [/interface get $i name];"
    ":local rx [/interface get $i rx-byte];"
    ":local tx [/interface get $i tx-byte];"
    ":local rxe [/interface get $i rx-error];"
    ":local txe [/interface get $i tx-error];"
    ":local rxd [/interface get $i rx-drop];"
    ":local txd [/interface get $i tx-drop];"
    ":local run [/interface get $i running];"
    ":put ($n.\"|\".$rx.\"|\".$tx.\"|\".$rxe.\"|\".$txe.\"|\".$rxd.\"|\".$txd.\"|\".$run)"
    "}}"
)


def collect_metrics(client, cfg):
    """Collect all metrics from the router. Returns list of InfluxDB line protocol strings."""
    wifi_if = cfg["wifi_if"]
    ifaces = cfg["ifaces"]
    host_tag = "powerwall-bridge"
    timestamp_ns = int(time.time() * 1e9)
    lines = []

    # --- Interface traffic (monitor-traffic for real-time bps) ---
    traffic_cmd = f"/interface monitor-traffic {','.join(ifaces)} once"
    traffic_out = ssh_run(client, traffic_cmd)
    traffic = parse_monitor_traffic(traffic_out)

    for iface, data in traffic.items():
        try:
            rx_bps = int(data.get("rx-bits-per-second", 0))
            tx_bps = int(data.get("tx-bits-per-second", 0))
            rx_pps = int(data.get("rx-packets-per-second", 0))
            tx_pps = int(data.get("tx-packets-per-second", 0))
        except (ValueError, TypeError):
            continue
        lines.append(
            f'mikrotik_traffic,host={host_tag},interface={iface} '
            f'rx_bps={rx_bps}i,tx_bps={tx_bps}i,rx_pps={rx_pps}i,tx_pps={tx_pps}i '
            f'{timestamp_ns}'
        )

    # --- Interface error/drop counters ---
    stats_out = ssh_run(client, STATS_SCRIPT)
    stats = parse_interface_stats(stats_out)

    for iface, data in stats.items():
        lines.append(
            f'mikrotik_errors,host={host_tag},interface={iface} '
            f'rx_bytes={data["rx_bytes"]}i,'
            f'tx_bytes={data["tx_bytes"]}i,'
            f'rx_errors={data["rx_errors"]}i,'
            f'tx_errors={data["tx_errors"]}i,'
            f'rx_drops={data["rx_drops"]}i,'
            f'tx_drops={data["tx_drops"]}i,'
            f'running={"1" if data["running"] else "0"}i '
            f'{timestamp_ns}'
        )

    # --- WiFi stats for the Powerwall link ---
    wifi_out = ssh_run(client, f"/interface wireless monitor {wifi_if} once")
    wifi = parse_kv(wifi_out)

    if wifi:
        status = wifi.get("status", "unknown")
        connected = 1 if status == "connected-to-ess" else 0

        signal_raw = wifi.get("signal-strength", "")
        signal_dbm = parse_signal(signal_raw) if signal_raw else None

        snr_raw = re.sub(r'[^\d-]', '', wifi.get("signal-to-noise", ""))
        try:
            snr = int(snr_raw)
        except (ValueError, TypeError):
            snr = None

        ccq_raw = re.sub(r'[^\d]', '', wifi.get("tx-ccq", ""))
        try:
            ccq = int(ccq_raw)
        except (ValueError, TypeError):
            ccq = None

        tx_rate = parse_rate_mbps(wifi.get("tx-rate", ""))
        rx_rate = parse_rate_mbps(wifi.get("rx-rate", ""))

        # Build fields (only include fields we have values for)
        fields = [f"connected={connected}i"]
        if signal_dbm is not None:
            fields.append(f"signal_strength={signal_dbm}i")
        if snr is not None:
            fields.append(f"signal_to_noise={snr}i")
        if ccq is not None:
            fields.append(f"tx_ccq={ccq}i")
        if tx_rate is not None:
            fields.append(f"tx_rate_mbps={tx_rate}")
        if rx_rate is not None:
            fields.append(f"rx_rate_mbps={rx_rate}")

        lines.append(
            f'mikrotik_wifi,host={host_tag},interface={wifi_if} '
            f'{",".join(fields)} '
            f'{timestamp_ns}'
        )

    return lines


# ---------------------------------------------------------------------------
# InfluxDB write
# ---------------------------------------------------------------------------

def write_to_influxdb(cfg, lines):
    if not lines:
        return
    icfg = cfg["influx"]
    data = "\n".join(lines).encode("utf-8")
    url = (
        f'http://{icfg["host"]}:{icfg["port"]}/write'
        f'?db={urllib.parse.quote(icfg["db"])}&precision=ns'
    )
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if icfg["user"]:
        creds = base64.b64encode(f'{icfg["user"]}:{icfg["password"]}'.encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"InfluxDB returned HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(cfg, dry_run=False):
    client = ssh_connect(cfg)
    try:
        lines = collect_metrics(client, cfg)
    finally:
        client.close()

    if dry_run:
        for line in lines:
            print(line)
        return

    write_to_influxdb(cfg, lines)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Wrote {len(lines)} points to InfluxDB")


def main():
    parser = argparse.ArgumentParser(description="MikroTik metrics collector")
    parser.add_argument("--loop",    action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Print metrics, don't write")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    cfg = get_config()

    if args.loop:
        print(f"Starting loop every {cfg['interval']}s — Ctrl-C to stop")
        while True:
            try:
                run_once(cfg, dry_run=args.dry_run)
            except Exception as e:
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {e}", file=sys.stderr)
            time.sleep(cfg["interval"])
    else:
        run_once(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
