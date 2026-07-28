FROM python:3.13-alpine

ARG VERSION=1.0.0

LABEL org.opencontainers.image.title="IOmeter Prometheus-Exporter" \
      org.opencontainers.image.description="Exportiert die lokale IOmeter-HTTP-API als Prometheus-Metriken" \
      org.opencontainers.image.source="https://github.com/DasCanard/iometer-dashboard" \
      org.opencontainers.image.url="https://github.com/DasCanard/iometer-dashboard" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXPORTER_VERSION="${VERSION}"

WORKDIR /app

COPY app.py /app/app.py

USER 65532:65532

EXPOSE 9786

ENTRYPOINT ["python", "/app/app.py"]
