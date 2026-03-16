FROM python:3.14.3-slim-bookworm

WORKDIR /app
COPY services/tailscale_status/server.py /app/server.py

RUN useradd --system --create-home --shell /usr/sbin/nologin tailscale && \
    chown -R tailscale:tailscale /app

USER tailscale

ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
