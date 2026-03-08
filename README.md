# vueprom

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Overview

**Vueprom** is a [Prometheus](https://prometheus.io) exporter for the [Emporia Vue](https://emporiaenergy.com) energy monitoring system. It is inspired by [Vuegraf](https://github.com/jertel/vuegraf), but exports metrics to Prometheus/Grafana instead of InfluxDB.

Vueprom periodically queries the Emporia Vue cloud API, converts the energy readings to watts, and exposes them on an HTTP endpoint that Prometheus can scrape. A pre-built Grafana dashboard is included so you can visualise your energy usage immediately after deploying the stack.

This project is not affiliated with *Emporia Energy*.

---

## Features

- Exports per-channel energy usage as the `energy_usage_watts` Prometheus gauge
- Labels each metric with `account`, `device`, and `channel`
- Supports multiple Emporia Vue accounts
- Supports nested devices (sub-panels, smart plugs)
- Supports human-readable channel names via the config file
- Configurable scrape port and collection interval
- Ready-to-use `docker-compose.yml` that brings up Vueprom + Prometheus + Grafana

---

## Dependencies

- [Emporia Vue](https://emporiaenergy.com) account (email + password)
- [Python 3.9+](https://python.org) with pip, **or** Docker
- [Prometheus](https://prometheus.io) to scrape the metrics
- [Grafana](https://grafana.com) to visualise the data (optional but recommended)

---

## Quick Start with Docker Compose

This is the easiest way to get the full Vueprom + Prometheus + Grafana stack running.

### 1. Configure Vueprom

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

### 2. Set the Grafana admin password

Create a `.env` file (it is listed in `.gitignore` and will not be committed):

```bash
echo "GF_SECURITY_ADMIN_PASSWORD=changeme" > .env
```

### 3. Start the stack

```bash
docker compose up -d
```

| Service    | URL                        |
|------------|---------------------------|
| Vueprom    | http://localhost:8080/metrics |
| Prometheus | http://localhost:9090      |
| Grafana    | http://localhost:3000      |

### 3. View the Grafana dashboard

Open http://localhost:3000, log in with `admin` and the password you set in `.env`, then navigate to **Dashboards → Vueprom → Emporia Vue Energy Usage**.

---

## Running without Docker

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run

```bash
python vueprom.py vueprom.json
```

Optional arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--port` | `8080`  | Prometheus metrics port |
| `--interval` | `60` | Collection interval in seconds |

---

## Configuration Reference

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `port` | No | `8080` | HTTP port for the `/metrics` endpoint |
| `updateIntervalSecs` | No | `60` | How often to poll the Emporia API |
| `accounts` | Yes | – | List of Emporia Vue accounts |
| `accounts[].name` | Yes | – | Friendly name used as the `account` label |
| `accounts[].email` | Yes | – | Emporia account email |
| `accounts[].password` | Yes | – | Emporia account password |
| `accounts[].devices` | No | – | Optional list of devices for custom channel names |
| `accounts[].devices[].name` | Yes (if devices) | – | Device name (must match the name in the Emporia app) |
| `accounts[].devices[].channels` | No | – | List of channel names in circuit order |

### Custom channel names example

```json
"devices": [
    {
        "name": "Main Panel",
        "channels": [
            "Air Conditioner",
            "Furnace",
            "Washer",
            "Dryer",
            "Refrigerator",
            "Oven",
            "Dishwasher",
            "Office"
        ]
    }
]
```

---

## Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `energy_usage_watts` | Gauge | `account`, `device`, `channel` | Current energy usage in watts |

### Example PromQL queries

```promql
# Total home usage
sum(energy_usage_watts{channel!~"Balance|TotalUsage"})

# Usage per device
sum by (device) (energy_usage_watts{channel!~"Balance|TotalUsage"})

# Top 5 circuits right now
topk(5, energy_usage_watts{channel!~"Balance|TotalUsage"})
```

---

## Grafana Dashboard

The included dashboard (`grafana/provisioning/dashboards/energy_dashboard.json`) provides:

- **Total Usage by Device** – time-series graph of watts per device
- **Usage by Channel** – time-series graph of watts per circuit
- **Current Usage per Channel** – bar gauge showing live readings

The dashboard is automatically provisioned when using `docker-compose.yml`.

To import it manually into an existing Grafana instance, go to **Dashboards → Import** and upload the JSON file.

---

## Upgrading image versions

`docker-compose.yml` pins Prometheus and Grafana to known-good versions to keep the stack reproducible. To upgrade, edit the image tags in `docker-compose.yml`:

```yaml
image: prom/prometheus:v3.2.1   # → replace with newer tag
image: grafana/grafana:11.5.2   # → replace with newer tag
```

Check the latest tags at [hub.docker.com/r/prom/prometheus/tags](https://hub.docker.com/r/prom/prometheus/tags) and [hub.docker.com/r/grafana/grafana/tags](https://hub.docker.com/r/grafana/grafana/tags).

---

## Architecture

```
Emporia Vue Cloud API
        │
        │  (pyemvue, every N seconds)
        ▼
   vueprom.py
        │
        │  HTTP /metrics (prometheus_client)
        ▼
   Prometheus  ──────────────────▶  Grafana
```

---

## License

MIT