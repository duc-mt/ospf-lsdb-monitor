from __future__ import annotations

"""
==============================================================================
Module Name:   test_poller.py
Description:   Tests for the polling and configuration layer.  These tests cover: - ``OSPF_MONITOR_*`` environment variable overrides (#3 fix) - ``DevicePoller`` validation of missing / empty credentials - ``FilePoller`` replay - ``GraphEngine.process(dry_run=True)`` (#8 fix) - ``GraphEngine._append_changelog()`` (#11 fix)
Author:        Mai Tan Duc <ducmai.network@gmail.com>
Created:       2026-10-10
Version:       1.0.0
License:       MIT
==============================================================================
Usage:         python3 test_poller.py [options]
Notes:         Requires Python 3.8+
==============================================================================
"""


import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.config import ConfigError
from src.poller import DevicePoller, FilePoller

# ------------------------------------------------------------------ helpers
_DEVICE = {
    "host": "10.0.0.1",
    "username": "admin",
    "password": "secret",
    "device_type": "vyos",
}

_SETTINGS = {"device": _DEVICE}


def _write_config(tmp_path: Path, settings: dict) -> Path:
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.dump(settings))
    return cfg


# ------------------------------------------------ env-var credential override
def test_env_username_overrides_yaml(tmp_path):
    cfg = _write_config(tmp_path, _SETTINGS)
    with patch.dict(os.environ, {"OSPF_MONITOR_USERNAME": "env_admin"}):
        poller = DevicePoller(cfg)
    assert poller._username == "env_admin"


def test_env_password_overrides_yaml(tmp_path):
    cfg = _write_config(tmp_path, _SETTINGS)
    with patch.dict(os.environ, {"OSPF_MONITOR_PASSWORD": "env_pass"}):
        poller = DevicePoller(cfg)
    assert poller._password == "env_pass"


def test_env_secret_overrides_yaml(tmp_path):
    cfg = _write_config(tmp_path, _SETTINGS)
    with patch.dict(os.environ, {"OSPF_MONITOR_SECRET": "env_secret"}):
        poller = DevicePoller(cfg)
    assert poller._secret == "env_secret"


def test_env_vars_take_precedence_over_yaml_credentials(tmp_path):
    """All three env vars at once."""
    cfg = _write_config(tmp_path, _SETTINGS)
    env = {
        "OSPF_MONITOR_USERNAME": "u",
        "OSPF_MONITOR_PASSWORD": "p",
        "OSPF_MONITOR_SECRET": "s",
    }
    with patch.dict(os.environ, env):
        poller = DevicePoller(cfg)
    assert (poller._username, poller._password, poller._secret) == ("u", "p", "s")


# ----------------------------------------------- DevicePoller validation
def test_missing_host_raises_config_error(tmp_path):
    cfg = _write_config(tmp_path, {"device": {**_DEVICE, "host": ""}})
    with pytest.raises(ConfigError, match="missing"):
        DevicePoller(cfg)


def test_missing_password_raises_config_error(tmp_path):
    cfg = _write_config(tmp_path, {"device": {**_DEVICE, "password": ""}})
    with pytest.raises(ConfigError, match="missing"):
        DevicePoller(cfg)


def test_missing_username_raises_config_error(tmp_path):
    cfg = _write_config(tmp_path, {"device": {**_DEVICE, "username": ""}})
    with pytest.raises(ConfigError, match="missing"):
        DevicePoller(cfg)


def test_no_device_section_raises_config_error(tmp_path):
    cfg = _write_config(tmp_path, {})
    with pytest.raises(ConfigError, match="device"):
        DevicePoller(cfg)


def test_device_type_override_is_applied(tmp_path):
    cfg = _write_config(tmp_path, _SETTINGS)
    poller = DevicePoller(cfg, device_type="cisco_ios")
    assert poller.device_type == "cisco_ios"


# ------------------------------------------------- FilePoller replay
def test_file_poller_reads_both_files(tmp_path):
    (tmp_path / "router_lsdb.txt").write_text("router output")
    (tmp_path / "network_lsdb.txt").write_text("network output")
    raw = FilePoller(tmp_path, device_type="vyos").poll()
    assert raw.router == "router output"
    assert raw.network == "network output"
    assert raw.source == str(tmp_path)


def test_file_poller_missing_network_file_is_ok(tmp_path):
    (tmp_path / "router_lsdb.txt").write_text("router output")
    raw = FilePoller(tmp_path).poll()
    assert raw.network == ""


def test_file_poller_missing_router_file_raises(tmp_path):
    from src.poller import PollerError

    with pytest.raises(PollerError):
        FilePoller(tmp_path).poll()


# ----------------------------------------- GraphEngine dry_run + changelog
def _topology() -> dict:
    ids = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    return {
        "metadata": {},
        "nodes": [
            {"id": r, "type": "router", "areas": ["0.0.0.0"], "resolved": True}
            for r in ids
        ],
        "edges": [
            {
                "source": "10.0.0.1",
                "target": "10.0.0.2",
                "metric": 10,
                "link_type": "point-to-point",
                "area": "0.0.0.0",
            },
            {
                "source": "10.0.0.2",
                "target": "10.0.0.1",
                "metric": 10,
                "link_type": "point-to-point",
                "area": "0.0.0.0",
            },
        ],
    }


def test_dry_run_does_not_write_state_files(tmp_path):
    from src.graph_engine import GraphEngine

    engine = GraphEngine(tmp_path)
    _, diff = engine.process(_topology(), dry_run=True)
    assert not diff.baseline_updated
    assert not engine.current_path.exists()
    assert not engine.previous_path.exists()


def test_dry_run_still_returns_correct_diff(tmp_path):
    from src.graph_engine import GraphEngine

    engine = GraphEngine(tmp_path)
    engine.process(_topology())  # establish baseline
    modified = dict(_topology())
    modified["nodes"] = modified["nodes"][:2]  # remove one node
    _, diff = engine.process(modified, dry_run=True)
    assert diff.baseline_available
    assert len(diff.removed_nodes) == 1
    # baseline must be unchanged
    assert engine.current_path.exists()
    saved = json.loads(engine.current_path.read_text())
    assert len(saved["nodes"]) == 3  # original untouched


def test_dry_run_does_not_write_changelog(tmp_path):
    from src.graph_engine import GraphEngine

    engine = GraphEngine(tmp_path)
    engine.process(_topology(), dry_run=True)
    assert not engine.changelog_path.exists()


def test_normal_run_appends_changelog(tmp_path):
    from src.graph_engine import GraphEngine

    engine = GraphEngine(tmp_path)
    engine.process(_topology())
    engine.process(_topology())
    lines = engine.changelog_path.read_text().splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["status"] == "ok"
    assert entry["nodes"] == 3
    assert "timestamp" in entry


def test_suspect_run_appends_changelog_with_suspect_status(tmp_path):
    from src.graph_engine import GraphEngine, GuardConfig

    engine = GraphEngine(tmp_path, GuardConfig(min_node_retention=0.9))
    engine.process(_topology())
    # Remove 2 of 3 nodes → retention 33% < 90% → suspect
    shrunken = {**_topology(), "nodes": [_topology()["nodes"][0]], "edges": []}
    engine.process(shrunken)
    lines = engine.changelog_path.read_text().splitlines()
    entries = [json.loads(l) for l in lines]
    statuses = [e["status"] for e in entries]
    assert "suspect" in statuses
