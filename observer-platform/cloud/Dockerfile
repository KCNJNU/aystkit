ARG PYTHON_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 observer \
    && useradd --uid 10001 --gid observer --no-create-home observer \
    && mkdir /data && chown observer:observer /data
COPY cloud /app/cloud
COPY shared /app/shared
USER observer
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/readyz',timeout=3).read()"
CMD ["python", "-m", "cloud.backend.app"]
