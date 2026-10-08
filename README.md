# OSPF LSDB Monitor

Poll a seed router over SSH, parse its full OSPF LSDB, build a directed graph, diff it against the previous run, and render a topology diagram — all in one command.

```
python main.py --replay samples/vyos
```

---

## Features

- **Multi-vendor** — Cisco IOS/IOS-XE/IOS-XR/NX-OS, Juniper JunOS, Arista EOS, VyOS/FRR, Huawei VRP
- **Live polling** over SSH (Netmiko) or **offline replay** from saved CLI output
- **Change detection** — added/removed nodes, added/removed links, metric changes
- **Partial-LSDB guard** — detects when the seed router has lost an adjacency and refuses to overwrite a good baseline with a degraded view
- **Atomic state rotation** — `current_state.json` → `previous_state.json`, crash-safe
- **Credential-safe** — passwords never have to touch disk; `OSPF_TRACKER_*` env vars override the config file

---

## Requirements

| Dependency | Version |
|---|---|
| Python | ≥ 3.10 |
| Graphviz (`dot`) | system package — see below |
| netmiko | ≥ 4.3, < 5 |
| pyATS / Genie | ≥ 24.1 |
| networkx | ≥ 3.0 |
| graphviz (Python) | ≥ 0.20 |
| PyYAML | ≥ 6.0 |

**Install system Graphviz first:**

```bash
# Debian / Ubuntu
sudo apt install graphviz

# macOS
brew install graphviz
```

> [!NOTE]
> pyATS/Genie is supported on **Linux and macOS** only. Use WSL2 on Windows.

---

## Installation

```bash
git clone https://github.com/duc-mt/ospf-lsdb-monitor.git
cd ospf-lsdb-monitor

python3 -m venv venv
source venv/bin/activate

# Install exact dependencies
pip install -r requirements.lock
# OR, to install loose dependencies: pip install -r requirements.txt
```

---

## Quick Start

### 1. Offline replay (no router needed)

Try any of the bundled vendor samples:

```bash
python main.py --replay samples/vyos
python main.py --replay samples/cisco_ios
python main.py --replay samples/arista_eos
python main.py --replay samples/juniper_junos
python main.py --replay samples/huawei
```

The topology diagram is written to `output/topology.png`.

### 2. Live polling

Edit `config/settings.yaml` (or set environment variables) and run:

```bash
python main.py
```

---

## Configuration

Copy and edit the defaults in [`config/settings.yaml`](config/settings.yaml):

```yaml
device:
  host: 10.255.255.6       # seed router management IP or hostname
  username: admin
  password: ""             # or leave blank and use env vars (see below)
  secret: ""               # enable secret — leave empty if already in privileged mode
  device_type: vyos        # see supported types below
  port: 22
  conn_timeout: 15
  auth_timeout: 20
  banner_timeout: 20
  read_timeout: 120        # large LSDBs can be slow

ospf_process_id: 1         # omit for platforms without process IDs (JunOS, VyOS)

guard:
  enabled: true
  min_node_retention: 0.7  # flag run if > 30% of known nodes vanish
  flag_partition: true      # flag run if the graph splits into more pieces than before
```

### Supported device types

| `device_type` | Platform |
|---|---|
| `cisco_ios` / `cisco_xe` | Cisco IOS / IOS-XE |
| `cisco_xr` | Cisco IOS-XR |
| `cisco_nxos` | Cisco NX-OS |
| `juniper_junos` | Juniper JunOS |
| `arista_eos` | Arista EOS |
| `vyos` | VyOS / FRR |
| `huawei` / `huawei_vrpv8` | Huawei VRP |

### Credentials via environment variables

These override anything in `settings.yaml` — use them in CI or to avoid storing passwords on disk:

```bash
export OSPF_MONITOR_USERNAME=admin
export OSPF_MONITOR_PASSWORD=s3cr3t
export OSPF_MONITOR_SECRET=enable_pass   # optional
python main.py
```

---

## CLI Reference

```
python main.py [OPTIONS]

Options:
  --config PATH         Settings file (default: config/settings.yaml)
  --device-type TYPE    Override device.device_type from the settings file
  --replay DIR          Parse saved CLI output instead of connecting via SSH
  --save-raw DIR        Also write the raw CLI output to DIR (replayable later)
  --output PATH         Diagram output path; extension sets format (png, svg, pdf, ...) (default: output/topology.png)
  --layout ENGINE       Graphviz layout engine: dot, neato, fdp, sfdp, circo, ... (default: dot)
  --accept-changes      Commit this run as the new baseline even if the guard flagged it
  --dry-run             Build and diff the topology without saving any state files
  -v, --verbose         Enable debug logging
  --version             Show version and exit
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success — baseline updated |
| `1` | Expected failure (config error, SSH failure, parse error, …) |
| `2` | Unexpected error |
| `3` | Run flagged as a possible partial LSDB — baseline kept; use `--accept-changes` to override |

---

## Architecture

```
Poll (SSH / file)
       │
       ▼
Parse (Genie / vendor text adapter)
       │  router-LSAs + network-LSAs → normalised dict
       ▼
Graph Engine (NetworkX)
       │  build DiGraph → diff vs baseline → partial-LSDB guard → atomic save
       ▼
Visualizer (Graphviz)
       │
       ▼
output/topology.png
```

State is kept in `data/`:

| File | Purpose |
|---|---|
| `current_state.json` | Topology from the last accepted run |
| `previous_state.json` | Topology from the run before that (for crash recovery) |
| `suspect_state.json` | Last run the guard rejected (not promoted to baseline) |
| `changelog.jsonl` | Append-only run history (one JSON line per run) |

---

## Capturing New Vendor Samples

Run against a live device and save the raw CLI output for future offline use:

```bash
python main.py --save-raw samples/my_router
# → writes samples/my_router/router_lsdb.txt
# → writes samples/my_router/network_lsdb.txt

# Replay it later (no SSH required):
python main.py --replay samples/my_router --device-type cisco_ios
```

---

## Running Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

---

## Project Structure

```
ospf-lsdb-monitor/
├── main.py                   # CLI entry point & pipeline orchestrator
├── config/
│   └── settings.yaml         # Device and guard configuration (template)
├── src/
│   ├── config.py             # Settings loading and validation
│   ├── poller.py             # SSH (DevicePoller) and file (FilePoller) data sources
│   ├── parser.py             # Dispatches to the right vendor adapter
│   ├── graph_engine.py       # NetworkX graph, diff, guard, state persistence
│   ├── visualizer.py         # Graphviz rendering
│   └── vendors/              # Per-platform LSDB parsers
│       ├── base.py           # VendorProfile base class
│       ├── cisco_style.py    # Cisco IOS/XE/XR/NX-OS + Arista (text)
│       ├── genie_cisco.py    # Cisco via pyATS/Genie
│       ├── genie_junos.py    # Juniper via pyATS/Genie
│       └── huawei.py         # Huawei VRP text parser
├── samples/                  # Bundled offline LSDB captures (one dir per vendor)
├── data/                     # Runtime state (git-ignored)
├── output/                   # Generated diagrams (git-ignored)
└── tests/                    # Unit tests
```

---

## License

MIT — see [LICENSE](LICENSE) if present, or contact the author.

## Author

**Duc Mai** · [ducmai.network@gmail.com](mailto:ducmai.network@gmail.com)
