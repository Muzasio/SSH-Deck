"""SSH Deck core: paths, settings, devices, host keys, the SSH key and its agent.

No Qt here. All user data lives in ~/.config/sshdeck (folder 700, files 600):
  hosts.json        saved devices (name, host, user, port, send-to folder)
  settings.json     terminal / file manager commands, timings, junk patterns
  ssh_config        generated; used with `ssh -F`
  known_hosts.d/    one known_hosts file per device
  id_ed25519(.pub)  the key SSH Deck installs on devices
Short-lived files (password pipe, sftp batch files, agent socket) live in
$XDG_RUNTIME_DIR/sshdeck (700) and are removed after use.
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

APP = "sshdeck"
APP_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = APP_ROOT / "sshdeck.py"
ASKPASS = APP_ROOT / "askpass.sh"
ICON = APP_ROOT / "icon.svg"
CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
HOSTS_FILE = CFG_DIR / "hosts.json"
SETTINGS_FILE = CFG_DIR / "settings.json"
SSH_CFG = CFG_DIR / "ssh_config"
KH_DIR = CFG_DIR / "known_hosts.d"
KEY = CFG_DIR / "id_ed25519"
INSTALL_PUB = CFG_DIR / "install_key.pub"
# Short-lived files: the per-user runtime dir, else a private folder in the config dir
# (never a guessable /tmp path another user could create first).
RUN_DIR = (Path(os.environ["XDG_RUNTIME_DIR"]) / APP if os.environ.get("XDG_RUNTIME_DIR")
           else CFG_DIR / "run")
USER_SSH_CFG = Path.home() / ".ssh" / "config"

# The key is installed with limits: terminal and file transfer only,
# no port / agent / X11 forwarding (sshd "restrict", then "pty" back on).
KEY_OPTIONS = "restrict,pty"
DEFAULT_DEST = "~"
DEFAULT_SETTINGS = {
    "terminal": ["konsole", "-e"],
    "file_manager": ["dolphin"],
    "check_every_seconds": 30,
    "timeout_seconds": 5,
    "trust_cache_seconds": 120,
    "unlock_minutes": 60,
    "download_dir": "~/Downloads",
    "junk": ["__pycache__", "*.pyc", ".git", "node_modules", ".venv", "venv",
             ".mypy_cache", ".pytest_cache", ".DS_Store", "Thumbs.db"],
}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
             "ecdsa-sha2-nistp521")
# One known_hosts line from ssh-keyscan: "<host> <type> <base64 key>"
KEYLINE_RE = re.compile(
    r"^\S+ (?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}$")

# Absolute paths of the programs used, filled in by resolve_bins() at start.
REQUIRED = ("ssh", "ssh-keygen", "ssh-keyscan", "ssh-copy-id", "sftp", "ssh-agent",
            "ssh-add", "setsid")
OPTIONAL = ("notify-send", "kdialog", "zenity")
BIN = {n: n for n in REQUIRED + OPTIONAL}


def resolve_bins() -> str | None:
    """Pin each program to its absolute path. Returns a missing required name, if any."""
    for n in REQUIRED + OPTIONAL:
        path = shutil.which(n)
        if path is None:
            if n in REQUIRED:
                return n
            BIN[n] = ""
        else:
            BIN[n] = os.path.realpath(path)
    return None


# ---------------------------------------------------------------- storage

def _private_dir(d: Path) -> None:
    if d.is_symlink():
        raise SystemExit(f"Refusing to use {d}: it is a symlink.")
    d.mkdir(parents=True, exist_ok=True)
    st = d.stat()
    if st.st_uid != os.getuid():
        raise SystemExit(f"Refusing to use {d}: not owned by you.")
    os.chmod(d, 0o700)


def ensure_dirs() -> None:
    for d in (CFG_DIR, KH_DIR, RUN_DIR):
        _private_dir(d)


def write_private(path: Path, text: str) -> None:
    """Write a file with 600 permissions, atomically, never following symlinks."""
    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def run_tmp(prefix: str) -> Path:
    """A new private temp folder in the runtime dir (caller removes it)."""
    _private_dir(RUN_DIR)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=RUN_DIR))


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOSTNAME_RE.match(host))


def check_device(d: dict) -> str | None:
    """Return an error message, or None if the device is valid."""
    if not all(isinstance(d.get(k), str) for k in ("name", "host", "user")):
        return "Name, IP and user must be text."
    if not NAME_RE.match(d["name"]):
        return "Name: 1-32 small letters, numbers or '-' (start with a letter or number)."
    if not valid_host(d["host"]):
        return "IP / host: type a valid IP address or host name."
    if not USER_RE.match(d["user"]):
        return "User: letters, numbers, '_', '.', '-' only."
    port = d.get("port")
    if type(port) is not int or not 1 <= port <= 65535:  # bool is an int subclass
        return "Port: a number from 1 to 65535."
    return None


def check_dest(dest) -> str | None:
    """Validate the 'send to' folder on the device."""
    if not isinstance(dest, str) or not dest.strip():
        return "Folder: type a folder on the device, e.g. ~/Downloads"
    if len(dest) > 1024:
        return "Folder: path is too long."
    if any(ord(c) < 32 or ord(c) == 127 for c in dest):
        return "Folder: control characters are not allowed."
    if any(c in dest for c in "*?[]"):
        return "Folder: wildcard characters * ? [ ] are not allowed."
    if dest.startswith("~") and dest != "~" and not dest.startswith("~/"):
        return "Folder: use ~/... or a full path (~user is not supported)."
    return None


def _good_patterns(v) -> bool:
    return (isinstance(v, list) and len(v) <= 100 and
            all(isinstance(x, str) and 0 < len(x) <= 100 and "/" not in x for x in v))


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for k in ("terminal", "file_manager"):
        v = data.get(k)
        if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
            s[k] = v
    for k, lo in (("check_every_seconds", 5), ("timeout_seconds", 1),
                  ("trust_cache_seconds", 0), ("unlock_minutes", 1)):
        v = data.get(k)
        if type(v) is int and lo <= v <= 3600:
            s[k] = v
    v = data.get("download_dir")
    if isinstance(v, str) and v.strip() and "\n" not in v:
        s["download_dir"] = v
    if _good_patterns(data.get("junk")):
        s["junk"] = data["junk"]
    if s != data:  # write missing keys back so the file shows every setting
        write_private(SETTINGS_FILE, json.dumps(s, indent=2) + "\n")
    return s


def load_hosts() -> list[dict]:
    try:
        data = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out, seen = [], set()
    for d in data if isinstance(data, list) else []:
        if isinstance(d, dict):
            dest = d.get("dest")
            d = {k: d.get(k) for k in ("name", "host", "user", "port")}
            if check_device(d) is None and d["name"] not in seen:
                d["dest"] = dest if check_dest(dest) is None else DEFAULT_DEST
                seen.add(d["name"])
                out.append(d)
    return out


def alias(d: dict) -> str:
    return f"sshdeck-{d['name']}"


def kh_file(d: dict) -> Path:
    return KH_DIR / d["name"]


def save_hosts(hosts: list[dict], timeout: int) -> None:
    write_private(HOSTS_FILE, json.dumps(hosts, indent=2) + "\n")
    lines = ["# Made by SSH Deck. Edits here are overwritten.", ""]
    for d in hosts:
        lines += [
            f"Host {alias(d)}",
            f"    HostName {d['host']}",
            f"    Port {d['port']}",
            f"    User {d['user']}",
            f'    IdentityFile "{KEY}"',
            "    IdentitiesOnly yes",
            f'    UserKnownHostsFile "{kh_file(d)}"',
            "    StrictHostKeyChecking yes",
            f"    ConnectTimeout {timeout}",
            "    ServerAliveInterval 20",
            "    ForwardAgent no",
            "    ForwardX11 no",
            "",
        ]
    write_private(SSH_CFG, "\n".join(lines))


def ensure_key() -> None:
    if not (KEY.exists() and KEY.with_suffix(".pub").exists()):
        subprocess.run(
            [BIN["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-C", "sshdeck", "-f",
             str(KEY)], check=True, stdin=subprocess.DEVNULL, capture_output=True)
    os.chmod(KEY, 0o600)
    pub = KEY.with_suffix(".pub").read_text(encoding="utf-8").strip()
    write_private(INSTALL_PUB, f"{KEY_OPTIONS} {pub}\n")


def key_blob() -> str:
    parts = KEY.with_suffix(".pub").read_text(encoding="utf-8").split()
    return parts[1] if len(parts) >= 2 else ""


def key_encrypted() -> bool:
    r = subprocess.run([BIN["ssh-keygen"], "-y", "-P", "", "-f", str(KEY)],
                       stdin=subprocess.DEVNULL, capture_output=True)
    return r.returncode != 0


# ---------------------------------------------------------------- agent (optional passphrase)

class Agent:
    """SSH Deck's own ssh-agent, only used when the key has a passphrase.
    The key is unlocked once (dialog), kept for unlock_minutes, gone when the app exits."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.sock: Path | None = None
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self.lock:
            if self.running():
                return
            self.sock = run_tmp("agent-") / "agent.sock"
            self.proc = subprocess.Popen([BIN["ssh-agent"], "-D", "-a", str(self.sock)],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True)
            for _ in range(50):
                if self.sock.exists():
                    return
                time.sleep(0.05)
            raise RuntimeError("ssh-agent did not start")

    def env(self) -> dict:
        return {"SSH_AUTH_SOCK": str(self.sock)} if self.running() else {}

    def has_key(self) -> bool:
        if not self.running():
            return False
        r = subprocess.run([BIN["ssh-add"], "-L"], env=dict(os.environ, **self.env()),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return any(key_blob() in line for line in r.stdout.splitlines())

    def unlock(self, minutes: int, askpass: Path | None = None) -> bool:
        self.start()
        env = dict(os.environ, **self.env(), SSH_ASKPASS=str(askpass or ASKPASS),
                   SSH_ASKPASS_REQUIRE="force")
        env.pop("SSHDECK_PW_FIFO", None)
        r = subprocess.run([BIN["ssh-add"], "-t", str(minutes * 60), str(KEY)], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        return r.returncode == 0

    def lock_now(self) -> None:
        if self.running():
            subprocess.run([BIN["ssh-add"], "-D"], env=dict(os.environ, **self.env()),
                           stdin=subprocess.DEVNULL, capture_output=True)

    def stop(self) -> None:
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.sock is not None:
            shutil.rmtree(self.sock.parent, ignore_errors=True)
        self.proc = None


AGENT = Agent()


def ssh_env() -> dict:
    """Environment for every ssh/sftp child: our agent if it runs, never a password."""
    env = dict(os.environ)
    env.pop("SSHDECK_PW_FIFO", None)
    env.update(AGENT.env())
    return env


def ensure_unlocked(minutes: int, askpass: Path | None = None) -> bool:
    """True when the key can be used without a prompt (plain key, or unlocked in our agent)."""
    if not key_encrypted():
        return True
    if AGENT.has_key():
        return True
    return AGENT.unlock(minutes, askpass)


# ---------------------------------------------------------------- host keys

def fingerprints(text: str) -> list[str]:
    """Return 'SHA256:... (TYPE)' for each key line."""
    if not text.strip():
        return []
    r = subprocess.run([BIN["ssh-keygen"], "-lf", "-"], input=text,
                       capture_output=True, text=True, timeout=10)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append(f"{parts[1]} {parts[-1]}")
    return out


def key_set(text: str) -> set[tuple[str, str]]:
    keys = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#"):
            keys.add((parts[1], parts[2]))
    return keys


def check_hostkey(d: dict, timeout: int) -> dict:
    """Scan the device's host key and compare it with the saved one."""
    try:
        r = subprocess.run(
            [BIN["ssh-keyscan"], "-T", str(timeout), "-p", str(d["port"]),
             "-t", "ed25519,ecdsa,rsa", "--", d["host"]],
            capture_output=True, text=True, timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return {"state": "error", "msg": "The device did not answer in time."}
    scanned = "\n".join(l for l in r.stdout.splitlines() if KEYLINE_RE.match(l))
    if not scanned:
        return {"state": "error",
                "msg": "No SSH server answered on this IP and port.\n"
                       "Check that the device is on and its SSH server is running."}
    saved = kh_file(d).read_text(encoding="utf-8") if kh_file(d).exists() else ""
    res = {"lines": scanned + "\n", "new_fps": fingerprints(scanned)}
    if not key_set(saved):
        res["state"] = "new"
    elif key_set(saved) & key_set(scanned):
        res["state"] = "same"
    else:
        res["state"] = "changed"
        res["old_fps"] = fingerprints(saved)
    return res


# ---------------------------------------------------------------- key install / removal

class PasswordPipe:
    """Hands the password to askpass.sh through a one-time named pipe in a private
    runtime folder. Never in argv, environment values or a regular file."""

    def __init__(self, password: str, reads: int = 3):
        self.password, self.reads = password, reads
        self.dir = run_tmp("pw-")
        self.path = self.dir / "pipe"
        os.mkfifo(self.path, 0o600)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        for _ in range(self.reads):
            try:
                fd = os.open(self.path, os.O_WRONLY)  # blocks until askpass reads
            except OSError:
                return
            try:
                if not self.stop.is_set():
                    os.write(fd, (self.password + "\n").encode())
            except OSError:
                pass
            finally:
                os.close(fd)
            if self.stop.is_set():
                return

    def close(self):
        self.stop.set()
        try:  # unblock a writer still waiting in open()
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
        except OSError:
            pass
        self.thread.join(timeout=2)
        self.password = ""  # nosec B105 - clearing the password, not a hardcoded one
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_key(d: dict, timeout: int) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            [BIN["ssh"], "-F", str(SSH_CFG), "-o", "BatchMode=yes", alias(d), "true"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout + 10,
            env=ssh_env())
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return r.returncode == 0, r.stderr.strip()


def install_key(d: dict, password: str, timeout: int) -> tuple[bool, str]:
    if test_key(d, timeout)[0]:
        return True, "The key already works on this device."
    with PasswordPipe(password) as pipe:
        env = dict(ssh_env(), SSH_ASKPASS=str(ASKPASS), SSH_ASKPASS_REQUIRE="force",
                   SSHDECK_PW_FIFO=str(pipe.path))
        # -f: the line has key options ("restrict,pty"), so skip ssh-copy-id's own check;
        # test_key above already showed the key is not installed.
        cmd = [BIN["ssh-copy-id"], "-f", "-F", str(SSH_CFG), "-i", str(INSTALL_PUB),
               "-o", "NumberOfPasswordPrompts=1", "-o", "PubkeyAuthentication=no", alias(d)]
        try:
            r = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            return False, "Key setup took too long and was stopped."
    ok, _ = test_key(d, timeout)
    if ok:
        return True, ("Key installed (terminal and file transfer only).\n"
                      "From now on, no password is needed.")
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
    if "Permission denied" in tail:
        return False, "Wrong password (or password login is turned off on the device).\n\n" + tail
    return False, "Key setup failed:\n\n" + tail


def strip_our_key(text: str, blob: str) -> tuple[str, int]:
    """Remove authorized_keys lines that hold our key blob (any options in front)."""
    keep, removed = [], 0
    for line in text.splitlines():
        toks = line.split()
        hit = any(t in KEY_TYPES and i + 1 < len(toks) and toks[i + 1] == blob
                  for i, t in enumerate(toks))
        if hit and not line.lstrip().startswith("#"):
            removed += 1
        else:
            keep.append(line)
    return ("\n".join(keep) + "\n") if keep else "", removed


def sftp_batch(d: dict, lines: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run sftp batch commands (no remote shell). Lines must already be escaped."""
    tmp = run_tmp("batch-")
    try:
        batch = tmp / "batch"
        write_private(batch, "\n".join(lines) + "\n")
        return subprocess.run(
            [BIN["sftp"], "-q", "-F", str(SSH_CFG), "-o", "BatchMode=yes", "-b", str(batch),
             alias(d)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout + 60, env=ssh_env())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove_key_from_device(d: dict, timeout: int) -> tuple[bool, str]:
    """Take SSH Deck's key out of ~/.ssh/authorized_keys on the device, over sftp only.
    Other keys stay exactly as they were. The new file is written next to the old one
    and renamed over it (one step), with mode 600."""
    from .transfer import sftp_escape  # local import: transfer imports core
    tmp = run_tmp("ak-")
    try:
        got = tmp / "authorized_keys"
        r = sftp_batch(d, [f"get .ssh/authorized_keys {sftp_escape(str(got))}"], timeout)
        if r.returncode != 0 or not got.exists():
            return False, "Could not read ~/.ssh/authorized_keys on the device.\n" + \
                r.stderr.strip()[-300:]
        new, removed = strip_our_key(got.read_text(encoding="utf-8", errors="replace"),
                                     key_blob())
        if not removed:
            return True, "SSH Deck's key was not on the device."
        out = tmp / "new"
        write_private(out, new)
        r = sftp_batch(d, [f"put {sftp_escape(str(out))} .ssh/authorized_keys.sshdeck-new",
                           "chmod 600 .ssh/authorized_keys.sshdeck-new",
                           "rename .ssh/authorized_keys.sshdeck-new .ssh/authorized_keys"],
                       timeout)
        if r.returncode != 0:
            return False, "Could not write the new authorized_keys:\n" + r.stderr.strip()[-300:]
        if test_key(d, timeout)[0]:
            return False, "The key still works on the device; check ~/.ssh/authorized_keys."
        return True, f"Removed SSH Deck's key from the device ({removed} line)."
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- ~/.ssh/config include

def include_line() -> str:
    return f'Include "{SSH_CFG}"'


def has_include() -> bool:
    try:
        return include_line() in USER_SSH_CFG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def add_include() -> Path | None:
    """Put the Include line at the top of ~/.ssh/config. Returns the backup path."""
    USER_SSH_CFG.parent.mkdir(mode=0o700, exist_ok=True)
    old = USER_SSH_CFG.read_text(encoding="utf-8") if USER_SSH_CFG.exists() else ""
    backup = None
    if old:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = USER_SSH_CFG.with_name(f"config.bak-sshdeck-{stamp}")
        shutil.copy2(USER_SSH_CFG, backup)
        os.chmod(backup, 0o600)
    header = "# SSH Deck devices (sshdeck-<name>). Remove this line to undo.\n"
    write_private(USER_SSH_CFG, header + include_line() + "\n\n" + old)
    return backup
