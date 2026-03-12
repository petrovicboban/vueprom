# Copilot Instructions for promexporters

## Project Overview

**promexporters** is a collection of Python Prometheus exporters for home monitoring devices. It ships four exporters:

| Exporter | Device | Metrics |
|----------|--------|---------|
| `vue` | [Emporia Vue](https://emporiaenergy.com) energy monitors | Per-channel energy usage in watts |
| `govee` | [Govee](https://govee.com) temperature/humidity sensors | Temperature (°F), humidity (%), battery (%) |
| `airthings` | [Airthings](https://www.airthings.com) air-quality monitors | Radon, CO₂, VOC, temperature, humidity, pressure, PM1, PM2.5, light, sound, battery |
| `ecobee` | [Ecobee](https://www.ecobee.com) smart thermostats (via [beestat](https://beestat.io)) | Indoor/outdoor temperature (°F), humidity, setpoints, HVAC mode, equipment status, remote sensor readings |

All exporters share a single CLI entry point (`promexporters`) and are selected via the `--exporter` flag.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.9+ |
| Vue API client | `pyemvue` |
| Govee / Airthings / Ecobee API client | `requests` |
| Metrics exposure | `prometheus_client` |
| Containerization | Docker / Docker Compose |
| Monitoring | Prometheus + Grafana |

---

## Repository Layout

```
promexporters/              # Main package
  __init__.py               # Package version
  __main__.py               # Shared CLI entry point (--exporter vue|govee|airthings|ecobee)
  vue.py                    # Emporia Vue exporter logic
  govee.py                  # Govee temperature/humidity exporter logic
  airthings.py              # Airthings air-quality exporter logic
  ecobee.py                 # Ecobee thermostat exporter logic (via beestat API)
pyproject.toml              # Build config, dependency declarations, ruff & mypy settings
vueprom.json.sample         # Sample config for the Vue exporter
govee.json.sample           # Sample config for the Govee exporter
airthings.json.sample       # Sample config for the Airthings exporter
ecobee.json.sample          # Sample config for the Ecobee exporter
Dockerfile                  # Docker image for the Vue exporter
Dockerfile.govee            # Docker image for the Govee exporter
Dockerfile.airthings        # Docker image for the Airthings exporter
Dockerfile.ecobee           # Docker image for the Ecobee exporter
docker-compose.yml          # Full stack: all four exporters + Prometheus + Grafana
prometheus.yml              # Prometheus scrape config
grafana/                    # Grafana provisioning (datasource + dashboards)
README.md
```

---

## Development Setup

```bash
# Clone and install in editable mode with dev tools
git clone https://github.com/petrovicboban/vueprom.git
cd vueprom
pip install -e ".[dev]"
```

Dev dependencies are declared in `pyproject.toml` under `[project.optional-dependencies] dev` and include `ruff` (linter/formatter), `mypy` (type-checker), and `types-requests`.

---

## Coding Conventions

- **Package structure** – all application code belongs under `promexporters/`. Each exporter lives in its own module (`vue.py`, `govee.py`, `airthings.py`, `ecobee.py`). The shared entry point is `__main__.py`.
- **Module interface** – every exporter module must expose two functions: `load_config(path: str) -> dict[str, Any]` and `run(config, port, interval, stop_event)`.
- **Python version** – target Python 3.9+; avoid syntax or stdlib features newer than 3.9.
- **Type annotations** – all functions must have full type annotations (`disallow_untyped_defs = true` in mypy config).
- **String quotes** – use single quotes for strings (enforced by `ruff format`).
- **Line length** – maximum 100 characters (enforced by `ruff`).
- **Imports** – use isort-compatible ordering (enforced by `ruff` rule set `I`).
- **Logging** – use a module-level `logger = logging.getLogger('<name>')` instance; do not call `print()`. Vue uses `'vueprom'`, Govee uses `'goveeprom'`, Airthings uses `'airthingsprom'`, Ecobee uses `'ecobeeprom'`, the entry point uses `'promexporters'`.
- **Error handling** – catch specific exceptions where possible; use `logger.exception(...)` to preserve tracebacks in broad `except Exception` blocks.

---

## Linting, Formatting, and Type-checking

```bash
# Lint
ruff check promexporters/

# Auto-fix lint issues
ruff check --fix promexporters/

# Format
ruff format promexporters/

# Type-check
mypy promexporters/
```

All three tools are configured in `pyproject.toml` (`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.format]`, `[tool.mypy]`). Do not change these configurations without good reason.

---

## Configuration

### Vue exporter (`vueprom.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8080` | Metrics HTTP port |
| `updateIntervalSecs` | int | `60` | Poll interval (seconds) |
| `debug` | bool | `false` | Enable debug logging |
| `accounts` | list | – | List of Emporia account objects |
| `accounts[].name` | str | – | Friendly label for Prometheus metrics |
| `accounts[].email` | str | – | Emporia account email |
| `accounts[].password` | str | – | Emporia account password |

### Govee exporter (`govee.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8081` | Metrics HTTP port |
| `updateIntervalSecs` | int | `60` | Poll interval (seconds) |
| `debug` | bool | `false` | Enable debug logging |
| `api_key` | str | – | Govee API key |

### Airthings exporter (`airthings.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8082` | Metrics HTTP port |
| `updateIntervalSecs` | int | `60` | Poll interval (seconds) |
| `debug` | bool | `false` | Enable debug logging |
| `client_id` | str | – | Airthings API client ID |
| `client_secret` | str | – | Airthings API client secret |

### Ecobee exporter (`ecobee.json`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8083` | Metrics HTTP port |
| `updateIntervalSecs` | int | `180` | Poll interval (seconds); minimum useful value is 180 s (beestat sync cadence) |
| `debug` | bool | `false` | Enable debug logging |
| `api_key` | str | – | beestat API key |

> **Security:** Config files contain credentials in plain text and must never be committed (all are listed in `.gitignore`).

---

## Prometheus Metrics

### Vue exporter

```
energy_usage_watts{account="...", device="...", channel="..."}
```

Stale label-sets (devices/channels that disappear between scrapes) are removed using `energy_usage_watts.remove(...)` so Prometheus does not keep serving outdated data.

### Govee exporter

```
govee_temperature_fahrenheit{device="...", device_name="...", sku="..."}
govee_humidity_percent{device="...", device_name="...", sku="..."}
govee_battery_percent{device="...", device_name="...", sku="..."}
```

Devices are auto-discovered via the Govee Router API; all sensors reporting temperature or humidity are included automatically. Temperature is always stored in Fahrenheit; devices reporting Celsius are converted (×9/5+32) before the metric is set.

### Airthings exporter

```
airthings_radon_pci_l{device="...", device_name="...", device_type="...", location="..."}
airthings_radon_longterm_pci_l{...}
airthings_co2_ppm{...}
airthings_voc_ppb{...}
airthings_temperature_fahrenheit{...}
airthings_humidity_percent{...}
airthings_pressure_hpa{...}
airthings_pm1_ug_m3{...}
airthings_pm25_ug_m3{...}
airthings_light_lux{...}
airthings_sound_db{...}
airthings_battery_percent{...}
airthings_rssi_db{...}
airthings_device_info{...}   # always 1; used as anchor for Grafana template variables
```

Devices are auto-discovered via the Airthings Consumer API. Radon is stored in pCi/L (converted from Bq/m³ via ×1/37). Temperature is stored in °F (converted from °C). The `airthings_device_info` gauge is always emitted for every device regardless of which sensors it has.

### Ecobee exporter

```
ecobee_thermostat_info{thermostat="...", thermostat_name="..."}         # always 1
ecobee_temperature_fahrenheit{thermostat="...", thermostat_name="..."}
ecobee_humidity_percent{...}
ecobee_heat_setpoint_fahrenheit{...}
ecobee_cool_setpoint_fahrenheit{...}
ecobee_outdoor_temperature_fahrenheit{...}
ecobee_outdoor_humidity_percent{...}
ecobee_hvac_mode{thermostat="...", thermostat_name="...", mode="..."}   # 1 = active, 0 = others
ecobee_equipment_running{thermostat="...", thermostat_name="...", equipment="..."}
ecobee_sensor_temperature_fahrenheit{thermostat="...", thermostat_name="...", sensor="...", sensor_name="..."}
ecobee_sensor_humidity_percent{...}
ecobee_sensor_occupancy{...}
```

Thermostats are auto-discovered via the beestat `read_id` method, which returns a **dict keyed by beestat record ID** (not a list). `get_thermostats()` calls `.values()` to extract the thermostat objects. The Ecobee thermostat body sensor is included in `remote_sensors`. All temperature values (including Ecobee's tenths-of-°F integers) are converted to floating-point °F.

---

## Docker / Docker Compose

```bash
# Build and run the full stack
docker compose up -d
```

Services:

| Service | Default URL |
|---------|------------|
| Vueprom (`/metrics`) | http://localhost:8080/metrics |
| Goveeprom (`/metrics`) | http://localhost:8081/metrics |
| Airthingsprom (`/metrics`) | http://localhost:8082/metrics |
| Ecobeeprom (`/metrics`) | http://localhost:8083/metrics |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

Set the Grafana admin password via a `.env` file (`GF_SECURITY_ADMIN_PASSWORD=...`). This file is `.gitignore`d.

Each exporter has its own Dockerfile: `Dockerfile` (vue), `Dockerfile.govee`, `Dockerfile.airthings`, `Dockerfile.ecobee`.

---

## Testing

There is currently no automated test suite. When adding tests, use `pytest` and place test files alongside the package (e.g., `test_vue.py`, `test_govee.py`). Add `pytest` to the `dev` extras in `pyproject.toml`.

---

## Key Patterns

- **Shared entry point** – `promexporters/__main__.py` dynamically imports the requested exporter module by name and calls its `load_config()` and `run()` functions. Adding a new exporter only requires creating a new module with those two functions and registering the name as a `choices` value in the `--exporter` argument.
- **Recursive metric collection (Vue)** – `update_metrics_recursive()` in `vue.py` walks a nested device/channel tree returned by `pyemvue` and updates the Prometheus gauge for each leaf channel.
- **Re-login on error (Vue)** – if `collect_usage()` raises an exception, the `vue` client is removed from the account dict so the next collection attempt triggers a fresh login.
- **Govee precision scaling** – raw sensor integer values are divided by a `precision` factor declared in the device capability parameters. When the precision field is absent the API returns values already in their final unit, so the default is `1.0` (no scaling). See `_precision_for()` in `govee.py`.
- **Airthings OAuth2 token caching** – `airthings.py` fetches a bearer token via the client-credentials flow (POST to `https://accounts-api.airthings.com/v1/token`) and caches it until expiry; the API base is `https://consumer-api.airthings.com/v1`.
- **Airthings device info anchor** – `airthings_device_info` (always 1) is emitted for every device independently of sensor readings, so Grafana template variables can use `label_values(airthings_device_info, device_name)` reliably.
- **Beestat read_id dict response** – the beestat `read_id` endpoint returns `{"data": {"<id>": {...}, ...}}` — a dict keyed by record ID, not a list. `get_thermostats()` in `ecobee.py` extracts `.values()` from the `data` dict to obtain the thermostat objects.
- **Ecobee temperature encoding** – Ecobee (and beestat) store temperatures as tenths of °F integers (e.g., 720 = 72.0°F). Divide by 10.0 before setting any gauge. The sentinel value 12300 means "sensor unavailable" and should be skipped.
- **Stale label-set cleanup** – all exporters track active label-sets between scrapes and call `gauge.remove(...)` for any that disappear, preventing Prometheus from serving stale data.
- **Interruptible sleep** – `threading.Event.wait(timeout=interval)` is used instead of `time.sleep()` so the process shuts down immediately on SIGINT/SIGTERM.
