FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 guardian \
    && useradd --uid 10001 --gid guardian --no-create-home --shell /usr/sbin/nologin guardian \
    && mkdir -p /data \
    && chown guardian:guardian /data

USER guardian
VOLUME ["/data"]

CMD ["python", "-m", "nexa_guardian"]
