"""
Ecobee thermostat Prometheus exporter.

Discovers all registered Ecobee thermostats and exposes indoor temperature,
humidity, setpoints, HVAC mode, equipment status, outdoor weather, and remote
sensor readings as Prometheus gauges.

Authentication uses the Ecobee PIN OAuth2 flow:
  1. First run: the exporter requests a PIN, logs instructions for the user to
     authorize at ecobee.com, polls until authorization succeeds, then saves
     the refresh token back to the config file automatically.
  2. Subsequent runs: the stored refresh_token is exchanged for a fresh access
     token (Ecobee rotates refresh tokens; the new token is saved each time).

API base: https://api.ecobee.com/1
"""

import json
import logging
import sys
import threading
import time
from typing import Any

import requests
from prometheus_client import Gauge, start_http_server

logger = logging.getLogger('ecobeeprom')

ECOBEE_AUTHORIZE_URL = 'https://api.ecobee.com/authorize'
ECOBEE_TOKEN_URL = 'https://api.ecobee.com/token'
ECOBEE_API_BASE = 'https://api.ecobee.com/1'

# Ecobee encodes "sensor unavailable" as 12300 (tenths of °F)
_ECOBEE_TEMP_UNAVAILABLE = 12300

# All HVAC modes the Ecobee API can report
_KNOWN_HVAC_MODES = ('heat', 'cool', 'auto', 'auxHeatOnly', 'off')

# Seconds before expiry to request a new token (safety buffer)
_TOKEN_EXPIRY_BUFFER_SECS = 60

# Maximum PIN polling interval in seconds (caps exponential back-off)
_MAX_POLL_INTERVAL_SECS = 120

# ---------------------------------------------------------------------------
# Prometheus metrics
# Labels:
#   thermostat-level : thermostat (identifier), thermostat_name
#   mode             : thermostat, thermostat_name, mode
#   equipment        : thermostat, thermostat_name, equipment
#   sensor           : thermostat, thermostat_name, sensor (id), sensor_name
# ---------------------------------------------------------------------------

_THERMOSTAT_LABELS = ['thermostat', 'thermostat_name']
_MODE_LABELS = ['thermostat', 'thermostat_name', 'mode']
_EQUIPMENT_LABELS = ['thermostat', 'thermostat_name', 'equipment']
_SENSOR_LABELS = ['thermostat', 'thermostat_name', 'sensor', 'sensor_name']

ecobee_thermostat_info = Gauge(
    'ecobee_thermostat_info',
    'Ecobee thermostat metadata; value is always 1',
    _THERMOSTAT_LABELS,
)
ecobee_temperature_fahrenheit = Gauge(
    'ecobee_temperature_fahrenheit',
    'Indoor temperature in degrees Fahrenheit from Ecobee thermostat',
    _THERMOSTAT_LABELS,
)
ecobee_humidity_percent = Gauge(
    'ecobee_humidity_percent',
    'Indoor relative humidity in percent from Ecobee thermostat',
    _THERMOSTAT_LABELS,
)
ecobee_heat_setpoint_fahrenheit = Gauge(
    'ecobee_heat_setpoint_fahrenheit',
    'Heating setpoint in degrees Fahrenheit',
    _THERMOSTAT_LABELS,
)
ecobee_cool_setpoint_fahrenheit = Gauge(
    'ecobee_cool_setpoint_fahrenheit',
    'Cooling setpoint in degrees Fahrenheit',
    _THERMOSTAT_LABELS,
)
ecobee_outdoor_temperature_fahrenheit = Gauge(
    'ecobee_outdoor_temperature_fahrenheit',
    'Outdoor temperature in degrees Fahrenheit from Ecobee weather forecast',
    _THERMOSTAT_LABELS,
)
ecobee_outdoor_humidity_percent = Gauge(
    'ecobee_outdoor_humidity_percent',
    'Outdoor relative humidity in percent from Ecobee weather forecast',
    _THERMOSTAT_LABELS,
)
ecobee_hvac_mode = Gauge(
    'ecobee_hvac_mode',
    'Current HVAC mode; value is 1 for the active mode label, 0 for all others',
    _MODE_LABELS,
)
ecobee_equipment_running = Gauge(
    'ecobee_equipment_running',
    'HVAC equipment running status (1 = running, 0 = idle)',
    _EQUIPMENT_LABELS,
)
ecobee_sensor_temperature_fahrenheit = Gauge(
    'ecobee_sensor_temperature_fahrenheit',
    'Temperature in degrees Fahrenheit from Ecobee remote sensor',
    _SENSOR_LABELS,
)
ecobee_sensor_humidity_percent = Gauge(
    'ecobee_sensor_humidity_percent',
    'Relative humidity in percent from Ecobee remote sensor',
    _SENSOR_LABELS,
)
ecobee_sensor_occupancy = Gauge(
    'ecobee_sensor_occupancy',
    'Occupancy detected by Ecobee remote sensor (1 = occupied, 0 = unoccupied)',
    _SENSOR_LABELS,
)

# Stale label-set tracking (devices/equipment that disappear between scrapes)
_known_thermostat_labelsets: set[tuple[str, str]] = set()
_known_mode_labelsets: set[tuple[str, str, str]] = set()
_known_equipment_labelsets: set[tuple[str, str, str]] = set()
_known_sensor_labelsets: set[tuple[str, str, str, str]] = set()

# ---------------------------------------------------------------------------
# Access-token cache
# ---------------------------------------------------------------------------

_token_cache: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Token / auth helpers
# ---------------------------------------------------------------------------


def _authorize_pin(api_key: str) -> tuple[str, str, int, int]:
    """Request a PIN from the Ecobee authorize endpoint.

    Returns (pin, auth_code, poll_interval_secs, expires_in_mins).
    """
    resp = requests.get(
        ECOBEE_AUTHORIZE_URL,
        params={
            'response_type': 'ecobeePin',
            'client_id': api_key,
            'scope': 'smartRead',
        },
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    pin: str = str(data['ecobeePin'])
    code: str = str(data['code'])
    interval: int = int(data.get('interval', 30))
    expires_in_mins: int = int(data.get('expires_in', 9))
    return pin, code, interval, expires_in_mins


def _request_tokens_pin(api_key: str, auth_code: str) -> dict[str, Any]:
    """Exchange a PIN auth code for access + refresh tokens."""
    resp = requests.post(
        ECOBEE_TOKEN_URL,
        params={
            'grant_type': 'ecobeePin',
            'code': auth_code,
            'client_id': api_key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def _request_tokens_refresh(api_key: str, refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for new access + refresh tokens."""
    resp = requests.post(
        ECOBEE_TOKEN_URL,
        params={
            'grant_type': 'refresh_token',
            'code': refresh_token,
            'client_id': api_key,
        },
        timeout=30,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def _save_refresh_token(token: str, config: dict[str, Any]) -> None:
    """Persist the refresh token back to the config file on disk."""
    config_path: str = config.get('_config_path', '')
    if not config_path:
        logger.warning('Cannot persist refresh token: config path unknown')
        return
    config['refresh_token'] = token
    # Strip internal keys before writing
    saved = {k: v for k, v in config.items() if not k.startswith('_')}
    try:
        with open(config_path, 'w') as fh:
            json.dump(saved, fh, indent=4)
        logger.info('Refresh token saved to %s', config_path)
    except OSError as exc:
        logger.error('Failed to save refresh token to %s: %s', config_path, exc)


def _update_token_cache(data: dict[str, Any]) -> str:
    """Store a new access token in the module-level cache and return it."""
    access_token: str = str(data['access_token'])
    expires_in: int = int(data.get('expires_in', 3600))
    _token_cache['access_token'] = access_token
    _token_cache['expires_at'] = time.time() + expires_in - _TOKEN_EXPIRY_BUFFER_SECS
    return access_token


def _get_token(api_key: str, config: dict[str, Any]) -> str:
    """Return a valid access token, refreshing via the refresh token if needed."""
    if _token_cache.get('access_token') and time.time() < float(_token_cache.get('expires_at', 0)):
        return str(_token_cache['access_token'])

    refresh_token: str = config.get('refresh_token', '')
    if not refresh_token:
        raise RuntimeError('No refresh token in config; PIN authorization is required')

    data = _request_tokens_refresh(api_key, refresh_token)
    access_token = _update_token_cache(data)
    new_refresh: str = str(data.get('refresh_token', refresh_token))
    if new_refresh != refresh_token:
        # Ecobee rotates refresh tokens on every use
        _save_refresh_token(new_refresh, config)
    logger.debug(
        'Refreshed Ecobee access token (expires in %ds)', int(data.get('expires_in', 3600))
    )
    return access_token


# ---------------------------------------------------------------------------
# PIN authorization flow (first-time / re-authorization)
# ---------------------------------------------------------------------------


def _do_pin_flow(api_key: str, config: dict[str, Any], stop_event: threading.Event) -> bool:
    """
    Run the interactive PIN authorization flow.

    Logs the PIN for the user to enter at ecobee.com, then polls the token
    endpoint until authorization succeeds, the PIN expires, or stop_event fires.
    Returns True on success, False on failure or cancellation.
    """
    try:
        pin, auth_code, poll_interval, expires_in_mins = _authorize_pin(api_key)
    except Exception:
        logger.exception('Failed to request Ecobee PIN')
        return False

    logger.info('=' * 60)
    logger.info('ACTION REQUIRED - Ecobee PIN authorization')
    logger.info('  1. Log in to https://www.ecobee.com')
    logger.info('  2. Click the person icon in the top-right corner')
    logger.info('  3. Navigate to My Apps')
    logger.info('  4. Click "Add Application"')
    logger.info('  5. Enter the PIN: %s', pin)
    logger.info('  (PIN expires in %d minutes)', expires_in_mins)
    logger.info('=' * 60)

    deadline = time.time() + expires_in_mins * 60
    while time.time() < deadline:
        if stop_event.wait(timeout=float(poll_interval)):
            logger.info('Stop requested during PIN authorization')
            return False
        try:
            data = _request_tokens_pin(api_key, auth_code)
            _update_token_cache(data)
            new_refresh: str = str(data['refresh_token'])
            _save_refresh_token(new_refresh, config)
            logger.info('Ecobee authorization successful!')
            return True
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                try:
                    body: dict[str, Any] = exc.response.json()
                except ValueError:
                    body = {}
                error: str = str(body.get('error', ''))
                if error == 'authorization_pending':
                    logger.debug('Waiting for user to authorize PIN...')
                    continue
                if error == 'authorization_expired':
                    logger.error('Ecobee PIN expired. Restart the exporter to obtain a new PIN.')
                    return False
                if error == 'slow_down':
                    poll_interval = min(poll_interval * 2, _MAX_POLL_INTERVAL_SECS)
                    logger.debug('Ecobee requests slower polling; new interval=%ds', poll_interval)
                    continue
            logger.exception('Unexpected error during Ecobee PIN authorization')
            return False

    logger.error('Ecobee PIN authorization timed out. Restart the exporter to get a new PIN.')
    return False


# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------


def get_thermostats(token: str) -> list[dict[str, Any]]:
    """Return all registered thermostats with runtime, settings, weather, sensors."""
    selection = {
        'selection': {
            'selectionType': 'registered',
            'selectionMatch': '',
            'includeRuntime': True,
            'includeSettings': True,
            'includeWeather': True,
            'includeEquipmentStatus': True,
            'includeSensors': True,
        }
    }
    resp = requests.get(
        f'{ECOBEE_API_BASE}/thermostat',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        params={'json': json.dumps(selection)},
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    result: list[dict[str, Any]] = data.get('thermostatList', [])
    return result


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


def collect_metrics(api_key: str, config: dict[str, Any]) -> None:
    """Collect readings from all registered Ecobee thermostats."""
    global _known_thermostat_labelsets, _known_mode_labelsets
    global _known_equipment_labelsets, _known_sensor_labelsets

    try:
        token = _get_token(api_key, config)
        thermostats = get_thermostats(token)
        logger.debug('Found %d Ecobee thermostat(s)', len(thermostats))

        active_thermostats: set[tuple[str, str]] = set()
        active_modes: set[tuple[str, str, str]] = set()
        active_equipment: set[tuple[str, str, str]] = set()
        active_sensors: set[tuple[str, str, str, str]] = set()

        for t in thermostats:
            t_id = str(t.get('identifier', ''))
            t_name = str(t.get('name', t_id))
            if not t_id:
                continue

            t_labels = {'thermostat': t_id, 'thermostat_name': t_name}
            t_tuple: tuple[str, str] = (t_id, t_name)

            ecobee_thermostat_info.labels(**t_labels).set(1)
            active_thermostats.add(t_tuple)

            # --- Runtime (indoor conditions + setpoints) ---
            runtime: dict[str, Any] = t.get('runtime') or {}

            actual_temp = runtime.get('actualTemperature')
            if actual_temp is not None:
                temp_f = float(actual_temp) / 10.0
                ecobee_temperature_fahrenheit.labels(**t_labels).set(temp_f)
                logger.debug('thermostat=%s name=%s temp=%.1f°F', t_id, t_name, temp_f)

            actual_hum = runtime.get('actualHumidity')
            if actual_hum is not None:
                ecobee_humidity_percent.labels(**t_labels).set(float(actual_hum))

            desired_heat = runtime.get('desiredHeat')
            if desired_heat is not None:
                ecobee_heat_setpoint_fahrenheit.labels(**t_labels).set(float(desired_heat) / 10.0)

            desired_cool = runtime.get('desiredCool')
            if desired_cool is not None:
                ecobee_cool_setpoint_fahrenheit.labels(**t_labels).set(float(desired_cool) / 10.0)

            # --- HVAC mode (emit all known modes; 1 = current, 0 = others) ---
            settings: dict[str, Any] = t.get('settings') or {}
            current_mode = str(settings.get('hvacMode', 'off'))
            for mode in _KNOWN_HVAC_MODES:
                m_labels = {'thermostat': t_id, 'thermostat_name': t_name, 'mode': mode}
                m_tuple: tuple[str, str, str] = (t_id, t_name, mode)
                ecobee_hvac_mode.labels(**m_labels).set(1 if mode == current_mode else 0)
                active_modes.add(m_tuple)
            logger.debug('thermostat=%s hvacMode=%s', t_id, current_mode)

            # --- Weather (outdoor conditions from Ecobee's weather forecast) ---
            weather: dict[str, Any] = t.get('weather') or {}
            forecasts: list[dict[str, Any]] = weather.get('forecasts') or []
            if forecasts:
                fc: dict[str, Any] = forecasts[0]
                outdoor_temp = fc.get('temperature')
                if outdoor_temp is not None:
                    ecobee_outdoor_temperature_fahrenheit.labels(**t_labels).set(
                        float(outdoor_temp) / 10.0
                    )
                outdoor_hum = fc.get('relativeHumidity')
                if outdoor_hum is not None:
                    ecobee_outdoor_humidity_percent.labels(**t_labels).set(float(outdoor_hum))

            # --- Equipment running status ---
            equip_status_str: str = str(t.get('equipmentStatus') or '')
            running_equip: set[str] = (
                {e.strip() for e in equip_status_str.split(',') if e.strip()}
                if equip_status_str.strip()
                else set()
            )
            # Union of currently running + previously tracked equipment for this thermostat
            prev_equip = {
                tup[2] for tup in _known_equipment_labelsets if tup[0] == t_id and tup[1] == t_name
            }
            for equip in running_equip | prev_equip:
                eq_labels = {
                    'thermostat': t_id,
                    'thermostat_name': t_name,
                    'equipment': equip,
                }
                eq_tuple: tuple[str, str, str] = (t_id, t_name, equip)
                ecobee_equipment_running.labels(**eq_labels).set(1 if equip in running_equip else 0)
                active_equipment.add(eq_tuple)
            if running_equip:
                logger.debug('thermostat=%s running=%s', t_id, ','.join(sorted(running_equip)))

            # --- Remote sensors (includes the thermostat body sensor itself) ---
            remote_sensors: list[dict[str, Any]] = t.get('remoteSensors') or []
            for sensor in remote_sensors:
                s_id = str(sensor.get('id', ''))
                s_name = str(sensor.get('name', s_id))
                if not s_id:
                    continue

                s_labels = {
                    'thermostat': t_id,
                    'thermostat_name': t_name,
                    'sensor': s_id,
                    'sensor_name': s_name,
                }
                s_tuple: tuple[str, str, str, str] = (t_id, t_name, s_id, s_name)

                for cap in sensor.get('capability') or []:
                    cap_type = str(cap.get('type', ''))
                    cap_value = cap.get('value')
                    if cap_value is None:
                        continue

                    if cap_type == 'temperature':
                        try:
                            raw_temp = float(cap_value)
                            if int(raw_temp) != _ECOBEE_TEMP_UNAVAILABLE:
                                ecobee_sensor_temperature_fahrenheit.labels(**s_labels).set(
                                    raw_temp / 10.0
                                )
                                active_sensors.add(s_tuple)
                        except (TypeError, ValueError):
                            logger.debug('Non-numeric temperature for sensor %s', s_id)

                    elif cap_type == 'humidity':
                        try:
                            ecobee_sensor_humidity_percent.labels(**s_labels).set(float(cap_value))
                            active_sensors.add(s_tuple)
                        except (TypeError, ValueError):
                            logger.debug('Non-numeric humidity for sensor %s', s_id)

                    elif cap_type == 'occupancy':
                        occ = 1 if str(cap_value).lower() == 'true' else 0
                        ecobee_sensor_occupancy.labels(**s_labels).set(occ)
                        active_sensors.add(s_tuple)

        # --- Remove stale label-sets (devices/equipment that disappeared) ---
        for stale_tuple in _known_thermostat_labelsets - active_thermostats:
            stale_id, stale_name = stale_tuple
            for gauge in [
                ecobee_thermostat_info,
                ecobee_temperature_fahrenheit,
                ecobee_humidity_percent,
                ecobee_heat_setpoint_fahrenheit,
                ecobee_cool_setpoint_fahrenheit,
                ecobee_outdoor_temperature_fahrenheit,
                ecobee_outdoor_humidity_percent,
            ]:
                try:
                    gauge.remove(stale_id, stale_name)
                except Exception:
                    logger.debug('Could not remove stale thermostat labelset %s', stale_tuple)

        for stale_t in _known_mode_labelsets - active_modes:
            stale_id, stale_name, stale_mode = stale_t
            try:
                ecobee_hvac_mode.remove(stale_id, stale_name, stale_mode)
            except Exception:
                logger.debug('Could not remove stale mode labelset %s', stale_t)

        for stale_e in _known_equipment_labelsets - active_equipment:
            stale_id, stale_name, stale_equip = stale_e
            try:
                ecobee_equipment_running.remove(stale_id, stale_name, stale_equip)
            except Exception:
                logger.debug('Could not remove stale equipment labelset %s', stale_e)

        for stale_s in _known_sensor_labelsets - active_sensors:
            stale_id, stale_name, stale_sid, stale_sname = stale_s
            for gauge in [
                ecobee_sensor_temperature_fahrenheit,
                ecobee_sensor_humidity_percent,
                ecobee_sensor_occupancy,
            ]:
                try:
                    gauge.remove(stale_id, stale_name, stale_sid, stale_sname)
                except Exception:
                    logger.debug('Could not remove stale sensor labelset %s', stale_s)

        _known_thermostat_labelsets = active_thermostats
        _known_mode_labelsets = active_modes
        _known_equipment_labelsets = active_equipment
        _known_sensor_labelsets = active_sensors

    except Exception:
        logger.exception('Failed to collect Ecobee metrics')


# ---------------------------------------------------------------------------
# Entry-point helpers required by __main__.py
# ---------------------------------------------------------------------------


def run(config: dict[str, Any], port: int, interval: int, stop_event: threading.Event) -> None:
    """Run the Ecobee exporter main loop."""
    start_http_server(port)
    logger.info('Prometheus metrics available at http://0.0.0.0:%d/metrics', port)

    api_key: str = config.get('api_key', '')
    if not api_key:
        logger.error('api_key is required in the Ecobee config')
        sys.exit(1)

    # If no refresh token is present, run the interactive PIN authorization flow
    if not config.get('refresh_token', ''):
        logger.info('No refresh token found; starting Ecobee PIN authorization flow...')
        if not _do_pin_flow(api_key, config, stop_event):
            if not stop_event.is_set():
                logger.error('Ecobee PIN authorization failed. Exiting.')
                sys.exit(1)
            return

    while not stop_event.is_set():
        collect_metrics(api_key, config)
        stop_event.wait(timeout=float(interval))

    logger.info('Ecobeeprom stopped.')


def load_config(path: str) -> dict[str, Any]:
    """Load and return the JSON configuration file."""
    try:
        with open(path) as fh:
            config: dict[str, Any] = json.load(fh)
        # Store path so run() can save the refresh_token back to disk
        config['_config_path'] = path
        return config
    except (OSError, json.JSONDecodeError) as exc:
        logger.error('Failed to load config file %s: %s', path, exc)
        sys.exit(1)
