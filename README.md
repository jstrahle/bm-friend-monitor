# BrandMeister Monitor → Pushover

A tiny, single-process app that connects to the **public BrandMeister
"Last Heard" real-time stream** and sends a **Pushover** notification when a
watched **DMR ID, callsign, talkgroup, or repeater** is heard on the network.
Built to run as a Podman container.

No BrandMeister account or API key is needed — the Last Heard feed is a public,
read-only Socket.IO stream. After connecting, the client must `join` a
subscription (the app does this automatically).

> **What "spotted" means:** DMR has no persistent "online" presence for end
> users. You only know a station is around when it actually keys up and
> transmits. This app notifies you when a watched target is *heard*.

## Files

| File | Purpose |
|------|---------|
| `bm_monitor.py` | The application |
| `bm_probe.py` | Diagnostic: connects, joins, prints raw events |
| `healthcheck.py` | Container healthcheck (freshness of the health file) |
| `requirements.txt` | Python dependencies |
| `Containerfile` | Image build recipe (Podman/Docker), with HEALTHCHECK |
| `.env.example` | Copy to `.env` and fill in your settings |
| `bm-friend-monitor.container` | Optional systemd Quadlet unit |

## Quick start

1. Get Pushover keys: your **user key** from https://pushover.net, and an
   **API token** from https://pushover.net/apps/build. Install the Pushover app.
2. Find DMR IDs / talkgroup / repeater numbers (radioid.net, or the
   BrandMeister Last Heard page).
3. Configure and run:

```bash
cp .env.example .env          # edit: Pushover keys + what to watch
podman build --format docker -t bm-friend-monitor:latest .
podman run -d --name bm-friend-monitor \
  --env-file .env \
  --restart=unless-stopped \
  --health-on-failure=kill \
  bm-friend-monitor:latest
podman logs -f bm-friend-monitor
```

> **`--format docker` matters.** Podman defaults to OCI image format, which
> silently ignores the image's `HEALTHCHECK` (you'll see a warning) and then
> `--health-on-failure=kill` errors with "cannot set on-failure action to kill
> without a health check." Building with `--format docker` preserves it. If you
> prefer to keep OCI format, define the check at run time instead (see
> "Healthcheck & auto-recovery" below).

On startup the log shows what you're watching and the subscription tokens
(`src_…` for IDs, `dst_…` for talkgroups, `con_…` for repeaters). Key up and
watch for a `MATCH` line, then the notification on your phone.

### Quick test without containers

```bash
pip install -r requirements.txt
set -a; . ./.env; set +a
python bm_monitor.py
```

## What you can watch

| Variable | Filtered server-side? | Matches on |
|----------|----------------------|------------|
| `WATCH_DMR_IDS` | yes (`src_`) | transmitting station's DMR ID (`SourceID`) |
| `WATCH_TALKGROUPS` | yes (`dst_`) | destination talkgroup (`DestinationID`) |
| `WATCH_REPEATERS` | yes (`con_`) | originating repeater (`ContextID`) |
| `WATCH_CALLSIGNS` | no | station callsign (`SourceCall`) |

All are comma-separated and accept optional `=Label` per entry. Setting any
callsign forces the app to receive the full network feed and match locally,
since the feed can only be filtered server-side by ID.

## Quiet hours

Set `QUIET_HOURS=23:00-07:00` (local time, in `QUIET_TZ`, an IANA name like
`Europe/Helsinki`). During that window:

- `QUIET_HOURS_MODE=low` (default) sends notifications at low Pushover priority
  (`QUIET_PRIORITY`, default `-1`) so they arrive silently;
- `QUIET_HOURS_MODE=mute` skips them entirely.

Windows that cross midnight are handled correctly.

## Anti-flood dedup granularity

After a notification fires, that "bucket" stays quiet for `MIN_SILENCE`
seconds. How wide a bucket is can be set per match category, so you get one
behavior for watching people and another for watching busy targets:

| Scope | One bucket per… |
|-------|-----------------|
| `station` | transmitting station (any talkgroup) |
| `station_tg` | (station, talkgroup) |
| `talkgroup` | talkgroup (any station) |
| `repeater` | repeater (any station/talkgroup) |
| `station_repeater` | (station, repeater) |

Defaults: `DEDUP_PERSON=station_tg`, `DEDUP_TALKGROUP=talkgroup`,
`DEDUP_REPEATER=repeater`. So a watched friend is debounced per talkgroup they
appear on, but a watched talkgroup or repeater gives you a single "it's active"
alert rather than one per operator. To instead get a notification for every
operator on a watched talkgroup, set `DEDUP_TALKGROUP=station_tg` (and likewise
`DEDUP_REPEATER=station_repeater`).

## Healthcheck & auto-recovery

The app refreshes `HEALTH_FILE` every ~10s while the socket is connected.
`healthcheck.py` exits non-zero if that file is older than `HEALTH_MAX_AGE`
seconds (default 90) — catching a connection that wedges half-alive without
crashing the process. With `--health-on-failure=kill` plus
`--restart=unless-stopped`, Podman kills and restarts it automatically. Check
status with `podman healthcheck run bm-friend-monitor` or the `STATUS` column
of `podman ps`.

The image carries a `HEALTHCHECK`, **but Podman only honors it when the image
is built in Docker format** (`podman build --format docker ...`); OCI format
ignores it. If you build OCI instead, define the check at run time:

```bash
podman run -d --name bm-friend-monitor \
  --env-file .env --restart=unless-stopped \
  --health-cmd "python /app/healthcheck.py" \
  --health-interval=30s --health-timeout=5s \
  --health-start-period=40s --health-retries=3 \
  --health-on-failure=kill \
  bm-friend-monitor:latest
```

The Quadlet unit defines the healthcheck itself, so the systemd path works
regardless of image format.

## Run as a systemd service (Quadlet)

```bash
mkdir -p ~/.config/containers/systemd
cp bm-friend-monitor.container ~/.config/containers/systemd/
# edit the copied file: confirm Image= and EnvironmentFile= paths
systemctl --user daemon-reload
systemctl --user start bm-friend-monitor
loginctl enable-linger "$USER"   # keep it running after you log out
```

## Configuration reference

| Variable | Default | Meaning |
|----------|---------|---------|
| `PUSHOVER_TOKEN` | — | Pushover application token (required) |
| `PUSHOVER_USER` | — | Pushover user key (required) |
| `WATCH_DMR_IDS` | — | DMR IDs, optional `=Label` |
| `WATCH_CALLSIGNS` | — | Callsigns, optional `=Label` |
| `WATCH_TALKGROUPS` | — | Talkgroup IDs, optional `=Label` |
| `WATCH_REPEATERS` | — | Repeater IDs, optional `=Label` |
| `MIN_SILENCE` | `300` | Seconds before re-notifying the same dedup bucket |
| `MIN_DURATION` | `0` | Ignore transmissions shorter than this (s) |
| `NOTIFY_ON` | `Session-Stop` | Event to act on |
| `DEDUP_PERSON` | `station_tg` | Dedup scope for DMR ID / callsign matches |
| `DEDUP_TALKGROUP` | `talkgroup` | Dedup scope for talkgroup matches |
| `DEDUP_REPEATER` | `repeater` | Dedup scope for repeater matches |
| `QUIET_HOURS` | — | Local-time window `HH:MM-HH:MM` |
| `QUIET_TZ` | `UTC` | IANA timezone for quiet hours |
| `QUIET_HOURS_MODE` | `low` | `low` (quiet priority) or `mute` |
| `QUIET_PRIORITY` | `-1` | Pushover priority used in `low` mode |
| `PUSHOVER_PRIORITY` | `0` | Default Pushover priority, -2..2 |
| `PUSHOVER_DEVICE` | — | Restrict to one device |
| `PUSHOVER_SOUND` | — | Custom Pushover sound name |
| `HEALTH_FILE` | `/tmp/bm_health` | Liveness file watched by HEALTHCHECK |
| `HEALTH_MAX_AGE` | `90` | Seconds before unhealthy |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

For Amateur Radio use only.
