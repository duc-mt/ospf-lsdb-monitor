"""
==============================================================================
Module Name:   poller.py
Description:   Source module poller.py.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 poller.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoBaseException,
    NetmikoTimeoutException,
    ReadTimeout,
)
from paramiko.ssh_exception import SSHException

from src import TrackerError
from src.config import (
    DEFAULT_CONFIG_PATH,
    ENV_OVERRIDES,
    ConfigError,
    load_settings,
    parse_process_id,
)
from src.vendors import VendorProfile, get_profile

logger = logging.getLogger(__name__)

__all__ = [
    "BasePoller",
    "ConfigError",
    "DevicePoller",
    "FilePoller",
    "PollerError",
    "RawLSDB",
]


def _first_line(exc: BaseException) -> str:
    """Netmiko exceptions carry multi-paragraph help text; keep only the first line."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    return lines[0].rstrip(".") if lines else exc.__class__.__name__


class PollerError(TrackerError):
    """Raised when the device cannot be reached or returns unusable output."""


@dataclass(frozen=True)
class RawLSDB:
    """Raw, unparsed LSDB text as returned by a poller."""

    router: str  # router-LSA command output
    network: str  # network-LSA command output
    source: str  # host name/IP (or directory when replaying files)
    collected_at: str  # ISO-8601 UTC timestamp


class BasePoller(ABC):
    """Interface every poller implements."""

    #: Netmiko-style device type; the parser uses it to pick the platform adapter.
    device_type: str = "cisco_ios"
    #: OSPF process being tracked (``None`` = every process found in the output).
    process_id: str | None = None

    @abstractmethod
    def poll(self) -> RawLSDB:
        """Collect and return the raw LSDB text."""


class DevicePoller(BasePoller):
    """Collect the OSPF LSDB from a network device over SSH."""

    def __init__(
        self,
        config_path: str | os.PathLike = DEFAULT_CONFIG_PATH,
        device_type: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        """Load and validate the settings.

        Args:
            config_path: Path to the YAML settings file (ignored if ``settings`` is given).
            device_type: Overrides ``device.device_type`` from the file.
            settings: Already-loaded settings, to avoid reading the file twice.

        Raises:
            ConfigError: If the settings are missing, unreadable or incomplete.
            UnsupportedVendorError: If the device type has no vendor profile.
        """
        self.config_path = Path(config_path)
        if settings is None:
            settings = load_settings(self.config_path)
        if not isinstance(settings.get("device"), dict):
            raise ConfigError(f"{self.config_path} must contain a 'device' mapping")

        device = dict(settings["device"])
        for key, env_name in ENV_OVERRIDES.items():
            if os.environ.get(env_name):
                device[key] = os.environ[env_name]

        missing = [k for k in ("host", "username", "password") if not device.get(k)]
        if missing:
            raise ConfigError(
                f"{self.config_path}: missing device setting(s): {', '.join(missing)}"
            )

        try:
            self._port = int(device.get("port", 22))
            self._conn_timeout = int(device.get("conn_timeout", 15))
            self._auth_timeout = int(device.get("auth_timeout", 20))
            self._banner_timeout = int(device.get("banner_timeout", 20))
            self._read_timeout = int(device.get("read_timeout", 120))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{self.config_path}: numeric device settings must be integers ({exc!r})"
            ) from exc

        self.host = str(device["host"])
        self.device_type = str(device_type or device.get("device_type", "cisco_ios"))
        self.process_id = parse_process_id(settings.get("ospf_process_id"))
        self.profile: VendorProfile = get_profile(self.device_type)
        self._username = str(device["username"])
        self._password = str(device["password"])
        self._secret = str(device.get("secret") or "")

        overrides = device.get("commands") or {}
        if not isinstance(overrides, dict):
            raise ConfigError(
                f"{self.config_path}: device.commands must be a mapping (router/network)"
            )
        self.commands = {
            which: self.profile.command(which, self.process_id, overrides.get(which))
            for which in ("router", "network")
        }

    def _connection_params(self) -> dict[str, Any]:
        return {
            "device_type": self.device_type,
            "host": self.host,
            "username": self._username,
            "password": self._password,
            "secret": self._secret,
            "port": self._port,
            "conn_timeout": self._conn_timeout,
            "auth_timeout": self._auth_timeout,
            "banner_timeout": self._banner_timeout,
        }

    # ----------------------------------------------------------------- polling
    def poll(self) -> RawLSDB:
        """Connect to the seed router and return the raw LSDB output.

        Raises:
            PollerError: On authentication failure, timeout, SSH/socket errors,
                or when the device rejects a command or returns nothing useful.
        """
        logger.info("Connecting to %s:%s (%s)", self.host, self._port, self.device_type)
        try:
            with ConnectHandler(**self._connection_params()) as conn:
                if self._secret and not conn.check_enable_mode():
                    conn.enable()
                router = self._run(conn, "router")
                # No Type 2 LSAs is legitimate (e.g. only point-to-point links).
                network = self._run(conn, "network", allow_empty=True)
        except NetmikoAuthenticationException as exc:
            raise PollerError(
                f"Authentication failed for {self.host}: check username, password and enable secret"
            ) from exc
        except (TimeoutError, NetmikoTimeoutException, ReadTimeout) as exc:
            raise PollerError(
                f"Timed out talking to {self.host}: {_first_line(exc)}. Check reachability, SSH access "
                f"and the timeout settings in {self.config_path}"
            ) from exc
        except (NetmikoBaseException, SSHException, OSError) as exc:
            raise PollerError(
                f"SSH session to {self.host} failed: {_first_line(exc)}"
            ) from exc

        logger.info(
            "Collected LSDB from %s (%d + %d bytes)",
            self.host,
            len(router),
            len(network),
        )
        return RawLSDB(
            router=router,
            network=network,
            source=self.host,
            collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def _run(self, conn: Any, key: str, allow_empty: bool = False) -> str:
        """Run one LSDB command and sanity-check its output."""
        command = self.commands[key]
        logger.debug("Running: %s", command)
        output = conn.send_command(command, read_timeout=self._read_timeout)

        if any(marker in output for marker in self.profile.error_markers):
            snippet = output.strip().splitlines()[0] if output.strip() else ""
            raise PollerError(f"Device rejected '{command}': {snippet}")
        if not output.strip() and not allow_empty:
            raise PollerError(
                f"'{command}' returned no output - is OSPF running on {self.host}?"
            )
        return output


import concurrent.futures


class FilePoller(BasePoller):
    """Replay previously saved CLI output (offline testing, demos, regression tests)."""

    ROUTER_FILE = "router_lsdb.txt"
    NETWORK_FILE = "network_lsdb.txt"

    def __init__(
        self,
        directory: str | os.PathLike,
        device_type: str = "cisco_ios",
        process_id: str | None = None,
        read_timeout: int = 10,
    ) -> None:
        self.directory = Path(directory)
        self.device_type = device_type
        self.process_id = process_id
        self.read_timeout = read_timeout

    def _read_with_timeout(self, path: Path) -> str:
        if not path.exists():
            return ""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(path.read_text, encoding="utf-8")
            try:
                return future.result(timeout=self.read_timeout)
            except concurrent.futures.TimeoutError as exc:
                raise PollerError(
                    f"Timed out reading {path} after {self.read_timeout}s"
                ) from exc
            except OSError as exc:
                raise PollerError(f"Could not read replay file {path}: {exc}") from exc

    def poll(self) -> RawLSDB:
        """Read ``router_lsdb.txt`` (required) and ``network_lsdb.txt`` (optional)."""
        router_path = self.directory / self.ROUTER_FILE
        network_path = self.directory / self.NETWORK_FILE

        router = self._read_with_timeout(router_path)
        if not router:
            raise PollerError(f"Required file missing or empty: {router_path}")
        network = self._read_with_timeout(network_path)

        return RawLSDB(
            router=router,
            network=network,
            source=str(self.directory),
            collected_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
