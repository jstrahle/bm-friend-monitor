#!/usr/bin/env python3
"""Container healthcheck: exit 0 if the health file is fresh, 1 otherwise.

The monitor refreshes HEALTH_FILE every ~10s while connected. If it goes stale
(connection wedged, process spinning, etc.) this exits non-zero so the runtime
can mark the container unhealthy and restart it.
"""
import os
import sys
import time

path = os.environ.get("HEALTH_FILE", "/tmp/bm_health")
max_age = int(os.environ.get("HEALTH_MAX_AGE", "90"))

try:
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path)) < max_age
except OSError:
    fresh = False

sys.exit(0 if fresh else 1)
