"""Configuration loading: TOML parsing, deep merging, and broker list merging."""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge_broker_lists(base_brokers: list[dict[str, Any]], override_brokers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep merge named brokers in encounter order, including new overlay names."""
    result: list[dict[str, Any]] = []
    names: dict[str, int] = {}

    for broker in (*base_brokers, *override_brokers):
        name = broker.get('name', '')
        if name and name in names:
            result[names[name]] = deep_merge(result[names[name]], broker)
        else:
            if name:
                names[name] = len(result)
            result.append(broker)

    return result


def _apply_override(config: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge an override dict into config, handling broker lists specially."""
    override_brokers = override.get('broker')
    config_brokers = config.get('broker', [])
    config = deep_merge(config, {key: value for key, value in override.items() if key != 'broker'})
    if override_brokers is not None:
        config['broker'] = merge_broker_lists(config_brokers, override_brokers)
    return config


def _load_toml(path: str | Path) -> dict[str, Any]:
    """Load a single TOML file and return its contents as a dict."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _load_config_dir(config: dict[str, Any], config_d: Path) -> dict[str, Any]:
    """Load all *.toml files from a config.d directory as overlays."""
    if not config_d.is_dir():
        return config
    for override_file in sorted(config_d.glob('*.toml')):
        logger.info(f"Loading config override: {override_file}")
        override = _load_toml(override_file)
        config = _apply_override(config, override)
    return config


def load_config(config_paths: list[str] | None = None) -> dict[str, Any]:
    """Load and merge TOML configuration.

    When no --config paths are provided (default):
      1. Load base config.toml from MCTOMQTT_CONFIG_DIR (/etc/mctomqtt if unset)
      2. Overlay that directory's config.d/*.toml files (alphabetical)

    When --config paths are provided:
      Load only those files in order, each overlaying the previous.
      Environment/default search paths and config.d directories are skipped.
    """
    if config_paths:
        config: dict = {}
        for path in config_paths:
            if not os.path.exists(path):
                logger.error(f"Config file not found: {path}")
                continue
            logger.info(f"Loading config: {path}")
            override = _load_toml(path)
            config = _apply_override(config, override)
        return config

    # Default: load system config
    config = {}
    config_dir = Path(os.environ.get('MCTOMQTT_CONFIG_DIR') or '/etc/mctomqtt')
    base_path = config_dir / 'config.toml'
    if os.path.exists(base_path):
        config = _apply_override(config, _load_toml(base_path))
        logger.info(f"Loaded base config from {base_path}")
    else:
        logger.warning(f"Base config not found at {base_path}, using defaults")

    # Load drop-in overrides
    config = _load_config_dir(config, config_dir / 'config.d')

    return config


def log_config_sources(config: dict[str, Any]) -> None:
    """Log configuration summary."""
    general = config.get('general', {})
    brokers = config.get('broker', [])
    serial_cfg = config.get('serial', {})

    logger.info(f"IATA: {general.get('iata', 'XXX')}")
    logger.info(f"Serial ports: {serial_cfg.get('ports', ['/dev/ttyACM0'])}")
    logger.info(f"Brokers configured: {len(brokers)}")

    for i, broker in enumerate(brokers):
        name = broker.get('name', f'broker-{i}')
        enabled = broker.get('enabled', False)
        server = broker.get('server', 'unknown')
        port = broker.get('port', 1883)
        logger.debug(f"  [{name}] enabled={enabled} server={server}:{port}")
