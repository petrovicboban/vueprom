#!/usr/bin/env python3
"""
promexporters – shared entry point for Prometheus exporters.

Usage:
    promexporters --exporter vue   <config.json> [options]
    promexporters --exporter govee <config.json> [options]
"""

import argparse
import importlib
import logging
import signal
import sys
import threading
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger('promexporters')


def _enable_debug() -> None:
    logging.getLogger().setLevel(logging.DEBUG)
    logger.setLevel(logging.DEBUG)
    logger.debug('Debug mode enabled')


def _make_signal_handler(stop_event: threading.Event) -> Any:
    def _handler(sig: int, frame: Any) -> None:
        logger.info('Received signal %s, shutting down…', sig)
        stop_event.set()

    return _handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            'promexporters – Prometheus exporters for Emporia Vue, Govee, Airthings, '
            'and Ecobee devices'
        ),
    )
    parser.add_argument(
        '--exporter',
        choices=['vue', 'govee', 'airthings', 'ecobee'],
        default='vue',
        help=(
            'Which exporter to run: "vue" (Emporia Vue energy), '
            '"govee" (Govee temperature/humidity), '
            '"airthings" (Airthings air quality), or '
            '"ecobee" (Ecobee thermostats). Default: vue'
        ),
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

    stop_event = threading.Event()
    signal.signal(signal.SIGINT, _make_signal_handler(stop_event))
    signal.signal(signal.SIGTERM, _make_signal_handler(stop_event))

    mod_name = f'promexporters.{args.exporter}'
    exporter_module: Any = importlib.import_module(mod_name)

    config = exporter_module.load_config(args.config)

    if args.debug or config.get('debug', False):
        _enable_debug()

    port: int = config.get('port', args.port)
    interval: int = config.get('updateIntervalSecs', args.interval)

    logger.info('Starting %s exporter (port=%d, interval=%ds)', args.exporter, port, interval)

    try:
        exporter_module.run(config, port, interval, stop_event)
    except Exception:
        logger.exception('Exporter terminated with an error')
        sys.exit(1)


if __name__ == '__main__':
    main()
