FROM python:3.12-slim

LABEL org.opencontainers.image.title="Vueprom" \
      org.opencontainers.image.description="Emporia Vue energy monitoring Prometheus exporter" \
      org.opencontainers.image.source="https://github.com/petrovicboban/vueprom"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vueprom.py .

EXPOSE 8080

ENTRYPOINT ["python", "vueprom.py"]
CMD ["vueprom.json"]
