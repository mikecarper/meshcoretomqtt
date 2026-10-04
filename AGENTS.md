# AGENTS.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

meshcoretomqtt is a Python bridge that reads serial data from MeshCore LoRa repeaters and publishes parsed packets/debug data to one or more MQTT brokers. It runs as a long-lived daemon, typically deployed as a systemd/launchd service, Docker container, or NixOS module.

**Environment note:** This software often runs on low-spec hardware (e.g. Raspberry Pi Zero) and at remote sites with low-bandwidth or high-latency connections. Network operations should use generous timeouts where appropriate.

## Running and Building

**Run locally (no build step):**
```bash
python3 mctomqtt.py --config config.toml.example          # requires pyserial and paho-mqtt
python3 mctomqtt.py --config config.toml.example --debug    # enables DEBUG-level logging
```

**Python dependencies:** `pyserial`, `paho-mqtt`, `ed25519-orlp` (install via pip or venv). Requires Python 3.11+ for `tomllib` stdlib. Note: `ed25519-orlp` may require a C toolchain and Python headers on Linux if a pre-compiled wheel is unavailable.

**Docker:**
```bash
docker build -t mctomqtt:latest .
docker run -d --name mctomqtt --device=/dev/ttyACM0 \
  --group-add="$(stat -c %g /path/to/mctomqtt-config)" \
  -v /path/to/mctomqtt-config:/etc/mctomqtt:ro \
  mctomqtt:latest
```

**NixOS:** `nix build` produces the default package. The flake also exports a NixOS module at `nixosModules.default`.

**Tests:** `python3 -m pytest tests/` (requires `pytest>=7.0`, declared in `pyproject.toml[project.optional-dependencies.test]`). GitHub Actions runs this on pull requests as a non-e2e test job. See the **Testing** section below for details.

## Architecture

The runtime codebase is a `bridge/` Python package with a thin entry point (project metadata in `pyproject.toml`):

- **`mctomqtt.py`** — Thin entry point (~45 lines). Keeps `__version__`, argparse, logging setup. Creates `MeshCoreBridge(config, debug, version)` and calls `bridge.run()`.

- **`bridge/`** — Python package containing all application logic, split into focused modules:
  - **`serial_connection.py`** - `SerialConnection` ABC + `RealSerialConnection` (one continuous bounded reader, command/log demultiplexing and deadline-aware command locking) + `connect()` factory
  - **`auth_provider.py`** — `AuthProvider` ABC + `MeshCoreAuthProvider` (wraps `auth_token.py`)
  - **`broker_client.py`** — `BrokerClient` ABC + `PahoBrokerClient` (wraps paho-mqtt)
  - **`state.py`** — `BridgeState` shared mutable state container (all ~30 instance variables)
  - **`topics.py`** — Topic resolution: `get_topic()`, `resolve_topic_template()`, `sanitize_client_id()`
  - **`mqtt_publish.py`** — `safe_publish()`, `build_status_message()`, `publish_status()`
    and confirmed `publish_shutdown_status()` with LWT fallback
  - **`message_parser.py`** — `RAW_PATTERN`, `PACKET_PATTERN`, `parse_and_publish()`
  - **`remote_serial.py`** — Remote serial command handling, nonce management, JWT validation
  - **`background.py`** - `stats_logging_loop()` and bounded queue diagnostics
  - **`mqtt_manager.py`** - `MqttManager` with one background lifecycle supervisor, generation-checked callbacks and persistent per-broker retry state
  - **`service_health.py`** - Optional systemd readiness/progress watchdog notifications, using only the standard library
  - **`runner.py`** — `run()` main loop, `handle_signal()`, `wait_for_system_time_sync()`
  - **`__init__.py`** — `MeshCoreBridge` facade class

- **`auth_token.py`** — Provides JWT operations (`create_auth_token`, `verify_auth_token`, `decode_token_payload`) using `ed25519-orlp`.

- **`config_loader.py`** — TOML config loading with layered override system.

### Testability

Three ABC boundaries (`SerialConnection`, `AuthProvider`, `BrokerClient`) allow full control over external dependencies in tests. Fakes are in `tests/fakes.py`:
- `FakeSerialConnection` — returns canned device values
- `FakeAuthProvider` — returns deterministic tokens, configurable verify/reject
- `FakeBrokerClient` — records published messages for assertion
- `make_test_state()` — factory for `BridgeState` with fake injection

## Configuration System

Configuration uses TOML files with a layered override system. Python 3.11+ `tomllib` is used (stdlib, no third-party dependency).

**Default config loading** (no `--config` flags):
1. `/etc/mctomqtt/config.toml` (base defaults, overwritten on updates)
2. `/etc/mctomqtt/config.d/10-*.toml` (broker presets selected during install)
3. `/etc/mctomqtt/config.d/99-user.toml` (local user overrides, loaded last)

**`--config` override:** When one or more `--config <path>` flags are provided, default config loading is completely bypassed. Only the specified files are loaded, in order, each overlaying the previous. Multiple `--config` flags are supported for layered overrides.

`MCTOMQTT_CONFIG_DIR` replaces the default base/drop-in directory when no
`--config` is supplied. Native custom-path services set it; Docker still
mounts and reads `/etc/mctomqtt` inside the container.

**Override mechanism:** Drop-in files are deep-merged over the base config. Nested dicts are merged recursively; `[[broker]]` arrays are merged by `name` field, including repeated names within one file. Later fields win without creating duplicate connections. Applying an override must not mutate its input.

**Key config sections:** `[general]`, `[serial]`, `[topics]`, `[remote_serial]`, `[update]`, `[[broker]]` with nested `[broker.tls]` and `[broker.auth]`.

**Broker auth methods:** `"password"` (username/password), `"token"` (JWT from device Ed25519 key), or `"none"`.

**Broker presets:** Community broker presets live in repo `presets/` as TOML files with `[[broker]]` blocks. The installer copies selected or imported presets to `/etc/mctomqtt/config.d/10-<preset>.toml`.

See `config.toml.example` for the full reference with all options and defaults.

## Directory Layout (System Install)

```
/opt/mctomqtt/              # App home (owned by mctomqtt:mctomqtt)
  mctomqtt.py
  auth_token.py
  config_loader.py
  bridge/                   # Application package
  .version_info
  venv/                     # Python venv (pyserial, paho-mqtt, ed25519-orlp)

/etc/mctomqtt/              # Config (owned root:mctomqtt, 750)
  config.toml               # Defaults (640, overwritten on updates)
  config.d/
    10-*.toml               # Broker presets
    99-user.toml            # User config (640, never overwritten)
```

## Key Patterns

- **Thread safety:** Serial port access is protected by internal locking in `RealSerialConnection`. The main loop, stats thread, and remote serial handler all call methods on the `SerialConnection` ABC — the lock is never exposed to callers.
- **Bounded capture:** Only the serial reader reads bytes. Commands must not purge RX buffers or consume packet records. Default line/queue bounds are 4096 bytes/256 records; whole-record drops report `DROP:<count>`. Writes and command-lock/reply waits have finite deadlines. A timed-out command closes the session to quarantine late replies.
  Preserve getter framing (`  >value` and `  -> >value`) before log
  classification, including replies following a leftover terminal prompt;
  split the reply prefix only once. Debug text may quote packet records and
  must not change the cached RAW pair. Private keys require
  exactly 128 hexadecimal digits after whitespace removal.
- **Bounded MQTT:** `PahoBrokerClient` reserves message/byte credits before publishing, including QoS 0. Public `MQTTMessageInfo` receipts reconcile callback-before-return races and MID reuse. Never assume Paho's queue limit covers QoS 0. Manager owns reconnects; Paho automatic reconnect is disabled. Remove no-op/manual WebSocket keepalive workers rather than reintroducing a shared stop flag.
  Publish exceptions may occur after Paho queues data; retain uncertain credits
  until completion or transport retirement rather than releasing them blindly.
- **Lifecycle/health:** Start/stop broker work through the manager supervisor, not the USB main loop. Retain retry counts across successful transport setup and reject stale-generation callbacks. Watchdog notifications require progress from both the main loop and supervisor; idle meshes remain healthy. DNS is not guaranteed cancellable. Keep systemd, Docker installer/updater, NixOS, tests and deployment docs aligned when changing resource defaults.
  Retire failed/unconfirmed transports before reconnect backoff and preserve
  their offline LWT; only confirmed final status permits graceful retirement.
- **MQTT auth:** Two modes per broker — username/password or JWT auth tokens (generated from device's Ed25519 private key). Tokens are cached with TTL. Auth operations go through the `AuthProvider` ABC.
  Validate 32-byte public keys and 64-byte signatures before calling the native
  verifier. Present expiry claims must be finite numbers and expire at `exp`.
  The token CLI accepts existing key files regardless of path length and
  normalizes whitespace in inline hexadecimal keys. Missing-file diagnostics
  must not echo ambiguous input that could be a truncated private key.
- **Graceful shutdown:** SIGTERM/SIGINT handlers set `state.should_exit = True`. The main loop checks this flag each iteration.
  The supervisor holds transports until explicit stop. Confirm final offline
  using the broker retain policy and a shared five-second budget; abort
  unconfirmed transports before DISCONNECT to preserve LWT. Periodic status
  remains non-retained. Serialize online initialization with the shutdown
  boundary, reject late CONNACK side effects and abort pending connections.
  Record confirmation per broker; confirmed sessions share a one-second
  graceful DISCONNECT window before forced retirement.
  Wake idle statistics workers through the shutdown event before joining;
  signal handlers continue to set only the exit flag.
  Device counter decreases indicate resets; a decreasing uptime invalidates
  the entire previous device baseline. Never report negative counter deltas.
- **Remote serial:** Require finite expiry, bounded single-line commands and
  native verified claims. Protect nonce pruning/check/reservation with one
  lock; retain until max(minimum TTL, JWT expiry), rejecting new commands when
  the bounded cache is full. Keep a captured serial session and route signed
  responses only to the requesting broker with its IATA. Nonces are not durable
  across restarts; do not claim persistent replay protection.
  Match the exact subscribed broker/node command topic and ignore commands
  during shutdown. Reject malformed remote configuration before starting work.
- **Installer safety:** Validate TOML before atomic private writes; config
  directories/files use 750/640 and Docker gets the host numeric config group.
  Directory selectors must be absolute and must not resolve to filesystem root.
  Persistent configuration must stay outside replaceable `bridge/` and `venv/`
  trees. Validate the complete layout before privileged work or file mutation.
  Detect brokers from parsed base/drop-in TOML and allocate unused custom
  broker names, so named overlays cannot replace an existing broker by accident.
  Owner and IATA edits operate on parsed tables and preserve unrelated values,
  including TOML date/time scalars; serialization normalizes formatting/comments.
  Stage the bridge package before retiring its destination; `LOCAL_INSTALL`
  may refer to the installation directory itself or an alias of it.
  Installer temporary directories and snapshots must stay outside both source
  and destination package trees and the replaceable venv, even when `TMPDIR`
  points inside them. Reject local source roots inside the installed package
  or venv before mutation; a source at the application root remains supported.
  Parse layered serial TOML for Docker mappings and include all present
  candidates; validate configuration before removing an existing container.
  Preserve bootstrap argv and explicitly supported environment selectors
  across sudo. Validate migration before retiring legacy services; refresh
  Docker recipes from selected sources. Nix serial candidates must not become
  mandatory device units or writable-path mounts.
  Ownership changes must operate on symlinks themselves, without following
  their targets; config mode changes must not affect linked external files.
  Reject linked config roots/drop-in directories before installation or update
  writes, rather than waiting for the final permission pass.
  Migration preserves effective legacy defaults plus local overrides, since
  new TOML defaults may differ. Uninstallation honors absolute path selectors
  and must not print credential-bearing configuration. Reject filesystem-root
  selectors and application directories without an installed entry point.
  Backups use unique mode-600 files; backup failure stops configuration
  removal. Read uninstaller prompts from the controlling terminal when piped,
  decline actions on EOF, and match exact Docker container/image names.
  If retained configuration is inside the application directory, preserve that
  directory too and report which files remain.
- **Config access:** `state.config` dict with `state.config.get('section', {}).get('key', default)`. Broker configs accessed via `topics.get_broker_config(state, broker_idx)`.
- **Version:** `__version__` is defined at the top of `mctomqtt.py`. The `.version_info` JSON file (created by installer) appends git hash info. Version is passed to `MeshCoreBridge(config, debug, version)`.
- **Dependency injection:** All external dependencies (serial, MQTT, auth) are abstracted behind ABCs. Tests inject fakes via `make_test_state()` from `tests/fakes.py`.
- **Installer file operations:** Since the installer runs as root, use Python stdlib directly — `os.makedirs()`, `shutil.copy2()`, `os.chmod()`, `shutil.chown()`, `Path.write_text()`, `shutil.rmtree()`, `Path.unlink()`. Never shell out for file operations. Reserve `run_cmd()` for external tools with no Python equivalent (systemctl, docker, useradd, pip, etc.). All subprocess commands use list form (never `shell=True`).

## Development Guidelines

- **No mocks in tests** unless explicitly directed. Prefer extracting testable functions and testing them with real files (e.g., `tmp_path`). Mocks hide bugs and make tests brittle.
- **Every code change must include a pass on documentation and tests.** Update AGENTS.md, README.md, config.toml.example, and other relevant docs when behavior changes. Add or update tests to cover the change.

## Testing

**Run:** `python3 -m pytest tests/` (or `pytest tests/`). Config is in `pyproject.toml`.

**Test tiers** (via pytest markers):
- **Default (no marker):** Pure-logic unit tests — validation, TOML generation, env parsing, config files, context. Always run, no dependencies.
- **`@pytest.mark.network`:** Tests needing internet (IATA API, download, bootstrap `--help`). Run by default; skip with `MCTOMQTT_SKIP_NETWORK=1`.
- **`@pytest.mark.system`:** Tests needing root + Linux (permissions, service user creation, systemd). Auto-skipped when not root; also skip with `MCTOMQTT_SKIP_SYSTEM=1`.
- **`@pytest.mark.e2e`:** Tests needing real services/devices. Opt-in only: `MCTOMQTT_TEST_E2E=1`.

The NixOS VM check in `nix/nixos-test.nix` uses the production `ServiceHealth`
notifier to exercise readiness and watchdog-compatible unit settings. It is
not an end-to-end radio/MQTT test. Keep its IATA and restart assertions aligned
with the NixOS module.

**PR CI:** `.github/workflows/pr-tests.yaml` runs on `pull_request` and executes `python -m pytest tests/ -m "not e2e"` on Ubuntu. This includes the default unit tests plus any network/system tests that are functional in the GitHub runner, while still excluding opt-in e2e coverage that needs real services or devices.

**Conventions:**
- Test files mirror the module they test (e.g., `test_validation.py` tests `installer/config.py` validation helpers).
- Installer flow tests (`test_install_flow.py`, `test_update_flow.py`, `test_migrate_flow.py`) use `unittest.mock.patch` to stub interactive prompts and subprocess calls.
- Shell script syntax is validated via `bash -n` in `test_bash_bootstrap.py`.

## Deployment

### Installer Architecture

The installer is a Python package (`installer/`) with thin bash bootstraps. Python 3.11+ stdlib only (no pip dependencies for the installer itself).

**Privilege model:** The installer runs as root. Bash bootstraps capture the
original argv before parsing and escalate with `sudo env` forwarding only
supported selectors, then invoke Bash with that original argv. Directory
selectors must be absolute. The Python entry point validates those selectors
before `require_root()` and dispatch. File operations use Python stdlib
directly, not sudo wrappers; TOML uses validated atomic private replacement.
Service-user commands use `sudo -u <svc_user>` to drop privileges.

**Bash bootstraps** (download the Python package and dispatch):
- **`install.sh`** — Runs `python3 -m installer install` (fresh install or update detection)
- **`scripts/update.sh`** — Runs `python3 -m installer update` (standalone update, reads repo/branch from existing config)
- **`scripts/migrate.sh`** — Runs `python3 -m installer migrate` (standalone migration from `~/.meshcoretomqtt`)

**Python installer modules** (`installer/`):
- **`__init__.py`** — `InstallerContext` dataclass (shared state: repo, branch, install_dir, config_dir, svc_user, etc.)
- **`__main__.py`** — argparse entry point with `install`, `update`, `migrate` subcommands; calls `require_root()` after arg parsing
- **`ui.py`** — ANSI color output (auto-detects TTY), prompts via `/dev/tty` (works with `curl | bash`)
- **`system.py`** — `run_cmd()` subprocess wrapper (for external tools only: systemctl, docker, useradd, etc.), `require_root()`, `chown_recursive()`, user management, service management (systemd/launchd/Docker), serial device detection, venv setup. Note: Set `INSTALL_REBUILD_VENV=1` to force a full virtual environment rebuild.
- **`config.py`** — Validation (email, pubkey, IATA), TOML generation, IATA API search via `urllib.request` + `json` (no jq dependency), interactive MQTT broker configuration flows
- **`install_cmd.py`** — Fresh install orchestration (delegates to migrate/update when appropriate)
- **`update_cmd.py`** — Update existing installation (file download, dependency refresh, config preservation, service restart)
- **`migrate_cmd.py`** — Legacy `.env`/`.env.local` to TOML conversion, old service cleanup

### Other Deployment Files

- **`pyproject.toml`** — Project metadata, Python version requirement (>=3.11), and pytest configuration.
- **`uninstall.sh`** — Interactive uninstaller that detects the service user from the systemd unit, stops/removes the service, offers config backup, and cleans up `/opt/mctomqtt/` and `/etc/mctomqtt/`.
- **`Dockerfile`** - Alpine build that includes all Python dependencies. Config mounted at `/etc/mctomqtt/config.toml`. Only upstream/main remote installs pull Cisien's registry image; fork, custom-branch and local-source installs/updates build selected sources instead.
- **`mctomqtt.service`** — systemd unit template with security hardening (NoNewPrivileges, ProtectSystem, PrivateTmp).
- **`com.meshcore.mctomqtt.plist`** — macOS launchd plist for system-level daemon at `/Library/LaunchDaemons/`.
- **`configs/`** — User-contributed configuration examples.
- **`nix/`** — Nix flake with package definition (`packages.nix`), NixOS module (`nixos-module.nix`) that generates TOML config via `pkgs.formats.toml`, dev shell, and NixOS integration test.
