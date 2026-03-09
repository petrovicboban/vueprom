"""
Airthings air-quality Prometheus exporter.

Discovers all Airthings devices via the Airthings Consumer API and exposes
radon, CO₂, VOC, temperature, humidity, pressure, PM1, PM2.5, light,
sound, battery, and RSSI readings as Prometheus gauges.

Authentication uses the OAuth2 client-credentials flow:
  - Token endpoint: https://accounts-api.airthings.com/v1/token
  - API base:       https://consumer-api.airthings.com/v1

API flow (per the OpenAPI spec):
  1. GET /v1/accounts                          → resolve accountId(s)
  2. GET /v1/accounts/{accountId}/devices      → device metadata (name, type, home)
  3. GET /v1/accounts/{accountId}/sensors      → paginated bulk sensor readings
"""

import json
import logging
import sys
import threading
import time
from typing import Any

import requests
from prometheus_client import Gauge, start_http_server

logger = logging.getLogger('airthingsprom')

AIRTHINGS_TOKEN_URL = 'https://accounts-api.airthings.com/v1/token'
AIRTHINGS_API_BASE = 'https://consumer-api.airthings.com/v1'
AIRTHINGS_SCOPE = 'read:device:current_values'

# ---------------------------------------------------------------------------
# Prometheus metrics – one gauge per sensor type.
# Labels: device (serialNumber), device_name, device_type, location (home)
# ---------------------------------------------------------------------------
_LABELS = ['device', 'device_name', 'device_type', 'location']

airthings_radon_pci_l = Gauge(
    'airthings_radon_pci_l',
    'Short-term average radon level in pCi/L from Airthings sensor',
    _LABELS,
)
airthings_radon_longterm_pci_l = Gauge(
    'airthings_radon_longterm_pci_l',
    'Long-term average radon level in pCi/L from Airthings sensor',
    _LABELS,
)
airthings_co2_ppm = Gauge(
    'airthings_co2_ppm',
    'CO₂ concentration in ppm from Airthings sensor',
    _LABELS,
)
airthings_voc_ppb = Gauge(
    'airthings_voc_ppb',
    'VOC concentration in ppb from Airthings sensor',
    _LABELS,
)
airthings_humidity_percent = Gauge(
    'airthings_humidity_percent',
    'Relative humidity in percent from Airthings sensor',
    _LABELS,
)
airthings_temperature_fahrenheit = Gauge(
    'airthings_temperature_fahrenheit',
    'Temperature in degrees Fahrenheit from Airthings sensor',
    _LABELS,
)
airthings_pressure_hpa = Gauge(
    'airthings_pressure_hpa',
    'Atmospheric pressure in hPa from Airthings sensor',
    _LABELS,
)
airthings_pm1_ug_m3 = Gauge(
    'airthings_pm1_ug_m3',
    'Particulate matter PM1 in µg/m³ from Airthings sensor',
    _LABELS,
)
airthings_pm25_ug_m3 = Gauge(
    'airthings_pm25_ug_m3',
    'Particulate matter PM2.5 in µg/m³ from Airthings sensor',
    _LABELS,
)
airthings_light_lux = Gauge(
    'airthings_light_lux',
    'Ambient light level in lux from Airthings sensor',
    _LABELS,
)
airthings_sound_db = Gauge(
    'airthings_sound_db',
    'Sound level in dB from Airthings sensor',
    _LABELS,
)
airthings_battery_percent = Gauge(
    'airthings_battery_percent',
    'Battery level in percent from Airthings sensor',
    _LABELS,
)
airthings_rssi_db = Gauge(
    'airthings_rssi_db',
    'Wi-Fi/BLE signal strength in dB from Airthings sensor',
    _LABELS,
)
airthings_device_info = Gauge(
    'airthings_device_info',
    'Airthings device metadata; always 1 for every discovered device',
    _LABELS,
)

# Maps sensorType string returned by the API → Prometheus gauge.
# The API returns sensor readings in sensors[].sensorType as camelCase strings.
_SENSOR_TYPE_GAUGES: dict[str, Gauge] = {
    'radonShortTermAvg': airthings_radon_pci_l,
    'radonLongTermAvg': airthings_radon_longterm_pci_l,
    'co2': airthings_co2_ppm,
    'voc': airthings_voc_ppb,
    'humidity': airthings_humidity_percent,
    'temp': airthings_temperature_fahrenheit,
    'pressure': airthings_pressure_hpa,
    'pm1': airthings_pm1_ug_m3,
    'pm25': airthings_pm25_ug_m3,
    'light': airthings_light_lux,
    'soundPressureLevels': airthings_sound_db,
    'rssi': airthings_rssi_db,
}

# Conversion factors/functions applied to raw API values before storing.
# Radon: API delivers Bq/m³; convert to pCi/L  (1 Bq/m³ = 1/37 pCi/L ≈ 0.027027 pCi/L).
# Temperature: API delivers °C; convert to °F   (F = C × 9/5 + 32).
# Source: https://www.epa.gov/radon/radon-measurement-concepts
_BQ_M3_TO_PCI_L = 1.0 / 37.0  # exact: 1 pCi/L = 37 Bq/m³


def _convert(sensor_type: str, raw: float) -> float:
    """Apply unit conversion for sensor types that need it."""
    if sensor_type in ('radonShortTermAvg', 'radonLongTermAvg'):
        return raw * _BQ_M3_TO_PCI_L
    if sensor_type == 'temp':
        return raw * 9.0 / 5.0 + 32.0
    return raw


# All distinct gauge objects (for stale-series cleanup).
# airthings_battery_percent and airthings_device_info are not in _SENSOR_TYPE_GAUGES
# (they come from top-level API fields), so add them explicitly.
_ALL_GAUGES: list[Gauge] = list(
    {
        id(g): g
        for g in [*_SENSOR_TYPE_GAUGES.values(), airthings_battery_percent, airthings_device_info]
    }.values()
)

# Tracks label-tuples seen in the previous collection cycle
_known_labelsets: set[tuple[str, str, str, str]] = set()

# ---------------------------------------------------------------------------
# OAuth2 token management
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {}


def _fetch_token(client_id: str, client_secret: str) -> str:
    """Request a new OAuth2 access token using client credentials."""
    resp = requests.post(
        AIRTHINGS_TOKEN_URL,
        data={
            'grant_type': 'client_credentials',
            'client_id': client_id,
            'client_secret': client_secret,
            'scope': AIRTHINGS_SCOPE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    access_token: str = data['access_token']
    expires_in: int = int(data.get('expires_in', 3600))
    _token_cache['access_token'] = access_token
    _token_cache['expires_at'] = time.time() + expires_in - 60  # 1-min buffer
    logger.debug('Fetched new Airthings access token (expires in %ds)', expires_in)
    return access_token


def _get_token(client_id: str, client_secret: str) -> str:
    """Return a valid OAuth2 access token, refreshing if necessary."""
    if _token_cache.get('access_token') and time.time() < _token_cache.get('expires_at', 0):
        return str(_token_cache['access_token'])
    return _fetch_token(client_id, client_secret)


def _auth_headers(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def get_accounts(token: str) -> list[str]:
    """Return the list of account IDs the current user belongs to.

    Corresponds to GET /v1/accounts in the OpenAPI spec.
    """
    resp = requests.get(
        f'{AIRTHINGS_API_BASE}/accounts',
        headers=_auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    accounts: list[dict[str, Any]] = data.get('accounts', [])
    return [str(a['id']) for a in accounts if 'id' in a]


def get_devices(token: str, account_id: str) -> list[dict[str, Any]]:
    """Return device metadata for all devices in an account.

    Corresponds to GET /v1/accounts/{accountId}/devices.
    Each item has keys: serialNumber, name, type, home, sensors.
    """
    resp = requests.get(
        f'{AIRTHINGS_API_BASE}/accounts/{account_id}/devices',
        headers=_auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    result: list[dict[str, Any]] = data.get('devices', [])
    return result


def get_sensors(token: str, account_id: str) -> list[dict[str, Any]]:
    """Return all sensor readings for an account, handling pagination.

    Corresponds to GET /v1/accounts/{accountId}/sensors (metric units).
    Each item is a SensorsResponse:
      {serialNumber, sensors[{sensorType, value, unit}], recorded, batteryPercentage}
    """
    results: list[dict[str, Any]] = []
    page = 1
    while True:
        params: dict[str, str] = {'unit': 'metric', 'pageNumber': str(page)}
        resp = requests.get(
            f'{AIRTHINGS_API_BASE}/accounts/{account_id}/sensors',
            headers=_auth_headers(token),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        page_results: list[dict[str, Any]] = data.get('results', [])
        results.extend(page_results)
        if not data.get('hasNext', False):
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


def collect_metrics(client_id: str, client_secret: str) -> None:
    """Collect sensor readings from all Airthings devices across all accounts."""
    global _known_labelsets
    try:
        token = _get_token(client_id, client_secret)
        account_ids = get_accounts(token)
        if not account_ids:
            logger.warning('No Airthings accounts found for these credentials')
            return
        logger.debug('Found %d Airthings account(s)', len(account_ids))

        active: set[tuple[str, str, str, str]] = set()

        for account_id in account_ids:
            # Build a lookup from serialNumber → device metadata
            try:
                devices = get_devices(token, account_id)
            except Exception:
                logger.exception('Failed to fetch devices for account %s', account_id)
                continue

            device_info: dict[str, dict[str, str]] = {}
            for d in devices:
                sn = str(d.get('serialNumber', ''))
                if sn:
                    device_info[sn] = {
                        'name': str(d.get('name', sn)),
                        'type': str(d.get('type', '')),
                        'home': str(d.get('home', '')),
                    }
            logger.debug('Account %s: found %d device(s)', account_id, len(device_info))

            # Emit device-info metric for every known device so Grafana template
            # variables have a reliable, always-present label source.
            for sn, info in device_info.items():
                lv = {
                    'device': sn,
                    'device_name': info['name'],
                    'device_type': info['type'],
                    'location': info['home'],
                }
                airthings_device_info.labels(**lv).set(1)
                active.add((sn, info['name'], info['type'], info['home']))

            # Fetch paginated bulk sensor readings
            try:
                sensor_results = get_sensors(token, account_id)
            except Exception:
                logger.exception('Failed to fetch sensors for account %s', account_id)
                continue

            for item in sensor_results:
                serial = str(item.get('serialNumber', ''))
                if not serial:
                    continue

                info = device_info.get(serial, {})
                device_name = info.get('name', serial)
                device_type = info.get('type', '')
                location = info.get('home', '')

                label_values = {
                    'device': serial,
                    'device_name': device_name,
                    'device_type': device_type,
                    'location': location,
                }
                label_tuple = (serial, device_name, device_type, location)

                updated = False

                # Individual sensor readings from the sensors[] array
                for sensor in item.get('sensors', []):
                    sensor_type = str(sensor.get('sensorType', ''))
                    gauge = _SENSOR_TYPE_GAUGES.get(sensor_type)
                    if gauge is None:
                        logger.debug(
                            'Unknown sensorType %r for device %s – skipping',
                            sensor_type,
                            serial,
                        )
                        continue
                    raw = sensor.get('value')
                    if raw is None:
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        logger.debug(
                            'Non-numeric value for sensorType %s on device %s', sensor_type, serial
                        )
                        continue
                    gauge.labels(**label_values).set(_convert(sensor_type, value))
                    updated = True
                    logger.debug(
                        'device=%s name=%s sensorType=%s value=%s',
                        serial,
                        device_name,
                        sensor_type,
                        value,
                    )

                # Battery percentage is a top-level field in SensorsResponse
                battery = item.get('batteryPercentage')
                if battery is not None:
                    try:
                        airthings_battery_percent.labels(**label_values).set(float(battery))
                        updated = True
                        logger.debug('device=%s name=%s battery=%s%%', serial, device_name, battery)
                    except (TypeError, ValueError):
                        logger.debug('Non-numeric batteryPercentage for device %s', serial)

                if updated:
                    active.add(label_tuple)

        # Remove series for devices that disappeared since the last cycle
        stale = _known_labelsets - active
        for stale_tuple in stale:
            stale_serial, stale_name, stale_type, stale_loc = stale_tuple
            for gauge in _ALL_GAUGES:
                try:
                    gauge.remove(stale_serial, stale_name, stale_type, stale_loc)
                except Exception:
                    logger.debug(
                        'Could not remove stale labelset device=%s name=%s',
                        stale_serial,
                        stale_name,
                    )
        _known_labelsets = active

    except Exception:
        logger.exception('Failed to collect Airthings metrics')


# ---------------------------------------------------------------------------
# Entry-point helpers required by __main__.py
# ---------------------------------------------------------------------------


def run(config: dict[str, Any], port: int, interval: int, stop_event: threading.Event) -> None:
    """Run the Airthings exporter main loop."""
    start_http_server(port)
    logger.info('Prometheus metrics available at http://0.0.0.0:%d/metrics', port)

    client_id: str = config.get('client_id', '')
    client_secret: str = config.get('client_secret', '')
    if not client_id or not client_secret:
        logger.error('client_id and client_secret are required in the Airthings config')
        sys.exit(1)

    while not stop_event.is_set():
        collect_metrics(client_id, client_secret)
        stop_event.wait(timeout=interval)

    logger.info('Airthingsprom stopped.')


def load_config(path: str) -> dict[str, Any]:
    """Load and return the JSON configuration file."""
    try:
        with open(path) as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError) as e:
        logger.error('Failed to load config file %s: %s', path, e)
        sys.exit(1)
