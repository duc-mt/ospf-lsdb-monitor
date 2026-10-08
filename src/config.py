"""Settings loading and validation shared by the poller and the orchestrator."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from src import TrackerError

DEFAULT_CONFIG_PATH = Path("config/settings.yaml")

#: Environment variables that override credentials from the YAML file, so the
#: password does not have to live on disk.
ENV_OVERRIDES = {
    "username": "OSPF_MONITOR_USERNAME",
    "password": "OSPF_MONITOR_PASSWORD",
    "secret": "OSPF_MONITOR_SECRET",
}

# The process ID is interpolated into CLI commands, so only a conservative character
# set is accepted (numbers for IOS/Arista/Huawei, names for IOS-XR/NX-OS instance tags).
_PROCESS_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ConfigError(TrackerError):
    """Raised when settings.yaml is missing, malformed or incomplete."""


def load_settings(path: str | os.PathLike, required: bool = True) -> dict[str, Any]:
    """Read the YAML settings file into a dict.

    Args:
        path: Settings file.
        required: If False, a missing file yields ``{}`` (used by ``--replay``).

    Raises:
        ConfigError: If the file is unreadable or not a YAML mapping.
    """
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            settings = yaml.safe_load(handle)
    except FileNotFoundError as exc:
        if not required:
            return {}
        raise ConfigError(f"Settings file not found: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read settings file {path}: {exc}") from exc
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return settings


def parse_process_id(value: Any) -> str | None:
    """Validate ``ospf_process_id`` (int or name) and return it as a string, or ``None``.

    Raises:
        ConfigError: If the value contains characters unsafe for a CLI command.
    """
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not _PROCESS_ID.match(text):
        raise ConfigError(
            f"ospf_process_id {value!r} is invalid: use 1-64 letters, digits, '.', '_' or '-'"
        )
    return text
