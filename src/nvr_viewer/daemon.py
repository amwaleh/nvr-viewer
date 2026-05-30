"""Daemon / service management for NVR Viewer.

Supports:
- Linux: systemd service unit
- Windows: NSSM-based Windows Service, or Task Scheduler fallback
- macOS: launchd plist

CLI: nvr-viewer service install|uninstall|start|stop|status|logs
"""
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "nvr-viewer"
SERVICE_DISPLAY = "NVR Viewer"
SERVICE_DESC = "Network Video Recorder — camera streaming, recording, and AI detection"


def _python_exe() -> str:
    """Get the path to the current Python interpreter."""
    return sys.executable


def _nvr_module_cmd() -> list[str]:
    """Command to run nvr-viewer web server as a module."""
    return [_python_exe(), "-m", "nvr_viewer", "web", "--port", "8080"]


# ---------- Linux (systemd) ----------

_SYSTEMD_UNIT = f"""\
[Unit]
Description={SERVICE_DESC}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={{exec_start}}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
User={{user}}
WorkingDirectory={{work_dir}}
Environment="PATH={{path}}"

[Install]
WantedBy=multi-user.target
"""

UNIT_PATH = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"


def _systemd_install(port: int, host: str):
    exec_start = " ".join([
        _python_exe(), "-m", "nvr_viewer", "web",
        "--host", host, "--port", str(port),
    ])
    unit = _SYSTEMD_UNIT.format(
        exec_start=exec_start,
        user=os.getenv("USER", "root"),
        work_dir=str(Path.home()),
        path=str(Path(_python_exe()).parent) + ":" + os.environ.get("PATH", ""),
    )
    try:
        UNIT_PATH.write_text(unit)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True)
        print(f"[OK] Service installed: {UNIT_PATH}")
        print(f"   Start with: nvr-viewer service start")
        print(f"   Or:          sudo systemctl start {SERVICE_NAME}")
    except PermissionError:
        print("[ERROR] Permission denied. Run with sudo:")
        print(f"   sudo {_python_exe()} -m nvr_viewer service install")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] systemctl failed: {e}")


def _systemd_uninstall():
    try:
        subprocess.run(["systemctl", "stop", SERVICE_NAME],
                        capture_output=True)
        subprocess.run(["systemctl", "disable", SERVICE_NAME],
                        capture_output=True)
        if UNIT_PATH.exists():
            UNIT_PATH.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        print(f"[OK] Service uninstalled.")
    except PermissionError:
        print("[ERROR] Permission denied. Run with sudo.")


def _systemd_start():
    subprocess.run(["systemctl", "start", SERVICE_NAME], check=True)
    print(f"[OK] Service started.")


def _systemd_stop():
    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=True)
    print(f"[OK] Service stopped.")


def _systemd_status():
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True, text=True,
    )
    state = result.stdout.strip()
    if state == "active":
        print(f"[OK] {SERVICE_NAME} is running")
    elif state == "inactive":
        print(f"[STOPPED]  {SERVICE_NAME} is stopped")
    elif state == "failed":
        print(f"[ERROR] {SERVICE_NAME} has failed")
    else:
        print(f"[WARN]  {SERVICE_NAME}: {state}")
    # Show last few log lines
    subprocess.run([
        "journalctl", "-u", SERVICE_NAME, "-n", "5", "--no-pager",
    ])


def _systemd_logs(follow: bool = False, lines: int = 50):
    cmd = ["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"]
    if follow:
        cmd.append("-f")
    subprocess.run(cmd)


# ---------- Windows (NSSM or Task Scheduler) ----------

def _nssm_path() -> str | None:
    """Find nssm.exe on PATH or in common locations."""
    found = shutil.which("nssm")
    if found:
        return found
    for candidate in [
        r"C:\nssm\nssm.exe",
        r"C:\tools\nssm\nssm.exe",
        str(Path.home() / "nssm" / "nssm.exe"),
    ]:
        if Path(candidate).exists():
            return candidate
    return None


def _windows_install_nssm(port: int, host: str):
    nssm = _nssm_path()
    if not nssm:
        print("[ERROR] NSSM not found. Install it first:")
        print("   winget install nssm")
        print("   Or download from https://nssm.cc/download")
        print("\n   Falling back to Task Scheduler...")
        _windows_install_task(port, host)
        return

    args = f"-m nvr_viewer web --host {host} --port {port}"
    try:
        subprocess.run([nssm, "install", SERVICE_NAME, _python_exe(), args],
                        check=True)
        subprocess.run([nssm, "set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY],
                        check=True)
        subprocess.run([nssm, "set", SERVICE_NAME, "Description", SERVICE_DESC],
                        check=True)
        subprocess.run([nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"],
                        check=True)
        subprocess.run([nssm, "set", SERVICE_NAME, "AppStdout",
                        str(Path.home() / ".nvr-viewer" / "service.log")],
                        check=True)
        subprocess.run([nssm, "set", SERVICE_NAME, "AppStderr",
                        str(Path.home() / ".nvr-viewer" / "service.log")],
                        check=True)
        print(f"[OK] Windows Service installed via NSSM.")
        print(f"   Start with: nvr-viewer service start")
        print(f"   Logs: {Path.home() / '.nvr-viewer' / 'service.log'}")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] NSSM install failed: {e}")
        print("   Try running as Administrator.")


def _windows_install_task(port: int, host: str):
    """Fallback: install as a Task Scheduler task that runs at logon."""
    cmd = f'"{_python_exe()}" -m nvr_viewer web --host {host} --port {port}'
    task_name = f"\\{SERVICE_NAME}"
    try:
        # Remove existing task if any
        subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True,
        )
        subprocess.run([
            "schtasks", "/Create",
            "/TN", task_name,
            "/TR", cmd,
            "/SC", "ONLOGON",
            "/RL", "HIGHEST",
            "/F",
        ], check=True)
        print(f"[OK] Scheduled task '{SERVICE_NAME}' created (runs at logon).")
        print(f"   Start now with: nvr-viewer service start")
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Task Scheduler failed: {e}")
        print("   Try running as Administrator.")


def _windows_start():
    nssm = _nssm_path()
    if nssm:
        try:
            subprocess.run([nssm, "start", SERVICE_NAME], check=True)
            print(f"[OK] Service started.")
            return
        except subprocess.CalledProcessError:
            pass

    # Try Task Scheduler
    try:
        subprocess.run(
            ["schtasks", "/Run", "/TN", f"\\{SERVICE_NAME}"],
            check=True,
        )
        print(f"[OK] Task started.")
    except subprocess.CalledProcessError:
        print(f"[ERROR] Could not start service. Is it installed?")


def _windows_stop():
    nssm = _nssm_path()
    if nssm:
        try:
            subprocess.run([nssm, "stop", SERVICE_NAME], check=True)
            print(f"[OK] Service stopped.")
            return
        except subprocess.CalledProcessError:
            pass

    # Try Task Scheduler
    try:
        subprocess.run(
            ["schtasks", "/End", "/TN", f"\\{SERVICE_NAME}"],
            check=True,
        )
        print(f"[OK] Task stopped.")
    except subprocess.CalledProcessError:
        print(f"[ERROR] Could not stop service. Is it installed?")


def _windows_uninstall():
    nssm = _nssm_path()
    if nssm:
        try:
            subprocess.run([nssm, "stop", SERVICE_NAME], capture_output=True)
            subprocess.run([nssm, "remove", SERVICE_NAME, "confirm"],
                            check=True)
            print(f"[OK] Windows Service uninstalled.")
            return
        except subprocess.CalledProcessError:
            pass

    # Try Task Scheduler
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", f"\\{SERVICE_NAME}", "/F"],
            check=True,
        )
        print(f"[OK] Scheduled task removed.")
    except subprocess.CalledProcessError:
        print(f"[ERROR] Could not uninstall service. Is it installed?")


def _windows_status():
    nssm = _nssm_path()
    if nssm:
        result = subprocess.run(
            [nssm, "status", SERVICE_NAME],
            capture_output=True, text=True,
        )
        status = result.stdout.strip()
        if "SERVICE_RUNNING" in status:
            print(f"[OK] {SERVICE_NAME} is running (NSSM)")
        elif "SERVICE_STOPPED" in status:
            print(f"[STOPPED]  {SERVICE_NAME} is stopped (NSSM)")
        elif "SERVICE_PAUSED" in status:
            print(f"[PAUSED]  {SERVICE_NAME} is paused (NSSM)")
        else:
            print(f"[WARN]  {SERVICE_NAME}: {status}")
        return

    # Try Task Scheduler
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", f"\\{SERVICE_NAME}", "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if "Status:" in line:
                print(f"[INFO] Task {SERVICE_NAME}: {line.strip()}")
                return
        print(f"[INFO] Task {SERVICE_NAME} exists (status unknown)")
    else:
        print(f"[WARN]  {SERVICE_NAME} is not installed.")


def _windows_logs(follow: bool = False, lines: int = 50):
    log_file = Path.home() / ".nvr-viewer" / "service.log"
    if not log_file.exists():
        print(f"No log file found at {log_file}")
        return
    if follow:
        print(f"Tailing {log_file} (Ctrl+C to stop)...")
        subprocess.run(["powershell", "-Command",
                        f"Get-Content -Path '{log_file}' -Tail {lines} -Wait"])
    else:
        with open(log_file) as f:
            all_lines = f.readlines()
            for line in all_lines[-lines:]:
                print(line, end="")


# ---------- macOS (launchd) ----------

_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"com.nvr-viewer.plist"

_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nvr-viewer</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>nvr_viewer</string>
        <string>web</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/service.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/service.log</string>
    <key>WorkingDirectory</key>
    <string>{work_dir}</string>
</dict>
</plist>
"""


def _launchd_install(port: int, host: str):
    log_dir = Path.home() / ".nvr-viewer"
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = _LAUNCHD_PLIST.format(
        python=_python_exe(),
        host=host,
        port=str(port),
        log_dir=str(log_dir),
        work_dir=str(Path.home()),
    )
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_text(plist)
    subprocess.run(["launchctl", "load", str(_PLIST_PATH)], check=True)
    print(f"[OK] LaunchAgent installed: {_PLIST_PATH}")
    print(f"   Logs: {log_dir / 'service.log'}")


def _launchd_uninstall():
    if _PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(_PLIST_PATH)],
                        capture_output=True)
        _PLIST_PATH.unlink()
    print(f"[OK] LaunchAgent uninstalled.")


def _launchd_start():
    subprocess.run(["launchctl", "start", "com.nvr-viewer"], check=True)
    print(f"[OK] Service started.")


def _launchd_stop():
    subprocess.run(["launchctl", "stop", "com.nvr-viewer"], check=True)
    print(f"[OK] Service stopped.")


def _launchd_status():
    result = subprocess.run(
        ["launchctl", "list", "com.nvr-viewer"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"[OK] {SERVICE_NAME} is loaded")
        for line in result.stdout.splitlines():
            print(f"   {line}")
    else:
        print(f"[WARN]  {SERVICE_NAME} is not loaded.")


def _launchd_logs(follow: bool = False, lines: int = 50):
    log_file = Path.home() / ".nvr-viewer" / "service.log"
    if not log_file.exists():
        print(f"No log file found at {log_file}")
        return
    if follow:
        subprocess.run(["tail", "-f", "-n", str(lines), str(log_file)])
    else:
        subprocess.run(["tail", "-n", str(lines), str(log_file)])


# ---------- Dispatcher ----------

def _get_platform() -> str:
    s = platform.system()
    if s == "Linux":
        return "linux"
    elif s == "Windows":
        return "windows"
    elif s == "Darwin":
        return "macos"
    else:
        return s.lower()


def service_install(port: int = 8080, host: str = "0.0.0.0"):
    """Install NVR Viewer as a system service."""
    plat = _get_platform()
    print(f"Installing {SERVICE_NAME} service on {plat}...")
    print(f"  Python: {_python_exe()}")
    print(f"  Server: {host}:{port}")
    print()
    if plat == "linux":
        _systemd_install(port, host)
    elif plat == "windows":
        _windows_install_nssm(port, host)
    elif plat == "macos":
        _launchd_install(port, host)
    else:
        print(f"[ERROR] Unsupported platform: {plat}")


def service_uninstall():
    plat = _get_platform()
    if plat == "linux":
        _systemd_uninstall()
    elif plat == "windows":
        _windows_uninstall()
    elif plat == "macos":
        _launchd_uninstall()


def service_start():
    plat = _get_platform()
    if plat == "linux":
        _systemd_start()
    elif plat == "windows":
        _windows_start()
    elif plat == "macos":
        _launchd_start()


def service_stop():
    plat = _get_platform()
    if plat == "linux":
        _systemd_stop()
    elif plat == "windows":
        _windows_stop()
    elif plat == "macos":
        _launchd_stop()


def service_status():
    plat = _get_platform()
    if plat == "linux":
        _systemd_status()
    elif plat == "windows":
        _windows_status()
    elif plat == "macos":
        _launchd_status()


def service_logs(follow: bool = False, lines: int = 50):
    plat = _get_platform()
    if plat == "linux":
        _systemd_logs(follow, lines)
    elif plat == "windows":
        _windows_logs(follow, lines)
    elif plat == "macos":
        _launchd_logs(follow, lines)
