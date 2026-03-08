"""
Govee temperature and humidity Prometheus exporter.

Discovers all Govee sensor devices via the Govee Router API and exposes
temperature, humidity, and battery readings as Prometheus gauges.
"""

import json
import logging
import sys
import threading
import uuid
from typing import Any

import requests
from prometheus_client import Gauge, start_http_server

logger = logging.getLogger('goveeprom')

GOVEE_API_BASE = 'https://openapi.api.govee.com/router/api/v1'

# Prometheus metrics
govee_temperature_fahrenheit = Gauge(
    'govee_temperature_fahrenheit',
    'Current temperature in Fahrenheit from Govee sensor',
    ['device', 'device_name', 'sku'],
)

govee_humidity_percent = Gauge(
    'govee_humidity_percent',
    'Current relative humidity in percent from Govee sensor',
    ['device', 'device_name', 'sku'],
)

govee_battery_percent = Gauge(
    'govee_battery_percent',
    'Current battery level in percent from Govee sensor',
    ['device', 'device_name', 'sku'],
)


def _headers(api_key: str) -> dict[str, str]:
    return {'Govee-API-Key': api_key, 'Content-Type': 'application/json'}


def get_devices(api_key: str) -> list[dict[str, Any]]:
    """Fetch all Govee devices from the Router API."""
    resp = requests.get(
        f'{GOVEE_API_BASE}/user/devices',
        headers=_headers(api_key),
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if data.get('code') != 200:
        raise RuntimeError(f"Govee API error: {data.get('message', 'unknown')}")
    result: list[dict[str, Any]] = data.get('data', [])
    return result


def get_device_state(api_key: str, sku: str, device_id: str) -> dict[str, Any]:
    """Fetch the current state of a single Govee device."""
    payload: dict[str, Any] = {
        'requestId': str(uuid.uuid4()),
        'payload': {'sku': sku, 'device': device_id},
    }
    resp = requests.post(
        f'{GOVEE_API_BASE}/device/state',
        headers=_headers(api_key),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    if data.get('code') != 200:
        logger.debug(
            'Govee device state API error response: %s',
            json.dumps(data, ensure_ascii=False),
        )
        error_msg = data.get('msg') or data.get('message') or 'unknown'
        raise RuntimeError(f'Govee API error: {error_msg}')
    result: dict[str, Any] = data.get('payload', {})
    return result


def is_sensor_device(device: dict[str, Any]) -> bool:
    """Return True if the device exposes temperature or humidity capabilities."""
    for cap in device.get('capabilities', []):
        if cap.get('instance') in ('sensorTemperature', 'sensorHumidity'):
            return True
    return False


def _precision_for(device: dict[str, Any], instance: str) -> float:
    """Return the divisor needed to convert the raw integer value to real units.

    Govee encodes sensor values as integers scaled by a *precision* factor
    declared in the device capability parameters.  E.g. precision=100 means
    the raw value 2150 represents 21.50 °C.  When the precision is absent the
    API returns the value already in its final unit, so we use 1 (no scaling).
    """
    for cap in device.get('capabilities', []):
        if cap.get('instance') == instance:
            precision = cap.get('parameters', {}).get('range', {}).get('precision')
            if precision and precision > 0:
                return float(precision)
    return 1.0


def collect_metrics(api_key: str) -> None:
    """Collect temperature/humidity readings from all discovered Govee sensors."""
    try:
        devices = get_devices(api_key)
        sensor_devices = [d for d in devices if is_sensor_device(d)]
        logger.info('Found %d temperature/humidity Govee sensor(s)', len(sensor_devices))

        for device in sensor_devices:
            sku = device.get('sku', '')
            device_id = device.get('device', '')
            device_name = device.get('deviceName', device_id)

            temp_precision = _precision_for(device, 'sensorTemperature')
            humi_precision = _precision_for(device, 'sensorHumidity')

            try:
                state = get_device_state(api_key, sku, device_id)
            except Exception:
                logger.exception(
                    'Failed to fetch state for device %s (%s)', device_id, device_name
                )
                continue

            # Detect whether the device is reporting temperature in Celsius.
            # The Govee Router API exposes a `temperatureUnit` capability in the
            # device state: value 0 = Celsius, value 1 = Fahrenheit.
            # We always store in Fahrenheit, so convert only when the device
            # reports Celsius (temperatureUnit == 0).
            temp_unit_celsius = False
            for cap in state.get('capabilities', []):
                if cap.get('instance') == 'temperatureUnit':
                    temp_unit_celsius = cap.get('state', {}).get('value') == 0
                    break

            for cap in state.get('capabilities', []):
                instance = cap.get('instance', '')
                raw = cap.get('state', {}).get('value')
                if raw is None:
                    continue

                labels = {'device': device_id, 'device_name': device_name, 'sku': sku}

                if instance == 'sensorTemperature':
                    value = raw / temp_precision
                    if temp_unit_celsius:
                        value = value * 9.0 / 5.0 + 32.0
                    govee_temperature_fahrenheit.labels(**labels).set(value)
                    logger.debug(
                        'Temperature: device=%s name=%s value=%.2f°F%s',
                        device_id,
                        device_name,
                        value,
                        ' (converted from °C)' if temp_unit_celsius else '',
                    )

                elif instance == 'sensorHumidity':
                    value = raw / humi_precision
                    govee_humidity_percent.labels(**labels).set(value)
                    logger.debug(
                        'Humidity: device=%s name=%s value=%.2f%%',
                        device_id,
                        device_name,
                        value,
                    )

                elif instance == 'battery':
                    govee_battery_percent.labels(**labels).set(float(raw))
                    logger.debug(
                        'Battery: device=%s name=%s value=%d%%',
                        device_id,
                        device_name,
                        raw,
                    )

    except Exception:
        logger.exception('Failed to collect Govee metrics')


def run(config: dict[str, Any], port: int, interval: int, stop_event: threading.Event) -> None:
    """Run the Govee exporter main loop."""
    start_http_server(port)
    logger.info('Prometheus metrics available at http://0.0.0.0:%d/metrics', port)

    api_key: str = config.get('api_key', '')
    if not api_key:
        logger.error('No api_key found in Govee config')
        return

    while not stop_event.is_set():
        collect_metrics(api_key)
        stop_event.wait(timeout=interval)

    logger.info('Goveeprom stopped.')


def load_config(path: str) -> dict[str, Any]:
    """Load and return the JSON configuration file."""
    try:
        with open(path) as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError) as e:
        logger.error('Failed to load config file %s: %s', path, e)
        sys.exit(1)
