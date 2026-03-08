FROM python:3.12-slim

LABEL org.opencontainers.image.title="Vueprom" \
      org.opencontainers.image.description="Emporia Vue energy monitoring Prometheus exporter" \
      org.opencontainers.image.source="https://github.com/petrovicboban/vueprom"

WORKDIR /app

COPY pyproject.toml README.md ./
COPY vueprom.py .
RUN pip install --no-cache-dir .

EXPOSE 8080

ENTRYPOINT ["vueprom"]
CMD ["vueprom.json"]
