"""
Ecobee thermostat Prometheus exporter — via the beestat API.

Because the official Ecobee developer programme is closed to new
registrations, this exporter uses the beestat API (https://api.beestat.io/)
as a read-only proxy to Ecobee thermostat data.  Beestat is a third-party
analytics service that holds its own Ecobee developer credentials and
periodically syncs thermostat state on behalf of its users.

Authentication is simple: provide the ``api_key`` issued to you by beestat.
There is no OAuth flow, no PIN, and no token rotation — just a static API key.

API endpoint used:
  GET https://api.beestat.io/
    ?api_key=<KEY>
    &resource=ecobee_thermostat
    &method=read_id

The beestat API syncs Ecobee data on its own schedule (roughly every 3
minutes), so the minimum useful ``updateIntervalSecs`` value is 180.

How to get a beestat API key:
  1. Sign up / log in at https://beestat.io using your Ecobee account.
     Beestat handles the Ecobee OAuth for you.
  2. Request an API key via the beestat API request form or by contacting
     the beestat author via https://beestat.io.

Data-mapping notes (beestat vs. raw Ecobee API):
  • Top-level field names are snake_case (beestat DB columns) instead of
    camelCase (Ecobee API).
  • ``runtime``, ``settings``, and ``weather`` values are stored verbatim
    from the Ecobee API and therefore keep their camelCase inner keys.
  • ``equipment_status`` is a JSON *list* of strings (e.g. ["heatPump"]),
    whereas the Ecobee API returns a comma-separated string.
  • ``remote_sensors`` is a JSON list; inner keys are the Ecobee camelCase
    names (``id``, ``name``, ``capability``, …).
"""

import json
import logging
import sys
import threading
from typing import Any

import requests
from prometheus_client import Gauge, start_http_server

logger = logging.getLogger('ecobeeprom')

BEESTAT_API_URL = 'https://api.beestat.io/'

# Ecobee encodes "sensor unavailable" as 12300 (tenths of °F)
_ECOBEE_TEMP_UNAVAILABLE = 12300

# All HVAC modes the Ecobee / beestat API can report
_KNOWN_HVAC_MODES = ('heat', 'cool', 'auto', 'auxHeatOnly', 'off')

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
# Beestat API helper
# ---------------------------------------------------------------------------


def get_thermostats(api_key: str) -> list[dict[str, Any]]:
    """Return all Ecobee thermostats for the beestat account associated with api_key.

    Calls GET https://api.beestat.io/?api_key=KEY&resource=ecobee_thermostat&method=read_id

    The response is a dict with a ``data`` key containing a **dict** of
    thermostat objects keyed by their beestat ecobee_thermostat_id, e.g.::

        {"data": {"1": {...thermostat...}, "2": {...thermostat...}}}

    Each thermostat object mirrors the Ecobee API structure except:
      - top-level keys are snake_case (beestat DB column names)
      - ``equipment_status`` is a list of strings, not a comma-separated string
      - ``remote_sensors`` uses the snake_case key name (inner objects keep
        Ecobee camelCase keys)
    """
    resp = requests.get(
        BEESTAT_API_URL,
        params={
            'api_key': api_key,
            'resource': 'ecobee_thermostat',
            'method': 'read_id',
        },
        timeout=30,
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    raw: dict[str, Any] = data.get('data') or {}
    thermostats: list[dict[str, Any]] = list(raw.values())
    return thermostats


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------


def collect_metrics(api_key: str) -> None:
    """Collect readings from all Ecobee thermostats via the beestat API."""
    global _known_thermostat_labelsets, _known_mode_labelsets
    global _known_equipment_labelsets, _known_sensor_labelsets

    try:
        thermostats = get_thermostats(api_key)
        logger.debug('Found %d Ecobee thermostat(s) via beestat', len(thermostats))

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
            # beestat stores the raw Ecobee runtime JSON; inner keys are camelCase
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
            # beestat stores the raw Ecobee settings JSON; inner keys are camelCase
            settings: dict[str, Any] = t.get('settings') or {}
            current_mode = str(settings.get('hvacMode', 'off'))
            for mode in _KNOWN_HVAC_MODES:
                m_labels = {'thermostat': t_id, 'thermostat_name': t_name, 'mode': mode}
                m_tuple: tuple[str, str, str] = (t_id, t_name, mode)
                ecobee_hvac_mode.labels(**m_labels).set(1 if mode == current_mode else 0)
                active_modes.add(m_tuple)
            logger.debug('thermostat=%s hvacMode=%s', t_id, current_mode)

            # --- Weather (outdoor conditions) ---
            # beestat stores the raw Ecobee weather JSON; inner keys are camelCase
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
            # beestat converts the Ecobee comma-string into a JSON list, e.g.
            # Ecobee: "heatPump,fan"  →  beestat: ["heatPump", "fan"]
            equip_raw = t.get('equipment_status')
            running_equip: set[str] = (
                {str(e) for e in equip_raw if e} if isinstance(equip_raw, list) else set()
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
            # beestat stores the raw Ecobee remoteSensors list; inner keys are camelCase
            remote_sensors: list[dict[str, Any]] = t.get('remote_sensors') or []
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
        logger.exception('Failed to collect Ecobee metrics via beestat')


# ---------------------------------------------------------------------------
# Entry-point helpers required by __main__.py
# ---------------------------------------------------------------------------


def run(config: dict[str, Any], port: int, interval: int, stop_event: threading.Event) -> None:
    """Run the Ecobee/beestat exporter main loop."""
    start_http_server(port)
    logger.info('Prometheus metrics available at http://0.0.0.0:%d/metrics', port)

    api_key: str = config.get('api_key', '')
    if not api_key:
        logger.error('api_key (beestat API key) is required in the Ecobee config')
        sys.exit(1)

    while not stop_event.is_set():
        collect_metrics(api_key)
        stop_event.wait(timeout=float(interval))

    logger.info('Ecobeeprom stopped.')


def load_config(path: str) -> dict[str, Any]:
    """Load and return the JSON configuration file."""
    try:
        with open(path) as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError) as exc:
        logger.error('Failed to load config file %s: %s', path, exc)
        sys.exit(1)
