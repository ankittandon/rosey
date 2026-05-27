FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install Node.js 20 alongside Python — needed for the Baileys sidecar that
# handles WhatsApp groups (Cloud API can't reach groups without Official
# Business Account status, so we run the Baileys MultiDevice protocol
# client in parallel). curl is used by the official NodeSource setup.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first so Docker layer caching reuses them when only sources change.
COPY pyproject.toml ./
RUN pip install --no-cache-dir '.[telegram]'

# Node deps for the Baileys sidecar — separate copy step so changing
# JS source doesn't invalidate the npm-install layer.
COPY baileys/package.json ./baileys/
RUN cd baileys && npm install --omit=dev --no-audit --no-fund

# Python sources. Update this list when adding modules. Avoids COPY *
# which would also copy .env, .venv, scheduler.db, etc.
COPY agent.py alexa_handler.py app.py channels.py feed_format.py gate.py household.py \
     mcp_server.py memory_tool.py paths.py redact.py reminder_builder.py reminder_format.py \
     reminders.py roster.py scheduler.py server.py summary.py telegram_bot.py tools.py \
     transcribe.py whatsapp_handler.py ./

# Baileys source + the wrapper that launches both processes.
COPY baileys/index.js ./baileys/
COPY scripts/start.sh ./scripts/

EXPOSE 8080

# Wrapper script launches Python + Baileys side by side. When BAILEYS_MODE
# is "off" (default), only Python runs; we still ship the Node files in
# the image so flipping the env var enables groups without rebuilding.
CMD ["bash", "/app/scripts/start.sh"]
