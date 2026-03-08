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
import time
import threading

import pyemvue
from pyemvue.enums import Scale, Unit
from prometheus_client import start_http_server, Gauge

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('vueprom')

# Prometheus metrics
KWH_TO_WATTS = 60 * 1000  # 1 kWh over 1 minute == 60 * 1000 W

energy_usage_watts = Gauge(
    'energy_usage_watts',
    'Current energy usage in watts',
    ['account', 'device', 'channel'],
)

running = True
pause_event = threading.Event()


def signal_handler(sig, frame):
    global running
    logger.info('Received signal %s, shutting down...', sig)
    running = False
    pause_event.set()


def login(account):
    """Authenticate with the Emporia Vue API and store the session."""
    vue = pyemvue.PyEmVue()
    vue.login(username=account['email'], password=account['password'])
    account['vue'] = vue
    logger.info('Logged into Emporia Vue account: %s', account['name'])


def get_channel_name(account, device_name, chan):
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
                        return channels[idx]
                elif isinstance(channels, dict) and chan_num_str in channels:
                    return channels[chan_num_str]
                break
    return chan.name if chan.name else chan_num_str


def update_metrics_recursive(account_name, account, device_usage_dict, device_info):
    """Recursively update Prometheus metrics from a device usage dict."""
    for gid, device in device_usage_dict.items():
        device_name = device_info[gid].device_name if gid in device_info else str(gid)

        for chan_num, chan in device.channels.items():
            # Recurse into nested devices (subpanels / smart plugs)
            if chan.nested_devices:
                for nested_gid, nested_device in chan.nested_devices.items():
                    nested_name = (
                        nested_device.device_name
                        if hasattr(nested_device, 'device_name')
                        else str(nested_gid)
                    )
                    for nested_chan_num, nested_chan in nested_device.channels.items():
                        if nested_chan.usage is not None:
                            watts = KWH_TO_WATTS * nested_chan.usage
                            chan_label = get_channel_name(account, nested_name, nested_chan)
                            energy_usage_watts.labels(
                                account=account_name,
                                device=nested_name,
                                channel=chan_label,
                            ).set(watts)

            if chan.usage is not None:
                watts = KWH_TO_WATTS * chan.usage
                chan_label = get_channel_name(account, device_name, chan)
                energy_usage_watts.labels(
                    account=account_name,
                    device=device_name,
                    channel=chan_label,
                ).set(watts)


def collect_usage(account):
    """Collect energy usage from Emporia API and update Prometheus metrics."""
    if 'vue' not in account:
        login(account)

    vue = account['vue']
    account_name = account['name']

    try:
        devices = vue.get_devices()
        device_gids = []
        device_info = {}

        for device in devices:
            if device.device_gid not in device_gids:
                device_gids.append(device.device_gid)
                device_info[device.device_gid] = device
            else:
                device_info[device.device_gid].channels += device.channels

        device_usage_dict = vue.get_device_list_usage(
            deviceGids=device_gids,
            instant=None,
            scale=Scale.MINUTE.value,
            unit=Unit.KWH.value,
        )

        update_metrics_recursive(account_name, account, device_usage_dict, device_info)
        logger.info('Metrics updated for account: %s', account_name)

    except Exception:
        logger.exception('Failed to collect usage for account %s', account_name)
        # Force re-login on next attempt
        account.pop('vue', None)


def main():
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
    args = parser.parse_args()

    try:
        with open(args.config) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error('Failed to load config file %s: %s', args.config, e)
        sys.exit(1)

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
