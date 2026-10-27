FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    nginx-light \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY docker/nginx.conf.template /etc/nginx/ugreen-drive.conf.template
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Runs as root so seteuid can impersonate NAS users: every file operation
# happens as the signed-in NAS user, so UGOS permissions stay in charge.
ENV PYTHONUNBUFFERED=1 \
    WEB_PORT=8790
EXPOSE 8790

HEALTHCHECK --interval=20s --timeout=6s --retries=3 --start-period=25s \
    CMD curl -fsS --max-time 5 "http://127.0.0.1:${WEB_PORT}/api/health" || exit 1

LABEL org.opencontainers.image.source="https://github.com/neelsatyavolu/ugreen-drive" \
      org.opencontainers.image.description="A fast web file browser for Ugreen NAS (UGOS Pro)" \
      org.opencontainers.image.licenses="MIT"

CMD ["/entrypoint.sh"]
