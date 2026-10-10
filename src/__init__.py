"""
==============================================================================
Module Name:   __init__.py
Description:   Source module __init__.py.
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 __init__.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""

from __future__ import annotations

import ipaddress

__version__ = "1.0.0"

#: Transit-network nodes are keyed by the DR's interface address. A router-ID can
#: legitimately equal that same address (RID = highest interface IP when no
#: loopback exists), so networks get a prefix to keep the two ID spaces apart.
#: No colon is used on purpose: Graphviz would read it as a port separator.
NETWORK_ID_PREFIX = "net-"


class TrackerError(Exception):
    """Base class for expected, user-reportable failures in any pipeline stage."""


def network_node_id(address: str) -> str:
    """Return the graph node ID for a transit network whose DR interface is ``address``."""
    return f"{NETWORK_ID_PREFIX}{address}"


def node_sort_key(node_id: str) -> tuple:
    """Sort key giving a stable, human-friendly order: routers first, then networks,
    each in numeric IP order (non-IP IDs fall back to string order)."""
    text = str(node_id)
    kind = 0
    if text.startswith(NETWORK_ID_PREFIX):
        kind, text = 1, text[len(NETWORK_ID_PREFIX) :]
    try:
        return (kind, 0, int(ipaddress.ip_address(text)), "")
    except ValueError:
        return (kind, 1, 0, text)
