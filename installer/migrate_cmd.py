"""Migration from legacy ~/.meshcoretomqtt to /opt/mctomqtt."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

from .system import run_cmd
from .config import toml_escape, write_private_config, validate_config_directory, validate_install_layout
from .ui import (
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt_yes_no,
)

if TYPE_CHECKING:
    from . import InstallerContext


# ---------------------------------------------------------------------------
# .env file parsing and TOML conversion (ported from embedded Python in bash)
# ---------------------------------------------------------------------------

def parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict."""
    env: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def env_to_toml(env: dict[str, str]) -> str:
    """Convert a merged .env dict to TOML config string."""
    lines: list[str] = []

    # General section
    general: dict[str, str] = {}
    iata = env.get("MCTOMQTT_IATA", "")
    if iata and iata != "XXX":
        general["iata"] = iata
    log_level = env.get("MCTOMQTT_LOG_LEVEL", "")
    if log_level and log_level != "INFO":
        general["log_level"] = log_level
    sync_time = env.get("MCTOMQTT_SYNC_TIME", "")
    if sync_time and sync_time.lower() != "true":
        general["sync_time"] = sync_time.lower()

    if general:
        lines.append("[general]")
        for k, v in general.items():
            if v in ("true", "false"):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f'{k} = "{toml_escape(v)}"')
        lines.append("")

    # Serial section
    ports = env.get("MCTOMQTT_SERIAL_PORTS", "")
    baud = env.get("MCTOMQTT_SERIAL_BAUD_RATE", "")
    timeout = env.get("MCTOMQTT_SERIAL_TIMEOUT", "")
    if ports or baud or timeout:
        lines.append("[serial]")
        if ports:
            port_list = [p.strip() for p in ports.split(",") if p.strip()]
            ports_str = ", ".join(f'"{toml_escape(p)}"' for p in port_list)
            lines.append(f"ports = [{ports_str}]")
        if baud and baud != "115200":
            lines.append(f"baud_rate = {baud}")
        if timeout and timeout != "2":
            lines.append(f"timeout = {timeout}")
        lines.append("")

    # Update section
    repo = env.get("MCTOMQTT_UPDATE_REPO", "")
    branch = env.get("MCTOMQTT_UPDATE_BRANCH", "")
    if repo or branch:
        lines.append("[update]")
        if repo:
            lines.append(f'repo = "{toml_escape(repo)}"')
        if branch:
            lines.append(f'branch = "{toml_escape(branch)}"')
        lines.append("")

    # Remote serial
    rs_enabled = env.get("MCTOMQTT_REMOTE_SERIAL_ENABLED", "false")
    rs_companions = env.get("MCTOMQTT_REMOTE_SERIAL_ALLOWED_COMPANIONS", "")
    if rs_enabled == "true" or rs_companions:
        lines.append("[remote_serial]")
        lines.append(f"enabled = {rs_enabled}")
        if rs_companions:
            companion_list = [c.strip() for c in rs_companions.split(",") if c.strip()]
            comp_str = ", ".join(f'"{toml_escape(c)}"' for c in companion_list)
            lines.append(f"allowed_companions = [{comp_str}]")
        else:
            lines.append("allowed_companions = []")
        lines.append("")

    # Brokers
    for broker_num in range(1, 5):
        prefix = f"MCTOMQTT_MQTT{broker_num}_"
        enabled = env.get(f"{prefix}ENABLED", "false")
        server = env.get(f"{prefix}SERVER", "")
        if enabled != "true" or not server:
            continue

        port_val = env.get(f"{prefix}PORT", "1883")
        transport = env.get(f"{prefix}TRANSPORT", "tcp")
        use_tls = env.get(f"{prefix}USE_TLS", "false")
        tls_verify = env.get(f"{prefix}TLS_VERIFY", "true")
        use_auth_token = env.get(f"{prefix}USE_AUTH_TOKEN", "false")
        username = env.get(f"{prefix}USERNAME", "")
        password = env.get(f"{prefix}PASSWORD", "")
        token_audience = env.get(f"{prefix}TOKEN_AUDIENCE", "")
        token_owner = env.get(f"{prefix}TOKEN_OWNER", "")
        token_email = env.get(f"{prefix}TOKEN_EMAIL", "")
        keepalive = env.get(f"{prefix}KEEPALIVE", "60")
        qos = env.get(f"{prefix}QOS", "0")
        retain = env.get(f"{prefix}RETAIN", "true")

        if "letsmesh" in server:
            if "-us-" in server:
                broker_name = "letsmesh-us"
            elif "-eu-" in server:
                broker_name = "letsmesh-eu"
            else:
                broker_name = f"letsmesh-{broker_num}"
        else:
            broker_name = f"custom-{broker_num}"

        lines.append("[[broker]]")
        lines.append(f'name = "{toml_escape(broker_name)}"')
        lines.append("enabled = true")
        lines.append(f'server = "{toml_escape(server)}"')
        lines.append(f"port = {port_val}")
        lines.append(f'transport = "{toml_escape(transport)}"')
        lines.append(f"keepalive = {keepalive}")
        lines.append(f"qos = {qos}")
        lines.append(f"retain = {retain}")
        lines.append("")

        if use_tls == "true":
            lines.append("[broker.tls]")
            lines.append("enabled = true")
            lines.append(f"verify = {tls_verify}")
            lines.append("")

        lines.append("[broker.auth]")
        if use_auth_token == "true":
            lines.append('method = "token"')
            if token_audience:
                lines.append(f'audience = "{toml_escape(token_audience)}"')
            if token_owner:
                lines.append(f'owner = "{toml_escape(token_owner)}"')
            if token_email:
                lines.append(f'email = "{toml_escape(token_email)}"')
        elif username:
            lines.append('method = "password"')
            lines.append(f'username = "{toml_escape(username)}"')
            lines.append(f'password = "{toml_escape(password)}"')
        else:
            lines.append('method = "none"')
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Migration command
# ---------------------------------------------------------------------------

def _real_user_home() -> Path:
    """Get the real user's home directory, even when running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def detect_old_installation() -> str | None:
    """Check for legacy ~/.meshcoretomqtt installation. Returns path or None."""
    old_dir = _real_user_home() / ".meshcoretomqtt"
    if old_dir.is_dir() and (old_dir / "mctomqtt.py").exists():
        return str(old_dir)
    return None


_MIGRATED_SENTINEL = ".migrated"


def is_already_migrated(old_dir: str) -> bool:
    """Return True if the legacy directory has already been migrated."""
    return (Path(old_dir) / _MIGRATED_SENTINEL).exists()


def mark_migrated(old_dir: str, install_dir: str) -> None:
    """Write a sentinel file so future migration runs are skipped."""
    (Path(old_dir) / _MIGRATED_SENTINEL).write_text(
        f"Migrated to {install_dir}\n"
    )


def prepare_migrated_config(merged: dict[str, str], config_dir: str) -> Path | None:
    """Commit validated private config before any legacy service is retired."""
    validate_config_directory(config_dir)
    dest = Path(config_dir) / "config.d" / "99-user.toml"
    legacy = dest.with_name("00-user.toml")
    if dest.exists() or dest.is_symlink() or legacy.exists() or legacy.is_symlink():
        raise FileExistsError("Existing user configuration must not be overwritten by migration")
    if not merged:
        return None
    content = ("# MeshCore to MQTT - User Configuration\n"
               "# Migrated from legacy .env/.env.local installation\n\n"
               + env_to_toml(merged))
    # Invalid numeric/boolean inputs fail before creating a directory or file.
    import tomllib
    tomllib.loads(content)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_private_config(dest, content, overwrite=False)
    return dest


def run_migrate(ctx: InstallerContext) -> bool:
    """Migrate from ~/.meshcoretomqtt to /opt/mctomqtt.

    Returns True if migration was performed, False if skipped/nothing to migrate.
    """
    validate_install_layout(ctx.install_dir, ctx.config_dir)
    old_dir = detect_old_installation()
    if old_dir is None:
        return False

    if is_already_migrated(old_dir):
        print_info("Legacy installation already migrated. Skipping.")
        return False

    config_d = Path(ctx.config_dir) / "config.d"
    if any(path.exists() or path.is_symlink()
           for path in (config_d / "99-user.toml", config_d / "00-user.toml")):
        print_warning("Existing user configuration found; leaving legacy installation untouched")
        return False

    print()
    print_header("Legacy Installation Detected")
    print_info(f"Found existing installation at: {old_dir}")
    print()

    if not prompt_yes_no(f"Migrate to new installation at {ctx.install_dir}?", "y"):
        print_info("Skipping migration. Old installation left in place.")
        return False

    # Prepare the configuration before touching working legacy services.
    print_info("Migrating configuration to TOML format...")

    old_env = os.path.join(old_dir, ".env")
    old_env_local = os.path.join(old_dir, ".env.local")

    # Preserve the effective legacy settings. The new TOML defaults need not
    # reproduce a legacy source's defaults, and broker fields such as server
    # and enabled must survive even when only credentials were customized.
    user_env = parse_env_file(old_env)
    user_env_local = parse_env_file(old_env_local)
    merged = dict(user_env)
    merged.update(user_env_local)

    migrated_toml_path = prepare_migrated_config(merged, ctx.config_dir)
    if migrated_toml_path is None:
        print_warning("No user configuration found; leaving legacy services untouched")
        return False
    print_success(f"Configuration migrated to {migrated_toml_path}")
    print_info("Configuration contents are not printed because they may contain credentials")

    _stop_old_services(old_dir)

    # Step 3: Remove old systemd unit
    _cleanup_old_service_units()

    # Step 4: Mark migration as complete
    mark_migrated(old_dir, ctx.install_dir)

    # Step 5: Inform user about old directory
    print()
    print_info(f"Old installation preserved at: {old_dir}")
    print_info("You can remove it once you've verified the new installation works:")
    print(f"  rm -rf {old_dir}")
    print()

    return True


def _stop_old_services(old_dir: str) -> None:
    """Stop and disable old systemd/launchd services."""
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        print_info("Stopping old systemd service...")
        run_cmd(["systemctl", "stop", "mctomqtt.service"], check=False)
        run_cmd(["systemctl", "disable", "mctomqtt.service"], check=False)
        print_success("Old service stopped and disabled")

    if platform.system() == "Darwin":
        old_plist = _real_user_home() / "Library" / "LaunchAgents" / "com.meshcore.mctomqtt.plist"
        if old_plist.exists():
            print_info("Stopping old launchd service...")
            run_cmd(["launchctl", "unload", str(old_plist)], check=False)
            print_success("Old launchd service stopped")


def _cleanup_old_service_units() -> None:
    """Remove old systemd/launchd unit files."""
    if Path("/etc/systemd/system/mctomqtt.service").exists():
        print_info("Removing old systemd unit...")
        Path("/etc/systemd/system/mctomqtt.service").unlink(missing_ok=True)
        run_cmd(["systemctl", "daemon-reload"])
        print_success("Old systemd unit removed")

    if platform.system() == "Darwin":
        old_plist = _real_user_home() / "Library" / "LaunchAgents" / "com.meshcore.mctomqtt.plist"
        if old_plist.exists():
            os.unlink(old_plist)
            print_success("Old launchd plist removed")
