# macOS Upgrade Checklist — Powerwall Dashboard (extended)

## Context
Rob is upgrading macOS (currently **26.5.2 / 25F84**) on the Mac mini that hosts the Powerwall
Dashboard and its add-ons. This checklist covers every component that collects data or serves
dashboards, so after the upgrade you can confirm each one is running, writing fresh data,
and showing correct values. The baseline below was captured live on 2026-10-10 ~08:34 PT,
when everything was healthy.

## Inventory (what runs on this Mac)

| # | Component | How it runs | Writes to / serves |
|---|-----------|-------------|--------------------|
| 1 | Docker Desktop 29.6.1 | Login item (`AutoStart: true`) | Hosts everything below except #6 |
| 2 | Powerwall core stack: `influxdb` 1.8, `pypowerwall` 0.14.9t88, `telegraf` 1.28.2, `grafana` 12.3.1 | `docker compose -f powerwall.yml -f powerwall.extend.yml`, `restart: unless-stopped` | :8086, :8675, :9000 |
| 3 | Tesla gateway (TEDAPI) path | pypowerwall → 192.168.91.1 via home router → MikroTik `powerwall-bridge` (192.168.1.247) wlan1 → TEG-11W. **The Mac has no static route**; routing is handled by the network | `raw.http`, CQs → autogen/kwh/daily/monthly/pwtemps/vitals/alerts |
| 4 | MikroTik SNMP | telegraf + `tools/mikrotik/telegraf-mikrotik.conf` + `telegraf.env` (from the extend file) | `raw.mikrotik_*` |
| 5 | Tesla EV telemetry (`~/Projects/tesla-telemetry`) | Docker compose: `fleet-telemetry`, `vehicle-command`, `ev-bridge`, `caddy`; Tailscale Funnel tcp:443→127.0.0.1:4443 | `autogen.ev` |
| 6 | **ADC**: Omega OM-USB-1608GX flicker DAQ (`~/Projects/flickers/daq`) | LaunchAgent `com.flickers.daq-logger` (uv + native libuldaq/libusb, KeepAlive) | `flicker`, `flicker_severity`, `flicker_health`; CSV in `daq/data/` |
| 7 | LaunchAgents `com.rob.tesla-telemetry.cert-renew` (daily 03:15) and `.ev-freshness` (every 30 min) | launchd gui domain | cert renewal, macOS notifications |
| 8 | Grafana dashboards | Powerwall main (`dashboards/dashboard.json`), MikroTik (`tools/mikrotik/mikrotik-dashboard.json`), EV Charging, Tesla Telemetry Ops, Powerwall Flicker (`flickers/daq/grafana/powerwall-flicker.json`); Sun & Moon datasource plugin | :9000 |
| — | Not active (ignore): Apple `container` supervisor (`apple-supervisor/`, `container` service not running), weather411 (profile off), pvoutput | | |

## Before upgrading (5 min)
- [ ] Optional but safest: `docker compose -f powerwall.yml -f powerwall.extend.yml stop` so InfluxDB shuts down cleanly (prevents a dirty-shutdown WAL replay).
- [ ] Confirm a Time Machine (or other) backup covers `influxdb/` and `grafana/grafana.db`.
- [ ] Note the time the upgrade starts. You'll use it to measure the data gap.
- [ ] Remember: **FileVault is on and auto-login is off.** Nothing restarts until you log in. Log in as soon as the upgrade finishes.

## After the upgrade: checklist

### A. Host and boot
- [ ] Log in. `sw_vers` shows the new version.
- [ ] `pmset -g`: `sleep 0` and `disksleep 0` still set (the upgrade can reset energy settings). Baseline: `sleep 0, disksleep 0, displaysleep 60, womp 1`.
- [ ] System Settings → General → Login Items & Extensions: **Docker** and **Tailscale** are still enabled, and the background items for `uv`/the launch agents haven't been switched off. macOS upgrades sometimes re-prompt for or disable these.
- [ ] Approve any new privacy prompts (Docker networking, Tailscale VPN/system extension, Terminal/uv USB access).

### B. Docker Desktop
- [ ] Docker Desktop is running: `docker version` (baseline server 29.6.1). If you're asked to update Docker for the new macOS, do it now.
- [ ] `docker ps` shows **8 containers Up**: `influxdb`, `pypowerwall`, `telegraf`, `grafana` (all `healthy`), plus `tesla-telemetry-{fleet-telemetry,vehicle-command,ev-bridge,caddy}-1`.
- [ ] If any are missing: `cd ~/Projects/Powerwall-Dashboard && docker compose -f powerwall.yml -f powerwall.extend.yml up -d` and `cd ~/Projects/tesla-telemetry && docker compose up -d`.
  ⚠ Always include `-f powerwall.extend.yml`. Without it, telegraf comes up without the MikroTik SNMP config.
- [ ] Telegraf still has the MikroTik mount: `docker inspect telegraf --format '{{range .Mounts}}{{.Source}} {{end}}'` should list `tools/mikrotik/telegraf-mikrotik.conf`.

### C. Powerwall / Tesla gateway (TEDAPI)
- [ ] `ping -c3 192.168.91.1` succeeds (baseline ~5 ms). If it fails, the problem is on the network side (MikroTik wlan1 or the home router), not the Mac. Check with the `/mikrotik` skill that wlan1 is associated to TEG-11W.
- [ ] `curl -s localhost:8675/stats`: `"mode": "Local"`, and the `gets` counter goes up between two calls. `errors`/`timeout` shouldn't jump quickly (baseline 67 / 1794 over ~2 months).
- [ ] `curl -s localhost:8675/aggregates` returns live site/battery/solar/load power (not empty).
- [ ] `curl -s localhost:8675/soe` returns a sensible SOC percentage.

### D. InfluxDB data freshness
Run this (each line should show a timestamp from the last ~1–2 min, except where noted):
```bash
for q in raw.http autogen.http raw.alerts raw.cpu raw.powerwall_dashboard \
         raw.mikrotik_system raw.mikrotik_interface raw.mikrotik_wireless \
         vitals.http pwtemps.http autogen.ev autogen.flicker autogen.flicker_health \
         autogen.flicker_severity kwh.http daily.http; do
  printf '%-26s ' $q
  docker exec influxdb influx -database powerwall -precision rfc3339 -format csv \
    -execute "SELECT * FROM $q ORDER BY time DESC LIMIT 1" | sed -n 2p | cut -d, -f2
done
```
- [ ] `raw.http`, `raw.alerts`, `raw.cpu`, `raw.powerwall_dashboard`: within ~10 s.
- [ ] `autogen.http`, `pwtemps.http`, `vitals.http`: within ~1 min (CQs running).
- [ ] `raw.mikrotik_*`: within ~30 s.
- [ ] `autogen.ev`: recent **if a car is online/awake**. A sleeping car may go quiet for a while, so compare with the 120-min freshness threshold.
- [ ] `autogen.flicker*`: within ~10 s; `flicker_severity` within 10 min.
- [ ] `kwh.http` at the top of the current hour; `daily.http` at today's midnight PT (07:00Z).
- [ ] CQs are still present: `SHOW CONTINUOUS QUERIES` lists `cq_autogen`, `cq_kwh`, `cq_daily`, `cq_monthly`, `cq_pw_temps`, `cq_vitals*`, etc.
- Expected / ignore: `strings.http` is empty (no string data from this system), `flicker_mode` last written 2026-07-06, and `autogen.mikrotik_*` (MikroTik data goes to the `raw` RP).

### E. Tesla EV telemetry
- [ ] Tailscale is connected: `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`.
- [ ] Funnel is still on: `... Tailscale funnel status` shows `tcp://mac-mini.koala-stargazer.ts.net:443 → tcp://127.0.0.1:4443` (plus https :8443).
- [ ] Cert is valid: `openssl x509 -enddate -noout -in ~/Projects/tesla-telemetry/keys/server.crt` (baseline expires **Dec 21 2026**).
- [ ] `docker logs --since 15m tesla-telemetry-fleet-telemetry-1` shows vehicle connections and **no** `tls: bad certificate`.
- [ ] `ev-bridge` is still writing: `autogen.ev` timestamp advances (see D). If fleet-telemetry restarted and `telemetry.log` was truncated, ev_bridge should self-heal; confirm it does.
- [ ] `launchctl list | grep tesla-telemetry` shows both agents. Run the freshness check once: `launchctl kickstart -k gui/$(id -u)/com.rob.tesla-telemetry.ev-freshness`, then `tail ~/Projects/tesla-telemetry/logs/ev-freshness.log` should say `fresh`.
- [ ] macOS notifications for osascript/Script Editor are still allowed, so stale alerts can show up.

### F. ADC: flicker DAQ (highest risk from an OS upgrade)
- [ ] USB device is visible: `system_profiler SPUSBDataType | grep -i -B2 -A6 omega` (or `1608`).
- [ ] `cd ~/Projects/flickers/daq && uv run scripts/list_devices.py` finds the OM-USB-1608GX.
  If it fails with a library or dlopen error, the native `libuldaq`/Homebrew `libusb` needs a rebuild: `xcode-select --install` (if the CLT were removed), `brew update && brew reinstall libusb`, then `./scripts/setup_macos.sh`.
- [ ] `launchctl list | grep daq-logger` shows a PID (baseline: running; last exit 1 was a pre-existing FIFO overrun, and KeepAlive restarts it).
- [ ] `tail -5 data/logger.out.log` shows per-chunk lines with sensible values. Baseline: **~116–117 V, ~60.02 Hz, THD ~3.2%, mod <1%**, batt `--` (Powerwall correlation is disabled in config).
- [ ] `tail data/logger.err.log` has no repeating new errors. One occasional FIFO overrun is the known pre-existing behaviour.
- [ ] Today's `data/metrics_YYYYMMDD.csv` is growing.

### G. Grafana dashboards (http://localhost:9000)
- [ ] `curl -s localhost:9000/api/health` → `"database": "ok"`, version 12.3.1.
- [ ] Log in. Plugins load (Sun & Moon datasource, piechart, JSON datasource) with no "plugin not found" panels.
- [ ] **Powerwall main dashboard**: live power-flow animation, SOC, solar/home/grid/battery graphs current; kWh/day totals filled; alerts timeline renders; temps/frequency/voltage panels populated.
- [ ] **MikroTik dashboard**: uptime reset is fine; wlan1 signal/CCQ, interface throughput (TX positive) and CPU are current.
- [ ] **EV Charging**: battery level/charging state is current for each VIN.
- [ ] **Tesla Telemetry Ops**: feed liveness green, ingest rate non-zero.
- [ ] **Powerwall Flicker**: mod depth, Pst/Plt and health panels are current.
- [ ] Check from another device on the LAN (e.g. a kiosk or phone) that `http://<mac-mini>:9000` still loads. The macOS firewall may re-prompt for Docker's incoming connections.

### H. Data-gap review (after ~1–2 h)
- [ ] On the Powerwall dashboard, zoom to the upgrade window. You should see one clean gap and no flat or zero lines afterwards.
- [ ] Today's kWh totals will undercount by the downtime. If that matters, backfill from Tesla cloud with `tools/tesla-history/` (an upstream tool, not configured here yet).
- [ ] Next day: confirm `daily.http` produced a row for the upgrade day and that the 03:15 `cert-renew` run logged normally (`logs/cert-renew.log`).

## Verification of this deliverable
Running sections A–G once after the upgrade, with every box ticked, confirms the system is healthy.
The section D loop and the commands in C, E and F are read-only and safe to rerun at any time.
