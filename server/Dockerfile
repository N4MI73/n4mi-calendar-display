# N4MI Calendar Display -- server Dockerfile
# Built by GitHub Actions and published as ghcr.io/n4mi73/n4mi-calendar-display
# (see .github/workflows/build-image.yml). Portainer on n4misrv only pulls it.

FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an ordinary user (uid 1000), not root. The data folder on the host
# must be owned by uid 1000 -- see server/README.md.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin calendar \
    && mkdir -p /data && chown 1000:1000 /data
USER 1000

ENV PYTHONUNBUFFERED=1 \
    CAL_DB_PATH=/data/calendar.db \
    TZ=America/New_York

EXPOSE 8087

# Lets Portainer show the container as healthy / unhealthy.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8087/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8087"]
