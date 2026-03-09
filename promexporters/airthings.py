"""
Airthings air-quality Prometheus exporter.

Discovers all Airthings devices via the Airthings Consumer API and exposes
radon, CO₂, VOC, temperature, humidity, pressure, PM1, PM2.5, light,
sound, battery, and RSSI readings as Prometheus gauges.

Authentication uses the OAuth2 client-credentials flow:
  - Token endpoint: https://accounts-api.airthings.com/v1/token
  - API base:       https://consumer-api.airthings.com/v1
"""

import json
import logging
import sys
import threading
import time
from typing import Any, Optional

import requests
from prometheus_client import Gauge, start_http_server

logger = logging.getLogger('airthingsprom')

AIRTHINGS_TOKEN_URL = 'https://accounts-api.airthings.com/v1/token'
AIRTHINGS_API_BASE = 'https://consumer-api.airthings.com/v1'
AIRTHINGS_SCOPE = 'read:device:current_values'

# ---------------------------------------------------------------------------
# Prometheus metrics – one gauge per sensor field.
# Labels: device (serial), device_name, device_type, location
# ---------------------------------------------------------------------------
_LABELS = ['device', 'device_name', 'device_type', 'location']

airthings_radon_bq_m3 = Gauge(
    'airthings_radon_bq_m3',
    'Short-term average radon level in Bq/m³ from Airthings sensor',
    _LABELS,
)
airthings_radon_longterm_bq_m3 = Gauge(
    'airthings_radon_longterm_bq_m3',
    'Long-term average radon level in Bq/m³ from Airthings sensor',
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
airthings_temperature_celsius = Gauge(
    'airthings_temperature_celsius',
    'Temperature in degrees Celsius from Airthings sensor',
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

# Maps API field name → gauge
_FIELD_GAUGES: dict[str, Gauge] = {
    'radon': airthings_radon_bq_m3,
    'radonShortTermAvg': airthings_radon_bq_m3,
    'radonLongTermAvg': airthings_radon_longterm_bq_m3,
    'co2': airthings_co2_ppm,
    'voc': airthings_voc_ppb,
    'humidity': airthings_humidity_percent,
    'temp': airthings_temperature_celsius,
    'pressure': airthings_pressure_hpa,
    'pm1': airthings_pm1_ug_m3,
    'pm25': airthings_pm25_ug_m3,
    'light': airthings_light_lux,
    'sound': airthings_sound_db,
    'battery': airthings_battery_percent,
    'rssi': airthings_rssi_db,
}

# All distinct gauge objects (for stale-series cleanup)
_ALL_GAUGES: list[Gauge] = list({id(g): g for g in _FIELD_GAUGES.values()}.values())

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


def get_devices(token: str) -> list[dict[str, Any]]:
    """Return the list of all Airthings devices for this account."""
    resp = requests.get(
        f'{AIRTHINGS_API_BASE}/devices',
        headers=_auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    result: list[dict[str, Any]] = data.get('devices', [])
    return result


def get_latest_samples(token: str, serial_number: str) -> dict[str, Any]:
    """Return the latest sensor readings for a single device."""
    resp = requests.get(
        f'{AIRTHINGS_API_BASE}/devices/{serial_number}/latest-samples',
        headers=_auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    result: dict[str, Any] = data.get('data', {})
    return result


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


def _location_name(device: dict[str, Any]) -> str:
    """Extract a human-readable location name from a device dict."""
    loc: Optional[dict[str, Any]] = device.get('location')
    if isinstance(loc, dict):
        name: str = loc.get('name', '')
        if name:
            return name
    return ''


def collect_metrics(client_id: str, client_secret: str) -> None:
    """Collect sensor readings from all discovered Airthings devices."""
    global _known_labelsets
    try:
        token = _get_token(client_id, client_secret)
        devices = get_devices(token)
        logger.debug('Found %d Airthings device(s)', len(devices))

        active: set[tuple[str, str, str, str]] = set()

        for device in devices:
            serial = str(device.get('id', ''))
            device_name = str(device.get('productName', serial))
            device_type = str(device.get('deviceType', ''))
            location = _location_name(device)

            if not serial:
                continue

            try:
                samples = get_latest_samples(token, serial)
            except Exception:
                logger.exception('Failed to fetch samples for device %s (%s)', serial, device_name)
                continue

            label_values = {
                'device': serial,
                'device_name': device_name,
                'device_type': device_type,
                'location': location,
            }
            label_tuple = (serial, device_name, device_type, location)

            updated = False
            for field, gauge in _FIELD_GAUGES.items():
                raw = samples.get(field)
                if raw is None:
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    logger.debug('Non-numeric value for field %s on device %s', field, serial)
                    continue
                gauge.labels(**label_values).set(value)
                updated = True
                logger.debug(
                    'device=%s name=%s field=%s value=%s',
                    serial,
                    device_name,
                    field,
                    value,
                )

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
