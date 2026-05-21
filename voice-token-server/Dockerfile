FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY session_server.py .

EXPOSE 8088

# Single worker is plenty — this only mints short-lived client secrets.
CMD ["gunicorn", "-b", "0.0.0.0:8088", "--workers", "1", "--timeout", "30", "session_server:app"]
