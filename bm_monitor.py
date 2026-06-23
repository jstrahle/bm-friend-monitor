#!/usr/bin/env python3
"""
BrandMeister monitor -> Pushover

Connects to the public BrandMeister "Last Heard" real-time stream and sends a
Pushover notification when a watched DMR ID, callsign, talkgroup, or repeater
is heard on the network. Supports per-(station, talkgroup) anti-flood debounce,
quiet hours, and a health file for container healthchecks.

No BrandMeister API key is required: the Last Heard feed is a public,
read-only Socket.IO stream.

All configuration is read from environment variables (see .env.example),
which makes this convenient to run inside a Podman/Docker container.
"""

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import socketio

# --------------------------------------------------------------------------- #
# Configuration (from environment)
# --------------------------------------------------------------------------- #

BM_URL = os.environ.get("BM_URL", "https://api.brandmeister.network")
BM_SOCKETIO_PATH = os.environ.get("BM_SOCKETIO_PATH", "/lh/socket.io")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "").strip()
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "").strip()
PUSHOVER_PRIORITY = int(os.environ.get("PUSHOVER_PRIORITY", "0"))
PUSHOVER_DEVICE = os.environ.get("PUSHOVER_DEVICE", "").strip()  # optional
PUSHOVER_SOUND = os.environ.get("PUSHOVER_SOUND", "").strip()    # optional

# Only react to this event type. The Last Heard feed emits a "Session-Start"
# and a "Session-Stop" per transmission; we use Session-Stop so we know the
# transmission finished and can report its duration.
NOTIFY_ON = os.environ.get("NOTIFY_ON", "Session-Stop").strip()

# Anti-flood: after notifying for a given station, stay quiet for this many
# seconds before notifying about that same station again.
MIN_SILENCE = int(os.environ.get("MIN_SILENCE", "300"))

# Ignore very short keyups (kerchunks). 0 = report everything.
MIN_DURATION = int(os.environ.get("MIN_DURATION", "0"))

# Drop stale/replayed events whose transmission ended more than this many
# seconds ago (by the event's Stop time). BrandMeister periodically re-emits
# old Last Heard entries, and replays a recent buffer on reconnect; without
# this they would re-notify once MIN_SILENCE elapsed. 0 disables the check.
MAX_EVENT_AGE = int(os.environ.get("MAX_EVENT_AGE", "180"))

# Quiet hours: during this LOCAL-time window, either suppress notifications
# ("mute") or downgrade them to low Pushover priority ("low"). Empty = always
# notify normally.
QUIET_HOURS = os.environ.get("QUIET_HOURS", "").strip()           # e.g. "23:00-07:00"
QUIET_TZ = os.environ.get("QUIET_TZ", "UTC").strip() or "UTC"     # e.g. "Europe/Helsinki"
QUIET_MODE = os.environ.get("QUIET_HOURS_MODE", "low").strip().lower()  # "low" | "mute"
QUIET_PRIORITY = int(os.environ.get("QUIET_PRIORITY", "-1"))      # Pushover priority in "low" mode

# Health file: touched on connect and periodically while connected, so an
# external HEALTHCHECK can detect a wedged/stale connection and restart us.
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/tmp/bm_health").strip()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

USER_AGENT = "bm-friend-monitor/1.0 (+https://github.com/)"

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bm_monitor")


# --------------------------------------------------------------------------- #
# Watch-list parsing
# --------------------------------------------------------------------------- #
# Both env vars accept a comma-separated list. Each entry may optionally carry
# a friendly label using "value=Label", e.g.:
#   WATCH_DMR_IDS="2161234=John, 2625001=Anna"
#   WATCH_CALLSIGNS="VK3ABC=Dave, W1AW"

def _parse_watchlist(raw):
    """Return (values_set, labels_dict) from a comma/equals separated string."""
    values = set()
    labels = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            value, label = chunk.split("=", 1)
            value, label = value.strip(), label.strip()
        else:
            value, label = chunk, ""
        if value:
            values.add(value)
            if label:
                labels[value] = label
    return values, labels


def _parse_int_watchlist(raw, name):
    """Parse a comma list of integer IDs (with optional '=Label') into
    (set_of_ints, {int: label})."""
    values, labels = _parse_watchlist(raw)
    out, out_labels = set(), {}
    for v in values:
        try:
            iv = int(v)
        except ValueError:
            log.warning("Ignoring non-numeric value in %s: %r", name, v)
            continue
        out.add(iv)
        if labels.get(v):
            out_labels[iv] = labels[v]
    return out, out_labels


WATCH_IDS, ID_LABELS = _parse_int_watchlist(
    os.environ.get("WATCH_DMR_IDS", ""), "WATCH_DMR_IDS")
WATCH_TGS, TG_LABELS = _parse_int_watchlist(
    os.environ.get("WATCH_TALKGROUPS", ""), "WATCH_TALKGROUPS")
WATCH_RPTS, RPT_LABELS = _parse_int_watchlist(
    os.environ.get("WATCH_REPEATERS", ""), "WATCH_REPEATERS")

_call_values, _call_labels = _parse_watchlist(
    os.environ.get("WATCH_CALLSIGNS", "").upper())
WATCH_CALLS = {c.upper() for c in _call_values}
CALL_LABELS = {k.upper(): v for k, v in _call_labels.items()}

# Per-bucket timestamp of the last notification we sent (for MIN_SILENCE).
last_notify = {}


# --------------------------------------------------------------------------- #
# Dedup granularity
# --------------------------------------------------------------------------- #
# How wide a single anti-flood bucket is, per match category. A "scope" maps to
# the fields that form the dedup key:
#   station            -> one bucket per transmitting station
#   station_tg         -> one bucket per (station, talkgroup)   [default for people]
#   talkgroup          -> one bucket per talkgroup              [default for TG watch]
#   repeater           -> one bucket per repeater               [default for RPT watch]
#   station_repeater   -> one bucket per (station, repeater)
#
# Configure per category:
#   DEDUP_PERSON      (DMR ID / callsign matches)
#   DEDUP_TALKGROUP   (talkgroup matches)
#   DEDUP_REPEATER    (repeater matches)

_SCOPE_FIELDS = {
    "station": ("who",),
    "station_tg": ("who", "tg"),
    "talkgroup": ("tg",),
    "repeater": ("rpt",),
    "station_repeater": ("who", "rpt"),
}


def _scope(name, default):
    v = os.environ.get(name, default).strip().lower()
    if v not in _SCOPE_FIELDS:
        log.warning("Unknown %s=%r; using %r. Valid: %s",
                    name, v, default, ", ".join(sorted(_SCOPE_FIELDS)))
        return default
    return v


DEDUP_PERSON = _scope("DEDUP_PERSON", "station_tg")
DEDUP_TALKGROUP = _scope("DEDUP_TALKGROUP", "talkgroup")
DEDUP_REPEATER = _scope("DEDUP_REPEATER", "repeater")

_DEDUP_SCOPE_BY_KIND = {
    "person": DEDUP_PERSON,
    "talkgroup": DEDUP_TALKGROUP,
    "repeater": DEDUP_REPEATER,
}


def build_dedup_key(match_kind, who, dst_id, ctx_id):
    """Build the anti-flood bucket key for a matched event, honoring the
    configured dedup scope for its match category."""
    scope = _DEDUP_SCOPE_BY_KIND[match_kind]
    fields = {"who": who, "tg": f"tg:{dst_id}", "rpt": f"rpt:{ctx_id}"}
    return ":".join(fields[f] for f in _SCOPE_FIELDS[scope])


# --------------------------------------------------------------------------- #
# Quiet hours
# --------------------------------------------------------------------------- #

def _parse_quiet_hours(spec):
    """Parse 'HH:MM-HH:MM' into (start_minute, end_minute), or None if unset
    or malformed."""
    if not spec:
        return None
    try:
        start_s, end_s = spec.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
        start, end = sh * 60 + sm, eh * 60 + em
        if not (0 <= start < 1440 and 0 <= end < 1440):
            raise ValueError("time out of range")
        return (start, end)
    except (ValueError, AttributeError):
        log.warning("Ignoring malformed QUIET_HOURS %r (expected 'HH:MM-HH:MM')",
                    spec)
        return None


_quiet_range = _parse_quiet_hours(QUIET_HOURS)


def in_quiet_hours():
    """True if the current time in QUIET_TZ falls within the quiet window."""
    if not _quiet_range:
        return False
    try:
        tz = ZoneInfo(QUIET_TZ)
    except Exception:  # noqa: BLE001 - bad tz name -> fall back to UTC
        tz = timezone.utc
    nowt = datetime.now(tz)
    cur = nowt.hour * 60 + nowt.minute
    start, end = _quiet_range
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    # window wraps over midnight, e.g. 23:00-07:00
    return cur >= start or cur < end


# --------------------------------------------------------------------------- #
# Pushover
# --------------------------------------------------------------------------- #

def send_pushover(title, message, priority=None):
    if priority is None:
        priority = PUSHOVER_PRIORITY
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "message": message,
        "title": title,
        "priority": priority,
    }
    if PUSHOVER_DEVICE:
        data["device"] = PUSHOVER_DEVICE
    if PUSHOVER_SOUND:
        data["sound"] = PUSHOVER_SOUND

    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                "https://api.pushover.net/1/messages.json",
                data=data,
                headers=headers,
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Pushover network error (attempt %d/3): %s", attempt, exc)
            time.sleep(5)
            continue

        if resp.status_code == 200:
            log.info("Pushover sent: %s", title)
            return True

        if 400 <= resp.status_code < 500:
            # Bad input / quota / invalid token: retrying won't help.
            log.error("Pushover rejected request (%d): %s",
                      resp.status_code, resp.text.strip())
            return False

        # 5xx: server problem, back off and retry.
        log.warning("Pushover server error %d (attempt %d/3)",
                    resp.status_code, attempt)
        time.sleep(5)

    log.error("Pushover send failed after retries: %s", title)
    return False


# --------------------------------------------------------------------------- #
# Notification queue + worker
# --------------------------------------------------------------------------- #
# Sending happens on a dedicated thread so a slow or failing Pushover call can
# never block the Socket.IO event handler (which must stay responsive to keep
# the stream alive and answer keepalive pings).

notify_queue = queue.Queue(maxsize=1000)


def notification_worker():
    while True:
        title, message, priority = notify_queue.get()
        try:
            send_pushover(title, message, priority)
        except Exception as exc:  # noqa: BLE001 - worker must never die
            log.exception("Notification worker error: %s", exc)
        finally:
            notify_queue.task_done()


def enqueue_notification(title, message, priority=None):
    try:
        notify_queue.put_nowait((title, message, priority))
    except queue.Full:
        log.warning("Notification queue full; dropping: %s", title)


# --------------------------------------------------------------------------- #
# Health heartbeat
# --------------------------------------------------------------------------- #
# An external HEALTHCHECK watches the freshness of HEALTH_FILE. We refresh it
# on connect and every few seconds while the socket reports connected. If the
# connection wedges (half-open, no pings), sio.connected goes False, the file
# goes stale, and the container is marked unhealthy so it can be restarted.

def touch_health():
    try:
        with open(HEALTH_FILE, "w") as fh:
            fh.write(str(int(time.time())))
    except OSError as exc:
        log.debug("Could not write health file %s: %s", HEALTH_FILE, exc)


def health_heartbeat():
    while True:
        if sio.connected:
            touch_health()
        time.sleep(10)


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #

def construct_message(call):
    src_call = call.get("SourceCall") or "?"
    src_name = call.get("SourceName") or ""
    src_id = call.get("SourceID") or "?"
    dst_id = call.get("DestinationID") or "?"
    dst_name = call.get("DestinationName") or ""
    start = call.get("Start") or 0
    stop = call.get("Stop") or 0
    duration = (stop - start) if (stop and start) else 0

    ts = stop or start or int(time.time())
    when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    who = src_call
    if src_name:
        who += f" ({src_name})"

    tg = str(dst_id)
    if dst_name:
        tg += f" {dst_name}"

    msg = f"{who} [{src_id}] heard on TG {tg} at {when}"
    if duration > 0:
        msg += f" for {duration}s"
    return msg


# --------------------------------------------------------------------------- #
# Socket.IO client
# --------------------------------------------------------------------------- #

sio = socketio.Client(
    reconnection=True,
    reconnection_attempts=0,      # retry forever
    reconnection_delay=2,
    reconnection_delay_max=30,
    logger=False,
    engineio_logger=False,
)


def _join_tokens():
    """Build the list of subscription tokens to emit after connecting.

    The BrandMeister Last Heard server requires the client to 'join' before it
    sends any events. Tokens can be:
      - "everything"   : all traffic on the network
      - "src_<id>"     : only this source DMR ID
      - "dst_<id>"     : only this destination ID / talkgroup
      - "con_<id>"     : only this repeater ID

    We subscribe to each watched DMR ID / talkgroup / repeater individually for
    efficient server-side filtering. Callsigns can't be filtered server-side
    (the filter is by ID), so if any callsigns are watched we must receive
    everything and match locally.
    """
    if WATCH_CALLS:
        return ["everything"]
    tokens = [f"src_{i}" for i in sorted(WATCH_IDS)]
    tokens += [f"dst_{i}" for i in sorted(WATCH_TGS)]
    tokens += [f"con_{i}" for i in sorted(WATCH_RPTS)]
    return tokens or ["everything"]


@sio.event
def connect():
    touch_health()
    tokens = _join_tokens()
    for token in tokens:
        sio.emit("join", token)
    log.info("Connected to BrandMeister Last Heard stream. Subscribed: %s",
             ", ".join(tokens))


@sio.event
def connect_error(data):
    log.warning("Connection error: %s", data)


@sio.event
def disconnect():
    log.warning("Disconnected from BrandMeister stream (will auto-reconnect).")


@sio.on("mqtt")
def on_mqtt(data):
    """Handle one Last Heard event."""
    try:
        payload = data.get("payload")
        if payload is None:
            return
        call = json.loads(payload) if isinstance(payload, str) else payload
    except (ValueError, AttributeError, TypeError) as exc:
        log.debug("Could not parse event payload: %s", exc)
        return

    event = call.get("Event", "")
    if NOTIFY_ON and event != NOTIFY_ON:
        return

    # Drop stale/replayed events. A live Session-Stop arrives within seconds of
    # the transmission ending; a replayed Last Heard entry is minutes old.
    stop_ts = call.get("Stop") or 0
    if MAX_EVENT_AGE and stop_ts:
        try:
            age = int(time.time()) - int(stop_ts)
        except (ValueError, TypeError):
            age = 0
        if age > MAX_EVENT_AGE:
            log.debug("Ignoring stale event (age %ds > %ds): %s on TG %s",
                      age, MAX_EVENT_AGE, call.get("SourceCall"),
                      call.get("DestinationID"))
            return

    # Normalize SourceID to an int: the feed may deliver it as a number or as
    # a numeric string, and WATCH_IDS holds ints.
    src_id_raw = call.get("SourceID")
    try:
        src_id = int(str(src_id_raw).strip()) if src_id_raw not in (None, "") else None
    except (ValueError, TypeError):
        src_id = None
    src_call = (call.get("SourceCall") or "").upper().strip()

    dst_id = call.get("DestinationID")
    try:
        dst_id = int(dst_id) if dst_id not in (None, "") else None
    except (ValueError, TypeError):
        dst_id = None

    ctx_id = call.get("ContextID")
    try:
        ctx_id = int(ctx_id) if ctx_id not in (None, "") else None
    except (ValueError, TypeError):
        ctx_id = None

    # Match against the watch lists. Priority: DMR ID, callsign, talkgroup, repeater.
    matched = False
    label = ""
    match_kind = None
    if src_id is not None and src_id in WATCH_IDS:
        matched, label, match_kind = True, ID_LABELS.get(src_id, ""), "person"
    elif src_call and src_call in WATCH_CALLS:
        matched, label, match_kind = True, CALL_LABELS.get(src_call, ""), "person"
    elif dst_id is not None and dst_id in WATCH_TGS:
        matched, label, match_kind = True, TG_LABELS.get(dst_id, ""), "talkgroup"
    elif ctx_id is not None and ctx_id in WATCH_RPTS:
        matched, label, match_kind = True, RPT_LABELS.get(ctx_id, ""), "repeater"

    if not matched:
        return

    # Optional minimum-duration filter (ignore kerchunks).
    start = call.get("Start") or 0
    stop = call.get("Stop") or 0
    duration = (stop - start) if (stop and start) else 0
    if MIN_DURATION and 0 < duration < MIN_DURATION:
        log.debug("Ignoring short transmission (%ds) from %s", duration, src_call)
        return

    # Anti-flood debounce, keyed per the configured scope for this match kind.
    who = f"id:{src_id}" if src_id is not None else f"call:{src_call}"
    dedup_key = build_dedup_key(match_kind, who, dst_id, ctx_id)
    now = int(time.time())
    last = last_notify.get(dedup_key, 0)
    delta = now - last
    if delta < MIN_SILENCE:
        log.debug("SUPPRESS %s: %ds since last (< MIN_SILENCE %ds)",
                  dedup_key, delta, MIN_SILENCE)
        return

    # Quiet hours: mute entirely, or downgrade to low priority.
    quiet = in_quiet_hours()
    if quiet and QUIET_MODE == "mute":
        log.info("QUIET-MUTE %s: %s", dedup_key, src_call or src_id)
        return

    last_notify[dedup_key] = now
    priority = QUIET_PRIORITY if quiet else None

    title = label or src_call or f"DMR {src_id}"
    body = construct_message(call)
    log.info("MATCH %s%s: %s", dedup_key, " [quiet/low]" if quiet else "", body)
    enqueue_notification(f"{title} on air", body, priority)


# --------------------------------------------------------------------------- #
# Startup / shutdown
# --------------------------------------------------------------------------- #

def validate_config():
    problems = []
    if not PUSHOVER_TOKEN:
        problems.append("PUSHOVER_TOKEN is not set")
    if not PUSHOVER_USER:
        problems.append("PUSHOVER_USER is not set")
    if not (WATCH_IDS or WATCH_CALLS or WATCH_TGS or WATCH_RPTS):
        problems.append("No watch targets: set at least one of WATCH_DMR_IDS, "
                        "WATCH_CALLSIGNS, WATCH_TALKGROUPS, WATCH_REPEATERS")
    if problems:
        for p in problems:
            log.error("Config error: %s", p)
        sys.exit(1)


def handle_signal(signum, _frame):
    log.info("Received signal %d, shutting down.", signum)
    try:
        sio.disconnect()
    finally:
        sys.exit(0)


def main():
    validate_config()
    log.info("Watching %d ID(s), %d callsign(s), %d talkgroup(s), %d repeater(s). "
             "min_silence=%ds, min_duration=%ds, max_event_age=%ds, notify_on=%s",
             len(WATCH_IDS), len(WATCH_CALLS), len(WATCH_TGS), len(WATCH_RPTS),
             MIN_SILENCE, MIN_DURATION, MAX_EVENT_AGE, NOTIFY_ON)
    if WATCH_IDS:
        log.info("DMR IDs: %s", ", ".join(str(i) for i in sorted(WATCH_IDS)))
    if WATCH_CALLS:
        log.info("Callsigns: %s", ", ".join(sorted(WATCH_CALLS)))
    if WATCH_TGS:
        log.info("Talkgroups: %s", ", ".join(str(i) for i in sorted(WATCH_TGS)))
    if WATCH_RPTS:
        log.info("Repeaters: %s", ", ".join(str(i) for i in sorted(WATCH_RPTS)))
    if _quiet_range:
        log.info("Quiet hours: %s %s (mode=%s)", QUIET_HOURS, QUIET_TZ, QUIET_MODE)
    log.info("Dedup scopes: person=%s talkgroup=%s repeater=%s",
             DEDUP_PERSON, DEDUP_TALKGROUP, DEDUP_REPEATER)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    touch_health()
    threading.Thread(target=notification_worker, daemon=True).start()
    threading.Thread(target=health_heartbeat, daemon=True).start()

    while True:
        try:
            sio.connect(
                url=BM_URL,
                socketio_path=BM_SOCKETIO_PATH,
                transports=["websocket"],
            )
            sio.wait()
        except socketio.exceptions.ConnectionError as exc:
            log.warning("Initial connection failed: %s. Retrying in 10s.", exc)
            time.sleep(10)
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive
            log.exception("Unexpected error: %s. Restarting loop in 10s.", exc)
            time.sleep(10)


if __name__ == "__main__":
    main()
