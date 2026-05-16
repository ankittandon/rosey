FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# flyctl lets the router shell out to provision new household VMs.
# Install via the official script — drops binary at /root/.fly/bin/flyctl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://fly.io/install.sh | sh
ENV PATH="/root/.fly/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Code lives at /app/router/ so the package-relative imports (`from . import db`)
# resolve correctly when gunicorn runs `router.app:app`.
RUN mkdir -p router router/scripts
COPY __init__.py app.py db.py members.py notifications.py provisioning.py schema.sql telegram_onboarding.py timezone.py household_template.fly.toml ./router/
COPY scripts/wipe_tenants.py scripts/migrate_all.py ./router/scripts/

EXPOSE 8081

# --workers 1 keeps SQLite single-writer; provisioning runs in background threads.
CMD ["gunicorn", "--bind", "[::]:8081", "--workers", "1", "--timeout", "120", "router.app:app"]
