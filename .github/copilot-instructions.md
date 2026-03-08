# Copilot Instructions for Vueprom

## Project Overview

**Vueprom** is a Python Prometheus exporter for the [Emporia Vue](https://emporiaenergy.com) energy monitoring system. It periodically queries the Emporia Vue cloud API via the `pyemvue` library, converts kWh readings to watts, and exposes them on an HTTP `/metrics` endpoint for Prometheus to scrape.

The entire application logic lives in a single file: `vueprom.py`.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.9+ |
| API client | `pyemvue` |
| Metrics exposure | `prometheus_client` |
| Containerization | Docker / Docker Compose |
| Monitoring | Prometheus + Grafana |

---

## Repository Layout

```
vueprom.py              # Main application (single-file)
pyproject.toml          # Build config, dependency declarations, ruff & mypy settings
vueprom.json.sample     # Sample configuration file
Dockerfile              # Docker image for vueprom
docker-compose.yml      # Full stack: vueprom + Prometheus + Grafana
prometheus.yml          # Prometheus scrape config
grafana/                # Grafana provisioning (datasource + dashboard)
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

Dev dependencies are declared in `pyproject.toml` under `[project.optional-dependencies] dev` and include `ruff` (linter/formatter) and `mypy` (type-checker).

---

## Coding Conventions

- **Single-file application** – all application code belongs in `vueprom.py`. Do not split it into multiple modules unless specifically asked.
- **Python version** – target Python 3.9+; avoid syntax or stdlib features newer than 3.9.
- **Type annotations** – all functions must have full type annotations (`disallow_untyped_defs = true` in mypy config).
- **String quotes** – use single quotes for strings (enforced by `ruff format`).
- **Line length** – maximum 100 characters (enforced by `ruff`).
- **Imports** – use isort-compatible ordering (enforced by `ruff` rule set `I`).
- **Logging** – use the module-level `logger = logging.getLogger('vueprom')` instance; do not call `print()`.
- **Error handling** – catch specific exceptions where possible; use `logger.exception(...)` to preserve tracebacks in broad `except Exception` blocks.

---

## Linting, Formatting, and Type-checking

```bash
# Lint
ruff check vueprom.py

# Auto-fix lint issues
ruff check --fix vueprom.py

# Format
ruff format vueprom.py

# Type-check
mypy vueprom.py
```

All three tools are configured in `pyproject.toml` (`[tool.ruff]`, `[tool.ruff.lint]`, `[tool.ruff.format]`, `[tool.mypy]`). Do not change these configurations without good reason.

---

## Configuration

The application reads a JSON config file at runtime (see `vueprom.json.sample`). Key fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8080` | Metrics HTTP port |
| `updateIntervalSecs` | int | `60` | Poll interval (seconds) |
| `debug` | bool | `false` | Enable debug logging |
| `accounts` | list | – | List of Emporia account objects |
| `accounts[].name` | str | – | Friendly label for Prometheus metrics |
| `accounts[].email` | str | – | Emporia account email |
| `accounts[].password` | str | – | Emporia account password |

> **Security:** `vueprom.json` contains credentials in plain text and must never be committed (it is listed in `.gitignore`).

---

## Prometheus Metrics

A single gauge is exported:

```
energy_usage_watts{account="...", device="...", channel="..."}
```

Stale label-sets (devices/channels that disappear between scrapes) are removed using `energy_usage_watts.remove(...)` so Prometheus does not keep serving outdated data.

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
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

Set the Grafana admin password via a `.env` file (`GF_SECURITY_ADMIN_PASSWORD=...`). This file is `.gitignore`d.

---

## Testing

There is currently no automated test suite. When adding tests, use `pytest` and place test files alongside `vueprom.py` (e.g., `test_vueprom.py`). Add `pytest` to the `dev` extras in `pyproject.toml`.

---

## Key Patterns

- **Recursive metric collection** – `update_metrics_recursive()` walks a nested device/channel tree returned by `pyemvue` and updates the Prometheus gauge for each leaf channel.
- **Re-login on error** – if `collect_usage()` raises an exception, the `vue` client is removed from the account dict so the next collection attempt triggers a fresh login.
- **Interruptible sleep** – `threading.Event.wait(timeout=interval)` is used instead of `time.sleep()` so the process shuts down immediately on SIGINT/SIGTERM.
