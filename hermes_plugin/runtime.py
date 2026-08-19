"""
Runtime module for the Hermes tencent-relay plugin.

Manages the lifecycle of the local HTTP relay server (server/relay.py):
configuration, process spawning/stopping, health checks, and systemd/launchd
service registration.

All public functions return a (exit_code: int, message: str) tuple for easy
CLI wrapping.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_state_dir = Path.home() / ".hermes" / "state"
_runtime_dir = Path(__file__).resolve().parent  # hermes_plugin/
_plugin_root = _runtime_dir.parent  # hermes-tencent-relay/

_state_file = _state_dir / "hermes-tencent-relay.json"
_config_file = _state_dir / "hermes-tencent-relay.json"  # shared state file
_relay_script = _plugin_root / "server" / "relay.py"
_systemd_unit_name = "hermes-tencent-relay"
_systemd_unit_path = (
    Path.home() / ".config" / "systemd" / "user" / f"{_systemd_unit_name}.service"
)
_launchd_label = "com.hermes.tencent-relay"
_launchd_plist_path = (
    Path.home() / "Library" / "LaunchAgents" / f"{_launchd_label}.plist"
)

_HUB_REPO = "https://github.com/TencentCloud/TencentDB-Agent-Memory"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "upstream_url": "",
    "cf_client_id": "",
    "cf_client_secret": "",
    "listen_port": 8420,
    "listen_host": "127.0.0.1",
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    """Load configuration from the JSON state file.

    Returns a dict with all fields populated (missing keys are filled with
    defaults).
    """
    config = dict(_DEFAULT_CONFIG)
    if _config_file.exists():
        try:
            data = json.loads(_config_file.read_text())
            config.update(data)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: failed to load config, using defaults: {exc}",
                  file=sys.stderr)
    return config


def save_config(config: Dict[str, Any]) -> None:
    """Save configuration to the JSON state file.

    Only recognised keys are persisted; unknown keys are silently dropped.
    The state directory is created if it does not exist.
    """
    cleaned = {k: config[k] for k in _DEFAULT_CONFIG if k in config}
    _state_dir.mkdir(parents=True, exist_ok=True)
    try:
        _config_file.write_text(json.dumps(cleaned, indent=2, sort_keys=True))
        os.chmod(_config_file, 0o600)
    except OSError as exc:
        print(f"error: failed to write config: {exc}", file=sys.stderr)
        raise


def _upstream_configured(config: Dict[str, Any] | None = None) -> bool:
    cfg = config if config is not None else load_config()
    return bool(str(cfg.get("upstream_url", "")).strip())


# ---------------------------------------------------------------------------
# State file helpers (pid, log_path)
# ---------------------------------------------------------------------------


def _read_state() -> Dict[str, Any]:
    """Read the full state file, returning an empty dict on failure."""
    if _state_file.exists():
        try:
            return json.loads(_state_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_state(state: Dict[str, Any]) -> None:
    """Atomically write the state file."""
    _state_dir.mkdir(parents=True, exist_ok=True)
    tmp = _state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(_state_file)


def _relay_env(config: Dict[str, Any]) -> Dict[str, str]:
    """Build relay subprocess environment from config."""
    env = os.environ.copy()
    env["TENCENT_RELAY_UPSTREAM_URL"] = config.get("upstream_url", "")
    env["TENCENT_RELAY_CF_CLIENT_ID"] = config.get("cf_client_id", "")
    env["TENCENT_RELAY_CF_CLIENT_SECRET"] = config.get("cf_client_secret", "")
    env["TENCENT_RELAY_LISTEN_HOST"] = config.get(
        "listen_host", _DEFAULT_CONFIG["listen_host"]
    )
    env["TENCENT_RELAY_LISTEN_PORT"] = str(
        config.get("listen_port", _DEFAULT_CONFIG["listen_port"])
    )
    return env


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


def start() -> Tuple[int, str]:
    """Start the relay server as a background subprocess.

    The relay is launched via subprocess.Popen.  Environment variables are
    set from the current config so that relay.py picks them up.  PID and
    log_path are written to the state file.
    """
    config = load_config()
    if not _upstream_configured(config):
        return (
            1,
            f"upstream URL not configured — run: hermes tencent-relay setup",
        )

    log_path = _state_dir / "hermes-tencent-relay.log"
    _state_dir.mkdir(parents=True, exist_ok=True)

    # Check if already running
    state = _read_state()
    pid = state.get("pid")
    if pid is not None:
        try:
            os.kill(pid, 0)
            return 0, f"relay already running (pid {pid})"
        except OSError:
            pass  # stale pid

    env = _relay_env(config)

    if not _relay_script.is_file():
        return 1, f"relay script not found: {_relay_script}"

    python = shutil.which("python3") or sys.executable

    try:
        log_fh = open(log_path, "a")
    except OSError as exc:
        return 1, f"cannot open log file {log_path}: {exc}"

    try:
        proc = subprocess.Popen(
            [python, str(_relay_script)],
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log_fh.close()
        return 1, f"failed to start relay: {exc}"

    pid = proc.pid
    _write_state({"pid": pid, "log_path": str(log_path)})
    log_fh.close()

    # Brief wait to let the process settle and detect immediate failures
    time.sleep(0.5)
    ret = proc.poll()
    if ret is not None:
        _write_state({})
        return 1, f"relay exited immediately with code {ret} (check log: {log_path})"

    return 0, f"relay started (pid {pid}, log: {log_path})"


def stop() -> Tuple[int, str]:
    """Stop the relay server.

    SIGTERM is sent first; if the process is still alive after 5 seconds,
    SIGKILL is sent.  The PID entry is removed from the state file.
    """
    state = _read_state()
    pid = state.get("pid")
    if pid is None:
        return 0, "no relay pid found (not running?)"

    try:
        os.kill(pid, 0)
    except OSError:
        # Process already gone
        _write_state({})
        return 0, f"relay (pid {pid}) not found — already stopped"

    # SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return 1, f"failed to send SIGTERM to pid {pid}: {exc}"

    # Wait up to 5 seconds
    for _ in range(10):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except OSError:
            # Process is gone
            _write_state({})
            return 0, f"relay (pid {pid}) stopped"

    # SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        return 1, f"failed to send SIGKILL to pid {pid}: {exc}"

    # Final wait
    time.sleep(0.5)
    _write_state({})
    return 0, f"relay (pid {pid}) killed"


def status() -> Tuple[int, str]:
    """Check whether the relay process is alive.

    Returns (0, "running") if the PID is alive, (1, "not running") otherwise.
    """
    state = _read_state()
    pid = state.get("pid")
    if pid is None:
        return 1, "not running (no pid)"

    try:
        os.kill(pid, 0)
    except OSError:
        _write_state({})
        return 1, "not running (stale pid)"

    return 0, f"running (pid {pid})"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def _probe_health_url(url: str) -> Tuple[int, str] | None:
    """Try a single health URL. Returns (exit_code, message) or None on failure."""
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        body = resp.read().decode("utf-8", errors="replace")
        return 0, f"healthy (status {resp.status}): {body}"
    except urllib.error.HTTPError as exc:
        return 0, f"healthy (status {exc.code}): {exc.read().decode('utf-8', errors='replace')}"
    except (urllib.error.URLError, OSError):
        return None


def health() -> Tuple[int, str]:
    """Perform a health check against the relay's local listen port.

    Probes ``/health`` first (TencentDB Agent Memory Core), then ``/api/health``
    as a fallback for custom gateways.
    """
    config = load_config()
    listen_host = config.get("listen_host", _DEFAULT_CONFIG["listen_host"])
    listen_port = config.get("listen_port", _DEFAULT_CONFIG["listen_port"])
    base = f"http://{listen_host}:{listen_port}"

    for path in ("/health", "/api/health"):
        result = _probe_health_url(base + path)
        if result is not None:
            return result

    return 1, f"unreachable: no response from {base}/health or /api/health"


# ---------------------------------------------------------------------------
# Service management (systemd on Linux, launchd on macOS)
# ---------------------------------------------------------------------------


def _is_darwin() -> bool:
    return platform.system() == "Darwin"


def _enable_systemd(config: Dict[str, Any], python: str) -> Tuple[int, str]:
    listen_host = config.get("listen_host", _DEFAULT_CONFIG["listen_host"])
    listen_port = config.get("listen_port", _DEFAULT_CONFIG["listen_port"])
    upstream_url = config.get("upstream_url", "")
    cf_client_id = config.get("cf_client_id", "")
    cf_client_secret = config.get("cf_client_secret", "")

    unit_content = f"""\
[Unit]
Description=Hermes Tencent Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} {_relay_script}
Restart=on-failure
RestartSec=5
Environment=TENCENT_RELAY_LISTEN_HOST={listen_host}
Environment=TENCENT_RELAY_LISTEN_PORT={listen_port}
Environment=TENCENT_RELAY_UPSTREAM_URL={upstream_url}
Environment=TENCENT_RELAY_CF_CLIENT_ID={cf_client_id}
Environment=TENCENT_RELAY_CF_CLIENT_SECRET={cf_client_secret}

[Install]
WantedBy=default.target
"""

    try:
        _systemd_unit_path.parent.mkdir(parents=True, exist_ok=True)
        _systemd_unit_path.write_text(unit_content)
    except OSError as exc:
        return 1, f"failed to write systemd unit: {exc}"

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", _systemd_unit_name],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["systemctl", "--user", "start", _systemd_unit_name],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode() if exc.stderr else str(exc)
        return 1, f"systemd operation failed: {msg}"
    except OSError as exc:
        return 1, f"systemctl not available: {exc}"

    return 0, f"systemd service '{_systemd_unit_name}' enabled and started"


def _disable_systemd() -> Tuple[int, str]:
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", _systemd_unit_name],
            check=False,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", _systemd_unit_name],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except OSError:
        pass

    try:
        if _systemd_unit_path.exists():
            _systemd_unit_path.unlink()
    except OSError as exc:
        return 1, f"failed to remove systemd unit: {exc}"

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except OSError:
        pass

    _write_state({})
    return 0, f"systemd service '{_systemd_unit_name}' stopped and disabled"


def _enable_launchd(config: Dict[str, Any], python: str) -> Tuple[int, str]:
    listen_host = config.get("listen_host", _DEFAULT_CONFIG["listen_host"])
    listen_port = str(config.get("listen_port", _DEFAULT_CONFIG["listen_port"]))
    upstream_url = config.get("upstream_url", "")
    cf_client_id = config.get("cf_client_id", "")
    cf_client_secret = config.get("cf_client_secret", "")
    log_path = str(_state_dir / "hermes-tencent-relay.log")

    plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_launchd_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{_relay_script}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TENCENT_RELAY_LISTEN_HOST</key>
        <string>{listen_host}</string>
        <key>TENCENT_RELAY_LISTEN_PORT</key>
        <string>{listen_port}</string>
        <key>TENCENT_RELAY_UPSTREAM_URL</key>
        <string>{upstream_url}</string>
        <key>TENCENT_RELAY_CF_CLIENT_ID</key>
        <string>{cf_client_id}</string>
        <key>TENCENT_RELAY_CF_CLIENT_SECRET</key>
        <string>{cf_client_secret}</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""

    try:
        _launchd_plist_path.parent.mkdir(parents=True, exist_ok=True)
        _launchd_plist_path.write_text(plist_content)
    except OSError as exc:
        return 1, f"failed to write launchd plist: {exc}"

    uid = os.getuid()
    domain = f"gui/{uid}"

    try:
        # Unload first if already loaded (ignore errors)
        subprocess.run(
            ["launchctl", "bootout", domain, str(_launchd_plist_path)],
            check=False,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(_launchd_plist_path)],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["launchctl", "enable", f"{domain}/{_launchd_label}"],
            check=False,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{_launchd_label}"],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode() if exc.stderr else str(exc)
        return 1, f"launchctl operation failed: {msg}"
    except OSError as exc:
        return 1, f"launchctl not available: {exc}"

    return 0, f"launchd service '{_launchd_label}' enabled and started"


def _disable_launchd() -> Tuple[int, str]:
    uid = os.getuid()
    domain = f"gui/{uid}"

    try:
        subprocess.run(
            ["launchctl", "bootout", domain, str(_launchd_plist_path)],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except OSError:
        pass

    try:
        if _launchd_plist_path.exists():
            _launchd_plist_path.unlink()
    except OSError as exc:
        return 1, f"failed to remove launchd plist: {exc}"

    _write_state({})
    return 0, f"launchd service '{_launchd_label}' stopped and disabled"


def enable_service() -> Tuple[int, str]:
    """Install and start the auto-start service (systemd or launchd)."""
    config = load_config()
    if not _upstream_configured(config):
        return (
            1,
            "upstream URL not configured — run: hermes tencent-relay setup",
        )

    python = shutil.which("python3") or sys.executable
    if _is_darwin():
        return _enable_launchd(config, python)
    return _enable_systemd(config, python)


def disable_service() -> Tuple[int, str]:
    """Stop and disable the auto-start service, then remove the unit/plist."""
    if _is_darwin():
        return _disable_launchd()
    return _disable_systemd()


def setup() -> Tuple[int, str]:
    """Interactive setup: prompts for upstream URL and optional CF tokens."""
    config = load_config()

    print("=== TencentDB Agent Memory Relay Setup ===")
    print(f"Hub: {_HUB_REPO}")
    print("Memory Core typically listens on port 8420.")
    print("Leave Cloudflare fields blank unless your hub is behind Cloudflare Access.")
    print("Press Enter to keep current value shown in brackets.")
    print()

    # Required: upstream URL
    current_url = config.get("upstream_url", "")
    while True:
        hint = f" [{current_url}]" if current_url else ""
        val = input(f"  Memory Core URL (e.g. http://host:8420 or https://hub.example.com){hint}: ").strip()
        if not val:
            val = current_url
        if val:
            config["upstream_url"] = val
            break
        print("  Memory Core URL is required.")

    # Listen host / port
    for key, label, current in (
        ("listen_host", "Listen host", config.get("listen_host", _DEFAULT_CONFIG["listen_host"])),
        ("listen_port", "Listen port", str(config.get("listen_port", _DEFAULT_CONFIG["listen_port"]))),
    ):
        while True:
            val = input(f"  {label} [{current}]: ").strip()
            if not val:
                val = current
            if key == "listen_port":
                try:
                    int(val)
                    config[key] = int(val)
                    break
                except ValueError:
                    print(f"  Invalid port: {val}")
                    continue
            else:
                config[key] = val
                break

    # Optional Cloudflare Access
    print()
    print("  Cloudflare Access (optional — leave blank if unused):")
    for key, label in (
        ("cf_client_id", "Cloudflare Access Client ID"),
        ("cf_client_secret", "Cloudflare Access Client Secret"),
    ):
        current = config.get(key, "")
        hint = f" [{current}]" if current else ""
        val = input(f"  {label}{hint}: ").strip()
        config[key] = val if val else current

    save_config(config)
    print()
    print(f"Config saved to {_config_file}")
    print()
    print("Next steps:")
    print("  1. hermes tencent-relay start  — start the relay")
    print("  2. hermes tencent-relay health — verify connection to hub")
    print("  3. hermes tencent-relay enable — install auto-start on boot")
    print("  4. hermes memory setup memory_tencentdb — configure Hermes memory")
    return 0, "Setup complete."
