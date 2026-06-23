FROM python:3.12-slim

# Don't write .pyc files; flush logs immediately (good for container logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALTH_FILE=/tmp/bm_health \
    HEALTH_MAX_AGE=90

# Create the unprivileged runtime user first, so we can hand it ownership
# of the app files during COPY.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app owned by appuser and force readable permissions, so it works
# regardless of the file's mode on the build host.
COPY --chown=appuser:appuser bm_monitor.py healthcheck.py ./
RUN chmod 0644 /app/bm_monitor.py /app/healthcheck.py

USER appuser

# Mark the container unhealthy if the health file goes stale (the app refreshes
# it every ~10s while connected). Combine with `--health-on-failure=kill` and a
# restart policy to auto-recover a wedged connection.
#
# NOTE: Podman only honors a baked-in HEALTHCHECK when the image is built in
# Docker format. Build with:  podman build --format docker -t <tag> .
# (Plain `podman build` defaults to OCI format and silently ignores this.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

CMD ["python", "bm_monitor.py"]
