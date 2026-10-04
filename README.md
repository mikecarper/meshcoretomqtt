# meshcoretomqtt

A Python-based script to send MeshCore debug and packet capture data to MQTT for
analysis. Requires a MeshCore repeater to be connected to a Raspberry Pi,
server, or similar device running Python.

The goal is to have multiple repeaters logging data to the same MQTT server so
you can easily troubleshoot packets through the mesh. Your repeater firmware
must support USB packet logging. Some builds enable it at runtime; others need
a custom image with packet logging compiled in.

The parser supports the current MeshCore packet log variants from `main`,
including repeater packet logs without `hash=` and dispatcher/serial logs that
include `time=` and `hash=`.

One way of tracking a message through the mesh is filtering the MQTT data on the
`hash` field when the source log format includes it. Dispatcher/serial packet
logs include `hash=...`, while current repeater packet logs may not.

## Quick Install

### One-Line Installation (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash
```

The installer will:

- Create a dedicated `mctomqtt` system user
- Install to `/opt/mctomqtt/` with config at `/etc/mctomqtt/`
- Guide you through interactive MQTT broker configuration
- Set up Python virtual environment (requires Python 3.11+)
- Configure a systemd service (Linux) or launchd daemon (macOS)
- Auto-detect and migrate existing `~/.meshcoretomqtt` installations

### Custom Repository/Branch

Install from a fork or custom branch:

```bash
curl -fsSL https://raw.githubusercontent.com/yourusername/meshcoretomqtt/yourbranch/install.sh | \
  sudo bash -s -- --repo yourusername/meshcoretomqtt --branch yourbranch
```

Bootstraps preserve the original arguments when invoking sudo themselves.
Supported repository, branch, local-source and installation/configuration
directory environment selectors are explicitly forwarded, not the entire
caller environment. Installation/configuration directory selectors require
absolute paths. Native services use `MCTOMQTT_CONFIG_DIR` to find the base
configuration and sorted `config.d` overlays in a custom directory. Unset or
empty values default to `/etc/mctomqtt`. Explicit `--config` files bypass both
that environment setting and the default directories.

### Local Testing

```bash
git clone https://github.com/Cisien/meshcoretomqtt
cd meshcoretomqtt
sudo LOCAL_INSTALL=$(pwd) ./install.sh
```

### NixOS

configuration.nix:

```nix
{ config, pkgs, meshcoretomqtt, ... }:
```

flake.nix

```nix
inputs = {
  meshcoretomqtt.url = "github:Cisien/meshcoretomqtt"
};
```

in your system config

```nix
imports = [inputs.meshcoretomqtt.nixosModules.default];
services.mctomqtt = {
    enable = true;
    iata = "FOO";
    serialPorts = ["/dev/ttyUSB0"];

    # Disable defaults if you like.
    # Defaults are used if nothing is specified
    defaults = {
        letsmesh-us.enable = true;
        letsmesh-eu.enable = true;
    };

    # Define custom brokers if you need them
    brokers = [
      {
        name = "my-broker";
        enabled = true;
        server = "mqtt.example.com";
        port = 1883;
        tls.enabled = true;
        auth = {
          method = "password";
          username = "my_username";
          password = "my_password";
        };
      }
    ];

    # Additional settings
    settings = {
      log-level = "DEBUG";
    };
  };
```

`serialPorts` are optional candidates, not mandatory systemd device
dependencies. Missing fallback ports or by-id paths do not prevent startup;
the application owns serial selection and reconnects.

## Prerequisites

### Hardware Setup

1. Setup a Raspberry Pi (Zero / 2 / 3 or 4 recommended) or similar Linux/macOS
   device
2. Enable USB packet logging on a MeshCore repeater. If its CLI supports
   `set usb.logging on`, use that saved runtime setting on the normal artifact.
   Builds offering `set logging.output usb` can select USB forwarding without
   also publishing directly over WiFi. Do not simultaneously run a Binary
   Companion client or another serial logger on the same port.

   Otherwise, build/flash a repeater with appropriate build flags:

   **Recommended minimum:**
   ```
   -D MESH_PACKET_LOGGING=1
   ```

   **Optional debug data:**
   ```
   -D MESH_DEBUG=1
   ```

3. Plug the repeater into the device via USB (RAK or Heltec tested)
4. Configure the repeater with a unique name as per MeshCore guides

On Linux, prefer the radio's `/dev/serial/by-id/...` path in `[serial].ports`
instead of a renumberable `/dev/ttyACM0`. The bridge requests exclusive serial
ownership on POSIX systems. Keep firmware debug output and bridge DEBUG logging
off unless troubleshooting; packet logging does not require debug spam.

### Software Requirements

- Python 3.11 or higher (required for `tomllib` stdlib module)

The installer handles these dependencies automatically!

If a pre-compiled wheel for `ed25519-orlp` is not available for
your platform, a C toolchain (gcc/make) and Python development
headers are required for installation. The installer will attempt
to detect and optionally install these for you.

## Testing

Run the local test suite with:

```bash
python3 -m pytest tests/
```

Test tiers:

- Default tests include unit tests and isolated local PTY/MQTT/file tests.
- Network tests are skipped with `MCTOMQTT_SKIP_NETWORK=1`.
- System tests are skipped with `MCTOMQTT_SKIP_SYSTEM=1`.
- End-to-end tests are opt-in with `MCTOMQTT_TEST_E2E=1`.

Pull requests automatically run the GitHub Actions test workflow on Ubuntu. That job executes `python -m pytest tests/ -m "not e2e"`, which includes the normal unit suite plus any network/system tests that work in the runner, while still excluding opt-in e2e coverage that requires real services or devices.

The NixOS VM check verifies generated configuration and service settings using
the production readiness/watchdog notifier. It does not exercise radio capture
or live MQTT transport.

## Directory Layout

```
/opt/mctomqtt/              # App home (owned by mctomqtt:mctomqtt)
  mctomqtt.py               # Entry point
  bridge/                   # Core bridge package
  auth_token.py
  config_loader.py
  .version_info
  venv/                     # Python venv (pyserial, paho-mqtt, ed25519-orlp)

/etc/mctomqtt/              # Config (owned root:mctomqtt, 750)
  config.toml               # Defaults (640, OVERWRITTEN on updates)
  config.d/                 # Drop-in override directory (750)
    10-letsmesh.toml        # Optional broker preset selected during install
    99-user.toml            # User config (640, never overwritten)
```

## Configuration

Configuration uses TOML files with a layered override system:

- `/etc/mctomqtt/config.toml` — Default values (overwritten on updates, do not edit)
- `/etc/mctomqtt/config.d/10-*.toml` — Broker presets selected during install
- `/etc/mctomqtt/config.d/99-user.toml` — Your custom configuration (never overwritten)

Files in `config.d/` are loaded alphabetically and deep-merged over the defaults.
The installer writes presets with a `10-` prefix and user overrides to
`99-user.toml` so local settings load last.
Named broker blocks merge recursively in encounter order, including repeated
names within one file. Later values override earlier values while preserving
other fields; each name creates one broker connection.

To bypass the default config loading entirely, use `--config`:

```bash
mctomqtt.py --config /path/to/config.toml
mctomqtt.py --config /path/to/base.toml --config /path/to/overrides.toml
```

When `--config` is used, `/etc/mctomqtt/` is not read. Multiple `--config` flags are supported; files are loaded in order with later files overlaying earlier ones.

### Editing Configuration

```bash
sudo nano /etc/mctomqtt/config.d/99-user.toml
```

### Broker Presets

The installer can copy broker presets from the repo `presets/` directory or
import a preset from a TOML URL/local path. Selected presets are installed as
`/etc/mctomqtt/config.d/10-<preset>.toml`; user-specific overrides remain in
`99-user.toml`.

### Basic Example (99-user.toml)

```toml
[general]
iata = "SEA"

[serial]
ports = ["/dev/ttyACM0"]

[[broker]]
name = "my-mqtt"
enabled = true
server = "mqtt.example.com"
port = 1883

[broker.auth]
method = "password"
username = "my_username"
password = "my_password"
```

### Advanced Example with Multiple Brokers

```toml
[general]
iata = "SEA"

[serial]
ports = ["/dev/ttyACM0"]

# Local MQTT with Username/Password
[[broker]]
name = "local-mqtt"
enabled = true
server = "mqtt.local"
port = 1883

[broker.auth]
method = "password"
username = "localuser"
password = "localpass"

# LetsMesh.net Packet Analyzer (US)
[[broker]]
name = "letsmesh-us"
enabled = true
server = "mqtt-us-v1.letsmesh.net"
port = 443
transport = "websockets"

[broker.tls]
enabled = true

[broker.auth]
method = "token"
audience = "mqtt-us-v1.letsmesh.net"
```

### Topic Templates

Topics support template variables:

- `{IATA}` — Your 3-letter location code
- `{PUBLIC_KEY}` — Device public key (auto-detected)

**Global topics** (in config.toml defaults):

```toml
[topics]
status = "meshcore/{IATA}/{PUBLIC_KEY}/status"
packets = "meshcore/{IATA}/{PUBLIC_KEY}/packets"
debug = "meshcore/{IATA}/{PUBLIC_KEY}/debug"
```

**Per-broker topic overrides** (optional):

```toml
[[broker]]
name = "custom-broker"
enabled = true
server = "mqtt.example.com"

[broker.topics]
status = "custom/{IATA}/{PUBLIC_KEY}/status"
iata = "LAX"
```

## Authentication Methods

### 1. Username/Password

```toml
[[broker]]
name = "my-broker"
enabled = true
server = "mqtt.example.com"

[broker.auth]
method = "password"
username = "your_username"
password = "your_password"
```

### 2. Auth Token (Public Key Based)

Requires firmware supporting the `get prv.key` command (v1.8.0 or later).

```toml
[[broker]]
name = "letsmesh-us"
enabled = true
server = "mqtt-us-v1.letsmesh.net"
port = 443
transport = "websockets"

[broker.tls]
enabled = true

[broker.auth]
method = "token"
audience = "mqtt-us-v1.letsmesh.net"
```

The script will:

- Read the private key from the connected MeshCore device via serial
- Generate JWT auth tokens using the device's private key
- Authenticate using the `v1_{PUBLIC_KEY}` username format

**Note:** The private key is read directly from the device and used for signing
only. It's never transmitted or saved to disk.


### Additional Settings

```toml
[general]
# Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL
log_level = "INFO"

# Wait for system clock sync before setting repeater time (default: true)
# Set to false on systems without timedatectl or NTP
sync_time = true
```

### Remote Serial (LetsMesh.net Experimental)

Remote serial allows you to execute serial commands on your node remotely via
the LetsMesh.net MeshCore Packet Analyzer web interface. Commands are cryptographically
signed by an authorized companion device connected via Bluetooth.

**Security Model:**
- Commands must be signed with an Ed25519 private key
- Only companions in the allowlist can send commands
- A finite command expiry is required (the web interface normally uses 30 seconds)
- Nonces are claimed atomically across MQTT workers and kept until at least JWT expiry
- Responses are signed by the node's private key and sent only to the requesting broker

**Configuration:**

```toml
[remote_serial]
enabled = true
allowed_companions = [
    "03CEBEA3DA9C279CF8EB9449F0CC5BA3690621EE66A3B91067CDBA881EC883A5"
]
nonce_ttl = 120
max_pending_nonces = 4096
command_timeout = 10
```

**How it works:**
1. You connect your companion device via Bluetooth to the Packet Analyzer web interface
2. The browser uses the companion's private key to sign command JWTs
3. Commands are sent via MQTT to your node's `serial/commands` topic
4. This script verifies the JWT signature against the allowlist
5. Valid commands are executed on the serial port
6. Responses are signed and published to the `serial/responses` topic

**Note:** Ensure your system clock is synchronized (NTP) for JWT expiry verification.

`nonce_ttl` is minimum retention, not a limit on token lifetime. The replay
cache retains longer-lived tokens through their expiry; when the bounded cache
is full it rejects new commands instead of evicting live nonces. Commands must
be single nonempty lines within `serial.max_line_bytes` including the CRLF.
Responses and subscriptions use the source broker's IATA override. Execution
keeps one serial-session reference across USB reconnects. An accepted nonce
stays consumed even if execution fails or the response cannot be delivered;
retry with a freshly signed nonce. This cache is in-memory, not persistent
across service restarts; keep token lifetimes short.

## Running the Script

The installer offers three deployment options:

### 1. System Service (Recommended)

Automatically starts on boot and runs as a dedicated system user.

**Linux (systemd):**

```bash
sudo systemctl start mctomqtt      # Start service
sudo systemctl stop mctomqtt       # Stop service
sudo systemctl status mctomqtt     # Check status
sudo systemctl restart mctomqtt    # Restart service
sudo journalctl -u mctomqtt -f     # View logs
```

**macOS (launchd):**

```bash
sudo launchctl load /Library/LaunchDaemons/com.meshcore.mctomqtt.plist
sudo launchctl unload /Library/LaunchDaemons/com.meshcore.mctomqtt.plist
sudo launchctl list | grep mctomqtt
tail -f /var/log/mctomqtt.log
```

### 2. Docker Container

```bash
# Build the image
docker build -t mctomqtt:latest /path/to/meshcoretomqtt

# Run the container
docker run -d \
  --name mctomqtt \
  --restart unless-stopped \
  --memory=256m --memory-swap=256m --pids-limit=64 \
  --log-driver=json-file --log-opt=max-size=10m --log-opt=max-file=3 \
  --group-add="$(stat -c %g /path/to/mctomqtt-config)" \
  -v /path/to/mctomqtt-config:/etc/mctomqtt:ro \
  --device=/dev/ttyACM0 \
  mctomqtt:latest
```

These limits are also used by the installer and Docker updater. An existing
container must be recreated to adopt them. Docker's restart policy handles
exits, not a hung-but-running process; the systemd progress watchdog described
below is not enabled merely by running the same image in Docker.
Docker also pins the device passed with `--device`: if USB re-enumeration
changes its device number, recreate the container with the current device.
A persistent `/dev/serial/by-id/...` host path alone does not update that
container mapping. Do not use privileged mode just to work around this.
Fork, non-main branch and local-source installations build the selected source
instead of pulling Cisien's upstream `latest` image. Upstream/main installations
still try that published image first, with a local build fallback.
Local builds refresh the Dockerfile from the selected sources rather than
reusing a stale installation recipe. Config directories are `750` and TOML
files `640`; the installer/updater adds the host configuration directory's
numeric group to the non-root container. Manual Linux runs need the same
`--group-add` (shown above). The container always reads `/etc/mctomqtt`, not a
host-specific `MCTOMQTT_CONFIG_DIR` path.
The installer and updater parse the base and sorted drop-ins as TOML and map
every configured serial candidate present on the host when the container is
created. Missing candidates are skipped. A device added later still requires
recreating the container to add its mapping.

### 3. Manual Execution

```bash
cd /opt/mctomqtt
sudo -u mctomqtt ./venv/bin/python3 mctomqtt.py
```

With debug output:

```bash
sudo -u mctomqtt ./venv/bin/python3 mctomqtt.py --debug
```

These commands load the base plus user overlays. For a custom configuration
directory, use `sudo -u mctomqtt env MCTOMQTT_CONFIG_DIR=/absolute/config/path`
before the Python command. Use explicit `--config` only when intentionally
bypassing the normal layered configuration.

## USB and Host Reliability

USB logging remains a best-effort capture, not durable storage. A dedicated
reader keeps draining the radio while DNS, TLS or MQTT reconnects are slow.
Commands and packet logs are demultiplexed by that one reader; stats queries no
longer purge pending packets. POSIX serial opens request exclusive ownership.
Statistics polling retains one session reference during reconnect and ignores
results from retired sessions, so a disconnected port cannot stop the worker
or replace current statistics with a late reply.

The defaults bound unfinished lines to 4096 bytes, queued log records to 256,
and complete command responses to 64 KiB. Lines without a terminator are
discarded after 5 seconds, through the next newline. Queue overflow drops
oldest whole records and emits `DROP:<count>`. A drop, invalid RAW record,
mismatched timestamp/length or expired pairing clears the cached RAW payload;
a valid RAW record is consumed by only one packet summary. A summary can
therefore legitimately contain `"raw": null` after capture loss. Matching
timestamps and lengths are safeguards, not unique packet identifiers.

Serial writes have a 2-second timeout. CLI query deadlines default to 10
seconds and include time spent waiting for command ownership and writing.
Full Companion's bare ASCII replies also work when its unframed `> ` prompt
is immediately followed by a packet/debug log: the prompt ends the reply and
the following log stays in the capture queue. A prompt before the first reply
does not complete the command.
Getter replies support both Full Companion's indented `  > value` and legacy
`  -> >value` framing, so names containing `DEBUG`, `BLE:`, `RAW:` or `RX,`
remain values. Complete names and radio values survive embedded `-> >` text.
Fragmented prompt/getter prefixes survive quiet reader polls. Private keys
must contain exactly 128 hexadecimal digits after whitespace removal.
Malformed stats replies and fields are ignored without discarding other valid
stats. Stats must be JSON objects containing finite, float-representable numeric values, not strings
or booleans. Negative noise measurements are valid; counters, airtime and
battery readings must be nonnegative.

After a write/reply timeout the serial session is closed, since this ASCII
protocol has no transaction IDs with which to identify a late response. The
main loop scans configured candidates, closing wrong or unverifiable radios
and continuing until one matches the startup public key. Initial startup has
no expected identity and still chooses the first usable candidate, so order
ports deliberately. `serial.watchdog_timeout = 0` really disables the
idle reconnect watchdog; quiet meshes are not unhealthy merely because they
have no packets.

Each broker is bounded to 256 outstanding publications and 256 KiB of
UTF-8 topic/payload data plus conservative wire overhead, including QoS 0.
Offline or full brokers drop new publications instead of accumulating an
unlimited backlog. Accepted is not the same as delivered: QoS 0 completion
means handed to the socket, not acknowledged by the broker. A publication
pending for 120 seconds causes that connection to be retired. These defaults
can be tuned with the serial/broker options in `config.toml.example`.

Each broker uses its own client-ID prefix. IDs over the portable 23-character
limit include a 64-bit digest of the full prefix, node identity and broker suffix;
long prefixes cannot truncate away the identity. Existing long IDs change
once on upgrade. Online/LWT and final offline status follow the broker's
`retain` policy; periodic stats status remains non-retained. Shutdown confirms
offline with QoS 1/2 before closing, sharing a five-second confirmation budget
across brokers. If confirmation fails, the transport aborts without a graceful
DISCONNECT so the broker's offline LWT remains the fallback. Cleanup waits for
any online initialization already in progress and rejects late connection
callbacks, preventing online status from overwriting the final offline status.
Confirmed sessions share another one-second budget to send DISCONNECT, keeping
the final stats and timestamp from being replaced by an older Last Will.
Unconfirmed connection attempts also abort to preserve their wills. A custom broker
client without confirmation/abort support cannot provide that guarantee.

One supervisor owns broker reconnects, with persistent per-broker backoff and
failure counts. MQTT authentication rejection, missing CONNACK and short-lived
connections count as failures even after a successful transport handshake.
Counters reset only after 120 seconds of stable connection; 12 consecutive
failures request a service restart. Standard MQTT keepalive also works over
WebSockets; no extra WebSocket ping threads are created. TCP setup has a
30-second timeout, but DNS resolution and TLS do not have a guaranteed total
30-second deadline.

Outage/backpressure warnings are rate-limited to once per broker per 30
seconds; dropped-publication counters and periodic queue statistics still
show pressure. Leave the bridge at INFO for normal operation.

The supplied Linux systemd unit and NixOS service use a real progress watchdog:
readiness is announced after USB initialization, not after all brokers connect.
The main loop sends watchdog notifications only while it and the supervisor
make progress. After a supervisor stall of more than 120 seconds, notifications
stop; systemd's 180-second watchdog then restarts the service. Broker outages
alone do not suppress heartbeats while reconnection continues to progress.
Direct runs, launchd and Docker do not receive this systemd watchdog protection.

The systemd defaults are `MemoryHigh=192M`, `MemoryMax=256M`,
`MemorySwapMax=0`, and `TasksMax=64`. These require the corresponding kernel
cgroup support. Adjust them with a service drop-in for unusually large broker
configurations, rather than removing all limits. Docker installs/updates use
a 256 MiB memory/no-extra-swap limit, 64 PIDs, and three rotating 10 MiB log
files. Install/update the service unit or recreate the container as well as
updating Python files; existing deployments do not inherit new limits by magic.

### When the Pi Also Loses SSH

A bridge failure, memory pressure, lost networking, power trouble, SD-card
errors and a kernel failure are different diagnoses. None of these safeguards
proves the cause of a previous incident. After recovery, collect the bridge
version, broker presets, installation type, and previous-boot logs:

```bash
journalctl -b -1 -k --no-pager
journalctl -b -1 -u mctomqtt --no-pager
```

Previous-boot logs require persistent journaling. Look for out-of-memory kills,
blocked tasks, USB resets, filesystem errors and undervoltage, and compare the
timestamps with MQTT reconnect/rejection events. Redact keys, tokens and other
credentials before sharing logs.

## Updates

Use the standalone update script for the simplest update experience:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/scripts/update.sh | sudo bash
```

Or re-run the installer — it will detect your existing installation and offer to update:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash
```

For non-interactive updates:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/install.sh | sudo bash -s -- --update
```

The updater will:

- Detect your existing service type (systemd/launchd/Docker)
- Stop the service/container
- Download and verify updated files
- Overwrite `/etc/mctomqtt/config.toml` with latest defaults
- Preserve your `/etc/mctomqtt/config.d/99-user.toml` configuration
- Restart the service/container automatically

## Migration

If you have a legacy `~/.meshcoretomqtt` installation, migrate to the new layout:

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/scripts/migrate.sh | sudo bash
```

The migrator will:

- Escape and validate `.env`/`.env.local` conversion before retiring a working legacy service
- Atomically write private TOML without printing credentials or overwriting existing user configuration
- Stop and remove old systemd/launchd services
- Preserve the old installation directory for manual cleanup

## Uninstallation

```bash
curl -fsSL https://raw.githubusercontent.com/Cisien/meshcoretomqtt/main/uninstall.sh | sudo bash
```

The uninstaller will:

- Stop and remove the service
- Offer to backup your `99-user.toml` configuration
- Remove `/opt/mctomqtt/` and `/etc/mctomqtt/`
- Remove the `mctomqtt` system user

## Privacy

This tool collects and forwards all packets transmitted over the MeshCore
network. Privacy on MeshCore is provided by protecting secret channel 
keys. All packets will be forwarded as raw data without additional processing
or decryption. The primary use of this script is to send data to LetsMesh.net.
Learn at https://letsmesh.net/

## Viewing the data

- Use a MQTT tool to view the packet data. I recommend MQTTX.
- Data will appear in topics based on your configuration. Default format:
  ```
  meshcore/{IATA}/{PUBLIC_KEY}/status
  meshcore/{IATA}/{PUBLIC_KEY}/packets
  meshcore/{IATA}/{PUBLIC_KEY}/debug
  ```
  Where `{IATA}` is your 3-letter location code and `{PUBLIC_KEY}` is your
  device's public key (auto-detected).

  **status**: Last will and testament (LWT) showing online/offline status.

  **packets**: Flood or direct packets going through the repeater.

  **debug**: Debug info (if enabled on the repeater build).

## Example MQTT data...

Note: origin is the repeater node reporting the data to mqtt. Not the origin of
the LoRa packet.

Flood packet...

```
Topic: meshcore/SEA/A1B2.../packets QoS: 0
{"origin": "ag loft rpt", "origin_id": "A1B2...", "timestamp": "2025-03-16T00:07:11.191561", "type": "PACKET", "direction": "rx", "time": "00:07:09", "date": "16/3/2025", "len": "87", "packet_type": "5", "route": "F", "payload_len": "83", "raw": "0A1B2C...", "SNR": "4", "RSSI": "-93", "score": "1000", "hash": "AC9D2DDDD8395712"}
```

Direct packet...

```
Topic: meshcore/SEA/A1B2.../packets QoS: 0
{"origin": "ag loft rpt", "origin_id": "A1B2...", "timestamp": "2025-03-15T23:09:00.710459", "type": "PACKET", "direction": "rx", "time": "23:08:59", "date": "15/3/2025", "len": "22", "packet_type": "2", "route": "D", "payload_len": "20", "raw": "0A1B2C...", "SNR": "5", "RSSI": "-93", "score": "1000", "hash": "890BFA3069FD1250", "path": "C2 -> E2"}
```
