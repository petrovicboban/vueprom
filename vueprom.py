#!/usr/bin/env python3
"""
Vueprom - Emporia Vue energy monitoring Prometheus exporter.

Fetches energy metrics from the Emporia Vue API and exposes them
via a Prometheus HTTP endpoint for scraping by Prometheus.
"""

import argparse
import json
import logging
import signal
import sys
import threading
from typing import Any

import pyemvue
from prometheus_client import Gauge, start_http_server
from pyemvue.enums import Scale, Unit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('vueprom')


def enable_debug_mode() -> None:
    """Switch the root logger and the vueprom logger to DEBUG level."""
    logging.getLogger().setLevel(logging.DEBUG)
    logger.setLevel(logging.DEBUG)
    logger.debug('Debug mode enabled')


# Prometheus metrics
KWH_TO_WATTS = 60 * 1000  # 1 kWh over 1 minute == 60 * 1000 W

energy_usage_watts = Gauge(
    'energy_usage_watts',
    'Current energy usage in watts',
    ['account', 'device', 'channel'],
)

# Tracks the (device, channel) labelsets last seen per account so stale
# series can be removed when a device/channel disappears.
_known_labelsets: dict = {}

running = True
pause_event = threading.Event()


def signal_handler(sig: int, frame: Any) -> None:
    global running
    logger.info('Received signal %s, shutting down...', sig)
    running = False
    pause_event.set()


def login(account: dict[str, Any]) -> None:
    """Authenticate with the Emporia Vue API and store the session."""
    logger.debug('Logging into Emporia Vue account: %s', account['name'])
    vue = pyemvue.PyEmVue()
    vue.login(username=account['email'], password=account['password'])
    account['vue'] = vue
    logger.info('Logged into Emporia Vue account: %s', account['name'])


def get_channel_name(account: dict[str, Any], device_name: str, chan: Any) -> str:
    """Return a human-readable channel name from config or the API default."""
    chan_num = chan.channel_num
    chan_num_str = str(chan_num)
    if 'devices' in account:
        for dev_config in account['devices']:
            if dev_config['name'] == device_name:
                channels = dev_config.get('channels', [])
                if isinstance(channels, list) and chan_num_str.isdigit():
                    idx = int(chan_num_str) - 1
                    if 0 <= idx < len(channels):
                        name = str(channels[idx])
                        logger.debug(
                            'Channel name resolved from config list: device=%s chan=%s -> %s',
                            device_name,
                            chan_num_str,
                            name,
                        )
                        return name
                elif isinstance(channels, dict) and chan_num_str in channels:
                    name = str(channels[chan_num_str])
                    logger.debug(
                        'Channel name resolved from config dict: device=%s chan=%s -> %s',
                        device_name,
                        chan_num_str,
                        name,
                    )
                    return name
                break
    name = chan.name if chan.name else chan_num_str
    logger.debug(
        'Channel name resolved from API: device=%s chan=%s -> %s',
        device_name,
        chan_num_str,
        name,
    )
    return name


def update_metrics_recursive(
    account_name: str,
    account: dict[str, Any],
    device_usage_dict: dict[Any, Any],
    device_info: dict[Any, Any],
) -> set[tuple[str, str]]:
    """Recursively update Prometheus metrics from a device usage dict.

    Returns the set of (device, channel) label tuples that were updated this
    cycle, so callers can remove any labelsets that are no longer present.
    """
    active = set()

    for gid, device in device_usage_dict.items():
        device_name = device_info[gid].device_name if gid in device_info else str(gid)
        logger.debug('Processing device: %s (gid=%s)', device_name, gid)

        for _chan_num, chan in device.channels.items():
            # Recurse into nested devices (subpanels / smart plugs)
            if chan.nested_devices:
                for nested_gid, nested_device in chan.nested_devices.items():
                    if nested_gid in device_info and device_info[nested_gid].device_name:
                        nested_name = device_info[nested_gid].device_name
                    elif hasattr(nested_device, 'device_name') and nested_device.device_name:
                        nested_name = nested_device.device_name
                    else:
                        nested_name = str(nested_gid)
                    for _nested_chan_num, nested_chan in nested_device.channels.items():
                        if nested_chan.usage is not None:
                            watts = KWH_TO_WATTS * nested_chan.usage
                            chan_label = get_channel_name(account, nested_name, nested_chan)
                            logger.debug(
                                'Setting metric: account=%s device=%s channel=%s watts=%.2f',
                                account_name,
                                nested_name,
                                chan_label,
                                watts,
                            )
                            energy_usage_watts.labels(
                                account=account_name,
                                device=nested_name,
                                channel=chan_label,
                            ).set(watts)
                            active.add((nested_name, chan_label))

            if chan.usage is not None:
                watts = KWH_TO_WATTS * chan.usage
                chan_label = get_channel_name(account, device_name, chan)
                logger.debug(
                    'Setting metric: account=%s device=%s channel=%s watts=%.2f',
                    account_name,
                    device_name,
                    chan_label,
                    watts,
                )
                energy_usage_watts.labels(
                    account=account_name,
                    device=device_name,
                    channel=chan_label,
                ).set(watts)
                active.add((device_name, chan_label))

    return active


def collect_usage(account: dict[str, Any]) -> None:
    """Collect energy usage from Emporia API and update Prometheus metrics."""
    account_name = account['name']

    try:
        if 'vue' not in account:
            login(account)

        vue = account.get('vue')
        if vue is None:
            logger.error(
                'Vue client not initialized for account %s after login attempt',
                account_name,
            )
            return
        devices = vue.get_devices()
        device_gids = []
        device_info = {}

        for device in devices:
            if device.device_gid not in device_gids:
                device_gids.append(device.device_gid)
                device_info[device.device_gid] = device
                logger.debug(
                    'Discovered device: %s (gid=%s)', device.device_name, device.device_gid
                )
            else:
                # Duplicate device_gid encountered; skip to avoid mutating SDK objects.
                pass
            # Also index nested devices so their names are available when building metrics
            if hasattr(device, 'channels') and device.channels:
                for chan in device.channels.values():
                    if chan.nested_devices:
                        for nested_gid, nested_dev in chan.nested_devices.items():
                            if nested_gid not in device_info:
                                device_info[nested_gid] = nested_dev
                                logger.debug(
                                    'Discovered nested device: %s (gid=%s)',
                                    getattr(nested_dev, 'device_name', str(nested_gid)),
                                    nested_gid,
                                )

        device_usage_dict = vue.get_device_list_usage(
            deviceGids=device_gids,
            instant=None,
            scale=Scale.MINUTE.value,
            unit=Unit.KWH.value,
        )

        active = update_metrics_recursive(account_name, account, device_usage_dict, device_info)

        # Remove any labelsets that existed in the previous cycle but are gone now
        stale = _known_labelsets.get(account_name, set()) - active
        for device, channel in stale:
            try:
                energy_usage_watts.remove(account_name, device, channel)
            except Exception:
                logger.debug(
                    'Could not remove stale labelset account=%s device=%s channel=%s',
                    account_name,
                    device,
                    channel,
                )
        _known_labelsets[account_name] = active

        logger.info('Metrics updated for account: %s', account_name)

    except Exception:
        logger.exception('Failed to collect usage for account %s', account_name)
        # Force re-login on next attempt
        account.pop('vue', None)


def main() -> None:
    global running

    parser = argparse.ArgumentParser(
        description='Vueprom – Emporia Vue energy monitoring Prometheus exporter',
    )
    parser.add_argument('config', help='Path to configuration JSON file')
    parser.add_argument(
        '--port',
        type=int,
        default=8080,
        help='Port for Prometheus metrics HTTP endpoint (default: 8080)',
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Collection interval in seconds (default: 60)',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        default=False,
        help='Enable debug logging',
    )
    args = parser.parse_args()

    try:
        with open(args.config) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error('Failed to load config file %s: %s', args.config, e)
        sys.exit(1)

    if args.debug or config.get('debug', False):
        enable_debug_mode()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    port = config.get('port', args.port)
    interval = config.get('updateIntervalSecs', args.interval)

    start_http_server(port)
    logger.info('Prometheus metrics available at http://0.0.0.0:%d/metrics', port)

    while running:
        for account in config.get('accounts', []):
            if not running:
                break
            collect_usage(account)

        # Interruptible sleep: wake immediately on shutdown signal
        pause_event.wait(timeout=interval)
        pause_event.clear()

    logger.info('Vueprom stopped.')


if __name__ == '__main__':
    main()
