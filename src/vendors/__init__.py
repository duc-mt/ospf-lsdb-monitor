from __future__ import annotations

"""
==============================================================================
Module Name:   __init__.py
Description:   Implementation and logic for __init__.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 __init__.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""
"""Vendor registry: one ``VendorProfile`` per Netmiko ``device_type``.

A profile bundles everything platform-specific:

* the CLI commands that dump the router (Type 1) and network (Type 2) LSAs
* the text a device prints when it rejects a command
* a factory for the adapter that parses the output into the neutral ``Lsdb`` model

To add a platform: write an adapter (see ``base.LsdbAdapter``), then register a
profile at the bottom of this file. Nothing else in the project changes.

Verification status of the bundled platforms
--------------------------------------------
cisco_ios / cisco_xe   Genie (IOS-XE parsers); exercised with sample IOS output
cisco_xr / cisco_nxos  Genie; schema handling exercised with Genie's own fixtures,
                       not with raw device text
juniper_junos          Genie (per-area split); sample follows the layout Genie's parser expects
vyos                   FRR text format; router-LSA layout taken from real FRR 9 output
arista_eos             Cisco-style text; router-LSA layout taken from Arista's lab guide
huawei / huawei_vrpv8  VRP text; layout taken from Huawei's command reference
"""


from dataclasses import dataclass
from typing import Callable

from src import TrackerError
from src.config import ConfigError
from src.vendors.base import LsdbAdapter


class UnsupportedVendorError(TrackerError):
    """Raised for a device_type that has no registered profile."""


@dataclass(frozen=True)
class CommandPair:
    """A command, plus its variant that takes an OSPF process ID (``{pid}``)."""

    default: str
    with_pid: str | None = None


@dataclass(frozen=True)
class VendorProfile:
    """Everything platform-specific about collecting and parsing an OSPF LSDB."""

    key: str
    description: str
    router: CommandPair
    network: CommandPair
    adapter_factory: Callable[[], LsdbAdapter]
    error_markers: tuple[str, ...]

    def command(
        self, which: str, process_id: str | None, override: str | None = None
    ) -> str:
        """Return the CLI command for ``which`` (``"router"`` or ``"network"``).

        ``override`` is the user's ``device.commands.<which>`` setting; it may contain
        ``{pid}``, which is replaced by the process ID.
        """
        if override:
            if "{pid}" not in override:
                return override
            if process_id is None:
                raise ConfigError(
                    f"device.commands.{which} uses {{pid}} but ospf_process_id is not set"
                )
            return override.replace("{pid}", process_id)
        pair = self.router if which == "router" else self.network
        if process_id is not None and pair.with_pid:
            return pair.with_pid.replace("{pid}", process_id)
        return pair.default


# ------------------------------------------------------------------ factories
def _genie_cisco(
    module: str, router_cls: str, network_cls: str
) -> Callable[[], LsdbAdapter]:
    def factory() -> LsdbAdapter:
        from src.vendors.genie_cisco import GenieCiscoAdapter

        return GenieCiscoAdapter(module, router_cls, network_cls)

    return factory


def _genie_junos() -> LsdbAdapter:
    from src.vendors.genie_junos import GenieJunosAdapter

    return GenieJunosAdapter()


def _arista() -> LsdbAdapter:
    from src.vendors.cisco_style import CiscoStyleTextAdapter

    return CiscoStyleTextAdapter(
        "Arista EOS",
        metric_hint="Use the detailed form via device.commands in settings.yaml, "
        "e.g. 'show ip ospf database router detail'.",
    )


def _frr() -> LsdbAdapter:
    from src.vendors.cisco_style import CiscoStyleTextAdapter

    return CiscoStyleTextAdapter("VyOS/FRR")


def _huawei() -> LsdbAdapter:
    from src.vendors.huawei import HuaweiAdapter

    return HuaweiAdapter()


# ------------------------------------------------------------------- registry
_IOS_ERRORS = (
    "Invalid input detected",
    "% Incomplete command",
    "% Ambiguous command",
    "% Unknown command",
    "%OSPF: ",
    "% OSPF: ",
)

_IOSXE = "genie.libs.parser.iosxe.show_ospf_database"
_IOSXR = "genie.libs.parser.iosxr.show_ospf"
_NXOS = "genie.libs.parser.nxos.show_ospf"

PROFILES: dict[str, VendorProfile] = {}


def register(profile: VendorProfile, *device_types: str) -> None:
    """Register ``profile`` under one or more Netmiko device types."""
    for device_type in device_types or (profile.key,):
        PROFILES[device_type] = profile


register(
    VendorProfile(
        key="cisco_ios",
        description="Cisco IOS / IOS-XE (Genie)",
        router=CommandPair(
            "show ip ospf database router", "show ip ospf {pid} database router"
        ),
        network=CommandPair(
            "show ip ospf database network", "show ip ospf {pid} database network"
        ),
        adapter_factory=_genie_cisco(
            _IOSXE, "ShowIpOspfDatabaseRouter", "ShowIpOspfDatabaseNetwork"
        ),
        error_markers=_IOS_ERRORS,
    ),
    "cisco_ios",
    "cisco_xe",
)
register(
    VendorProfile(
        key="cisco_xr",
        description="Cisco IOS-XR (Genie; default VRF; process filter applied after parsing)",
        router=CommandPair("show ospf vrf all-inclusive database router"),
        network=CommandPair("show ospf vrf all-inclusive database network"),
        adapter_factory=_genie_cisco(
            _IOSXR,
            "ShowOspfVrfAllInclusiveDatabaseRouter",
            "ShowOspfVrfAllInclusiveDatabaseNetwork",
        ),
        error_markers=_IOS_ERRORS,
    )
)
register(
    VendorProfile(
        key="cisco_nxos",
        description="Cisco NX-OS (Genie; default VRF)",
        router=CommandPair(
            "show ip ospf database router detail",
            "show ip ospf {pid} database router detail",
        ),
        network=CommandPair(
            "show ip ospf database network detail",
            "show ip ospf {pid} database network detail",
        ),
        adapter_factory=_genie_cisco(
            _NXOS, "ShowIpOspfDatabaseRouterDetail", "ShowIpOspfDatabaseNetworkDetail"
        ),
        error_markers=_IOS_ERRORS,
    )
)
register(
    VendorProfile(
        key="juniper_junos",
        description="Juniper Junos (Genie; one parse per area; no process ID)",
        router=CommandPair("show ospf database router extensive"),
        network=CommandPair("show ospf database network extensive"),
        adapter_factory=_genie_junos,
        error_markers=("syntax error", "unknown command", "error:"),
    )
)
register(
    VendorProfile(
        key="arista_eos",
        description="Arista EOS (text parser)",
        router=CommandPair(
            "show ip ospf database router", "show ip ospf {pid} database router"
        ),
        network=CommandPair(
            "show ip ospf database network", "show ip ospf {pid} database network"
        ),
        adapter_factory=_arista,
        error_markers=(
            "% Invalid input",
            "% Incomplete command",
            "% Ambiguous command",
        ),
    )
)
register(
    VendorProfile(
        key="vyos",
        description="VyOS / FRR (text parser; default instance)",
        router=CommandPair("show ip ospf database router"),
        network=CommandPair("show ip ospf database network"),
        adapter_factory=_frr,
        error_markers=(
            "Invalid command",
            "Unknown command",
            "Command incomplete",
            "% Unknown command",
            "% Command incomplete",
        ),
    )
)
register(
    VendorProfile(
        key="huawei",
        description="Huawei VRP (text parser)",
        router=CommandPair(
            "display ospf lsdb router", "display ospf {pid} lsdb router"
        ),
        network=CommandPair(
            "display ospf lsdb network", "display ospf {pid} lsdb network"
        ),
        adapter_factory=_huawei,
        error_markers=(
            "Error: Wrong parameter",
            "Error: Unrecognized command",
            "Error: Incomplete command",
        ),
    ),
    "huawei",
    "huawei_vrpv8",
)

#: Platforms that are known but deliberately not supported, with the reason.
UNSUPPORTED_HINTS = {
    "mikrotik_routeros": (
        "MikroTik RouterOS is not supported yet: its router/network LSA output ('/routing/ospf/lsa/print "
        "detail') is not documented well enough to parse without guessing. Capture a real sample with "
        "'--save-raw DIR' on a router that has the data and the parser can be added."
    ),
}


def supported_device_types() -> list[str]:
    """Sorted list of registered Netmiko device types."""
    return sorted(PROFILES)


def get_profile(device_type: str) -> VendorProfile:
    """Look up the profile for a device type.

    Raises:
        UnsupportedVendorError: With a specific explanation for known-unsupported platforms.
    """
    try:
        return PROFILES[device_type]
    except KeyError:
        hint = UNSUPPORTED_HINTS.get(device_type, "")
        raise UnsupportedVendorError(
            f"device_type '{device_type}' is not supported. Supported: {', '.join(supported_device_types())}. {hint}"
        ) from None
