# promexporters

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Overview

**promexporters** is a collection of [Prometheus](https://prometheus.io) exporters for home monitoring devices.

| Exporter | Device | Metrics |
|----------|--------|---------|
| `vue` | [Emporia Vue](https://emporiaenergy.com) energy monitors | Per-channel energy usage in watts |
| `govee` | [Govee](https://govee.com) temperature/humidity sensors | Temperature (°F), humidity (%), battery (%) |

Both exporters share a single command-line entry point (`promexporters`) and are differentiated by the `--exporter` flag.

This project is not affiliated with *Emporia Energy* or *Govee*.

---

## Features

### Emporia Vue exporter (`--exporter vue`)
- Exports per-channel energy usage as the `energy_usage_watts` Prometheus gauge
- Labels each metric with `account`, `device`, and `channel`
- Supports multiple Emporia Vue accounts
- Supports nested devices (sub-panels, smart plugs)
- Auto-discovers channel and device names from the Emporia API

### Govee exporter (`--exporter govee`)
- Auto-discovers all Govee temperature/humidity sensor devices via the Govee Router API
- Exports `govee_temperature_fahrenheit`, `govee_humidity_percent`, and `govee_battery_percent` gauges
- Labels each metric with `device`, `device_name`, and `sku`

### Common
- Configurable scrape port and collection interval
- Ready-to-use `docker-compose.yml` that brings up both exporters + Prometheus + Grafana
- Pre-built Grafana dashboards for both exporters

---

## Dependencies

- Python 3.9+ with pip, **or** Docker
- [Prometheus](https://prometheus.io) to scrape the metrics
- [Grafana](https://grafana.com) to visualise the data (optional but recommended)
- Emporia Vue account (email + password) for the Vue exporter
- [Govee API key](https://developer.govee.com/reference/apply-you-govee-api-key) for the Govee exporter

---

## Quick Start with Docker Compose

This is the easiest way to get the full stack (both exporters + Prometheus + Grafana) running.

### 1. Configure the Vue exporter

Copy the sample config and fill in your Emporia credentials:

```bash
cp vueprom.json.sample vueprom.json
```

Edit `vueprom.json`:

```json
{
    "port": 8080,
    "updateIntervalSecs": 60,
    "accounts": [
        {
            "name": "My Home",
            "email": "me@example.com",
            "password": "my-emporia-password"
        }
    ]
}
```

> **Important:** Keep `vueprom.json` private – it contains your Emporia credentials in plain text.

### 2. Configure the Govee exporter

Copy the sample config and fill in your Govee API key:

```bash
cp govee.json.sample govee.json
```

Edit `govee.json`:

```json
{
    "port": 8081,
    "updateIntervalSecs": 60,
    "api_key": "your-govee-api-key"
}
```

Apply for a Govee API key at: https://developer.govee.com/reference/apply-you-govee-api-key

> **Important:** Keep `govee.json` private – it contains your Govee API key.

### 3. Set the Grafana admin password

```bash
echo "GF_SECURITY_ADMIN_PASSWORD=changeme" > .env
```

### 4. Start the stack

```bash
docker compose up -d
```

| Service      | URL                           |
|--------------|-------------------------------|
| Vueprom      | http://localhost:8080/metrics |
| Goveeprom    | http://localhost:8081/metrics |
| Prometheus   | http://localhost:9090         |
| Grafana      | http://localhost:3000         |

### 5. View the Grafana dashboards

Open http://localhost:3000, log in with `admin` and the password you set in `.env`, then navigate to **Dashboards → Vueprom** to find:
- **Emporia Vue Energy Usage** – energy monitoring dashboard
- **Govee Temperature & Humidity** – temperature/humidity sensor dashboard

---

## Running without Docker

### Install

```bash
pip install .
```

### Run the Vue exporter

```bash
promexporters --exporter vue vueprom.json
```

### Run the Govee exporter

```bash
promexporters --exporter govee govee.json
```

Optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--exporter` | `vue` | Which exporter to run: `vue` or `govee` |
| `--port` | `8080` | Prometheus metrics port |
| `--interval` | `60` | Collection interval in seconds |
| `--debug` | off | Enable debug logging |

You can also run the package directly:

```bash
python -m promexporters --exporter govee govee.json
```

---

## Development

### Set up a dev environment

```bash
git clone https://github.com/petrovicboban/vueprom.git
cd vueprom
pip install -e ".[dev]"
```

This installs the package in editable mode together with the dev tools ([ruff](https://docs.astral.sh/ruff/) and [mypy](https://mypy.readthedocs.io)).

### Lint

```bash
ruff check promexporters/
```

### Format

```bash
ruff format promexporters/
```

### Type-check

```bash
mypy promexporters/
```

---

## Configuration Reference

### Vue exporter (`vueprom.json`)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `port` | No | `8080` | HTTP port for the `/metrics` endpoint |
| `updateIntervalSecs` | No | `60` | How often to poll the Emporia API |
| `debug` | No | `false` | Set to `true` to enable debug logging |
| `accounts` | Yes | – | List of Emporia Vue accounts |
| `accounts[].name` | Yes | – | Friendly name used as the `account` label |
| `accounts[].email` | Yes | – | Emporia account email |
| `accounts[].password` | Yes | – | Emporia account password |

> Channel and device names are resolved automatically from the Emporia API.

### Govee exporter (`govee.json`)

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `port` | No | `8081` | HTTP port for the `/metrics` endpoint |
| `updateIntervalSecs` | No | `60` | How often to poll the Govee API |
| `debug` | No | `false` | Set to `true` to enable debug logging |
| `api_key` | Yes | – | Govee API key |

> Devices are auto-discovered. All sensors that report temperature or humidity are included automatically.

---

## Prometheus Metrics

### Vue exporter

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `energy_usage_watts` | Gauge | `account`, `device`, `channel` | Current energy usage in watts |

#### Example PromQL queries

```promql
# Total home usage
sum(energy_usage_watts{channel!~"Balance|TotalUsage"})

# Usage per device
sum by (device) (energy_usage_watts{channel!~"Balance|TotalUsage"})

# Top 5 circuits right now
topk(5, energy_usage_watts{channel!~"Balance|TotalUsage"})
```

### Govee exporter

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `govee_temperature_fahrenheit` | Gauge | `device`, `device_name`, `sku` | Current temperature in Fahrenheit |
| `govee_humidity_percent` | Gauge | `device`, `device_name`, `sku` | Current relative humidity in percent |
| `govee_battery_percent` | Gauge | `device`, `device_name`, `sku` | Current battery level in percent |

#### Example PromQL queries

```promql
# Temperature of all sensors
govee_temperature_fahrenheit

# Average humidity across all sensors
avg(govee_humidity_percent)

# Sensors with low battery
govee_battery_percent < 20
```

---

## Grafana Dashboards

Two dashboards are automatically provisioned when using `docker-compose.yml`:

### Emporia Vue Energy Usage (`grafana/provisioning/dashboards/energy_dashboard.json`)
- **Total Usage by Device** – time-series graph of watts per device
- **Usage by Channel** – time-series graph of watts per circuit
- **Current Usage per Channel** – bar gauge showing live readings

### Govee Temperature & Humidity (`grafana/provisioning/dashboards/govee_dashboard.json`)
- **Temperature** – time-series graph per sensor
- **Humidity** – time-series graph per sensor
- **Current Temperature** – gauge panel with colour thresholds
- **Current Humidity** – gauge panel with colour thresholds
- **Battery Level** – bar gauge for all sensors

To import either dashboard manually into an existing Grafana instance, go to **Dashboards → Import** and upload the JSON file.

---

## Upgrading image versions

`docker-compose.yml` pins Prometheus and Grafana to known-good versions. To upgrade, edit the image tags:

```yaml
image: prom/prometheus:v3.2.1   # → replace with newer tag
image: grafana/grafana:11.5.2   # → replace with newer tag
```

Check the latest tags at [hub.docker.com/r/prom/prometheus/tags](https://hub.docker.com/r/prom/prometheus/tags) and [hub.docker.com/r/grafana/grafana/tags](https://hub.docker.com/r/grafana/grafana/tags).

---

## Architecture

```
Emporia Vue Cloud API          Govee Cloud API
        │                            │
        │  (pyemvue, every N secs)   │  (requests, every N secs)
        ▼                            ▼
  promexporters                promexporters
  --exporter vue               --exporter govee
  (vueprom)                    (goveeprom)
        │                            │
        │  HTTP /metrics             │  HTTP /metrics
        ▼                            ▼
              Prometheus  ──────────────────▶  Grafana
```

---

## License

MIT
