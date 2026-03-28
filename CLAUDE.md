# Powerwall Dashboard — Project Context

## Architecture Overview

This is a fork of [jasonacox/Powerwall-Dashboard](https://github.com/jasonacox/Powerwall-Dashboard),
running locally on a Mac mini. The stack is Docker Compose:
- **pypowerwall** — polls the Powerwall via TEDAPI and exposes a local API
- **telegraf** — scrapes pypowerwall and writes metrics to InfluxDB
- **influxdb** (v1.8) — time-series database, database name: `powerwall`
- **grafana** — dashboards

## Critical: TEDAPI is WiFi-only

The Tesla TEDAPI (used by pypowerwall) is **only accessible on the Powerwall's
own WiFi network** (`TEG-11W`, 192.168.91.x). It is NOT available on the
Powerwall's ethernet port or from the general home network.

## MikroTik Bridge Router

A **MikroTik RB952Ui-5ac2nD** (`powerwall-bridge`, 192.168.1.247) solves this:

- `wlan1` (2.4 GHz) connects to the Powerwall WiFi (`TEG-11W`) in **station mode**
- `ether1–5` are all bridged and connected to the home ethernet network
- NAT masquerades traffic from the home network out through `wlan1` to reach TEDAPI
- This allows pypowerwall (running on the Mac mini on the home network) to reach
  TEDAPI at 192.168.91.1 via the bridge router

**The wlan1 WiFi link to TEG-11W must be maintained.** Connecting the Powerwall's
ethernet port to the router serves no purpose for this use case.

## MikroTik Monitoring

`tools/mikrotik/` contains:
- `mikrotik.py` — SSH command tool for RouterOS
- `mikrotik-metrics.py` — collects interface throughput, WiFi signal strength
  (wlan1 → TEG-11W), and error counters; writes to InfluxDB `powerwall` database
- `mikrotik-dashboard.json` — Grafana dashboard for router health
- `powerwall-bridge-backup.rsc` — RouterOS config export (keep updated)
- `.env` — SSH credentials (gitignored, never commit)

Run the metrics collector:
```bash
cd tools/mikrotik
uv run mikrotik-metrics.py --loop   # every 30s
uv run mikrotik-metrics.py --dry-run  # test without writing
```

## Repository

- Upstream: `origin` → `https://github.com/jasonacox/Powerwall-Dashboard.git`
- Personal fork: `fork` → `git@github.com:ohhorob/Powerwall-Dashboard.git`
- Local customisations are committed to `fork` remote, not pushed upstream
