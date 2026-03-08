# Copilot Instructions for promexporters

## Project Overview

**promexporters** is a collection of Python Prometheus exporters for home monitoring devices. It ships two exporters:

| Exporter | Device | Metrics |
|----------|--------|---------|
| `vue` | [Emporia Vue](https://emporiaenergy.com) energy monitors | Per-channel energy usage in watts |
| `govee` | [Govee](https://govee.com) temperature/humidity sensors | Temperature (°C), humidity (%), battery (%) |

Both exporters share a single CLI entry point (`promexporters`) and are selected via the `--exporter` flag.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.9+ |
| Vue API client | `pyemvue` |
| Govee API client | `requests` |
| Metrics exposure | `prometheus_client` |
| Containerization | Docker / Docker Compose |
| Monitoring | Prometheus + Grafana |

---

## Repository Layout

```
promexporters/              # Main package
  __init__.py               # Package version
  __main__.py               # Shared CLI entry point (--exporter vue|govee)
  vue.py                    # Emporia Vue exporter logic
  govee.py                  # Govee temperature/humidity exporter logic
pyproject.toml              # Build config, dependency declarations, ruff & mypy settings
vueprom.json.sample         # Sample config for the Vue exporter
govee.json.sample           # Sample config for the Govee exporter
Dockerfile                  # Docker image for the Vue exporter
Dockerfile.govee            # Docker image for the Govee exporter
docker-compose.yml          # Full stack: both exporters + Prometheus + Grafana
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

- **Package structure** – all application code belongs under `promexporters/`. Each exporter lives in its own module (`vue.py`, `govee.py`). The shared entry point is `__main__.py`.
- **Module interface** – every exporter module must expose two functions: `load_config(path: str) -> dict[str, Any]` and `run(config, port, interval, stop_event)`.
- **Python version** – target Python 3.9+; avoid syntax or stdlib features newer than 3.9.
- **Type annotations** – all functions must have full type annotations (`disallow_untyped_defs = true` in mypy config).
- **String quotes** – use single quotes for strings (enforced by `ruff format`).
- **Line length** – maximum 100 characters (enforced by `ruff`).
- **Imports** – use isort-compatible ordering (enforced by `ruff` rule set `I`).
- **Logging** – use a module-level `logger = logging.getLogger('<name>')` instance; do not call `print()`. Vue uses `'vueprom'`, Govee uses `'goveeprom'`, the entry point uses `'promexporters'`.
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

> **Security:** `vueprom.json` and `govee.json` contain credentials in plain text and must never be committed (both are listed in `.gitignore`).

---

## Prometheus Metrics

### Vue exporter

```
energy_usage_watts{account="...", device="...", channel="..."}
```

Stale label-sets (devices/channels that disappear between scrapes) are removed using `energy_usage_watts.remove(...)` so Prometheus does not keep serving outdated data.

### Govee exporter

```
govee_temperature_celsius{device="...", device_name="...", sku="..."}
govee_humidity_percent{device="...", device_name="...", sku="..."}
govee_battery_percent{device="...", device_name="...", sku="..."}
```

Devices are auto-discovered via the Govee Router API; all sensors reporting temperature or humidity are included automatically.

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
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

Set the Grafana admin password via a `.env` file (`GF_SECURITY_ADMIN_PASSWORD=...`). This file is `.gitignore`d.

The Vue exporter uses `Dockerfile` and the Govee exporter uses `Dockerfile.govee`.

---

## Testing

There is currently no automated test suite. When adding tests, use `pytest` and place test files alongside the package (e.g., `test_vue.py`, `test_govee.py`). Add `pytest` to the `dev` extras in `pyproject.toml`.

---

## Key Patterns

- **Shared entry point** – `promexporters/__main__.py` dynamically imports the requested exporter module by name and calls its `load_config()` and `run()` functions. Adding a new exporter only requires creating a new module with those two functions and registering the name as a `choices` value in the `--exporter` argument.
- **Recursive metric collection (Vue)** – `update_metrics_recursive()` in `vue.py` walks a nested device/channel tree returned by `pyemvue` and updates the Prometheus gauge for each leaf channel.
- **Re-login on error (Vue)** – if `collect_usage()` raises an exception, the `vue` client is removed from the account dict so the next collection attempt triggers a fresh login.
- **Govee precision scaling** – raw sensor integer values are divided by a `precision` factor declared in the device capability parameters (default 100). See `_precision_for()` in `govee.py`.
- **Interruptible sleep** – `threading.Event.wait(timeout=interval)` is used instead of `time.sleep()` so the process shuts down immediately on SIGINT/SIGTERM.
