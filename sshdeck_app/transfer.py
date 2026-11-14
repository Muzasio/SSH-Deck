"""SSH Deck file transfer: scan local files, plan sftp batches, run them with progress.

Everything goes over sftp (no remote shell): the device's login shell (bash, fish,
Windows cmd) never sees a file name. Batch arguments are backslash-escaped so names
with spaces, quotes, '#' or glob characters are taken literally.
"""
from __future__ import annotations

import fcntl
import fnmatch
import os
import posixpath
import pty
import re
import shutil
import signal
import stat
import struct
import subprocess
import termios
import time
from pathlib import Path

from PySide6 import QtCore

from . import core

LIST_CAP = 1000            # paths kept per warning list (counts are always exact)
VERIFY_MAX_FILES = 5000    # size check after upload up to this many files
VERIFY_MAX_DIRS = 500
BIG_FILES, BIG_BYTES = 10_000, 5 * 1024 ** 3
SENSITIVE_DIRS = {".ssh", ".gnupg", ".password-store", "keyrings", "sshdeck", ".aws", ".kube"}
# Always private by name
SECRET_NAME_RE = re.compile(
    r"^(?:id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|.*\.(?:p12|pfx|kdbx|keystore|jks)|"
    r"\.env(?:\..*)?|\.netrc|\.pgpass|credentials|shadow|authorized_keys)$", re.I)
# Private only if the content holds a private key (certificates are public)
MAYBE_KEY_RE = re.compile(r"^.*\.(?:pem|key|ovpn|ppk|asc)$", re.I)
PRIVATE_MARKERS = (b"PRIVATE KEY", b"<key>", b"PuTTY-User-Key-File", b"PRIVATE KEY BLOCK")
PROGRESS_RE = re.compile(r"^(?P<name>.*\S)\s+(?P<pct>\d{1,3})%\s+(?P<size>\S+)\s+(?P<rate>\S+/s)")
CMD_RE = re.compile(r"^sftp> (?P<cmd>put|get)\b")
_ESC_RE = re.compile(r'([\\\s"\'*?\[\]#{}])')


def sftp_escape(path: str) -> str:
    """Escape one argument for an sftp batch line (unquoted, every special char)."""
    if any(c in path for c in "\n\r\0"):
        raise ValueError("line breaks are not allowed in names")
    return _ESC_RE.sub(r"\\\1", path)


def remote_path(dest: str) -> str:
    """SFTP paths are relative to the home folder: '~' -> '.', '~/x' -> 'x'."""
    dest = dest.strip()
    if dest == "~":
        rp = "."
    elif dest.startswith("~/"):
        rp = dest[2:].lstrip("/") or "."
    else:
        rp = dest
    return "./" + rp if rp.startswith("-") else rp  # never looks like an option


def display_path(path: str, home: str | None) -> str:
    """Absolute remote path -> '~/x' when under the home folder."""
    if home and (path == home or path.startswith(home.rstrip("/") + "/")):
        rest = path[len(home.rstrip("/")):].lstrip("/")
        return "~/" + rest if rest else "~"
    return path


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def junk_matcher(patterns: list[str]):
    """One compiled regex for all junk patterns (fast on 100k+ files)."""
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in patterns))


def is_junk(rel: str, patterns) -> bool:
    rx = junk_matcher(patterns) if isinstance(patterns, list) else patterns
    return bool(rx) and any(rx.match(part) for part in rel.split("/"))


def valid_name(name: str) -> bool:
    return (bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name
            and not any(ord(c) < 32 or ord(c) == 127 for c in name) and len(name) <= 255)


# ---------------------------------------------------------------- scanning

class Scan:
    """Result of looking at dropped paths before anything is sent."""

    def __init__(self):
        self.items: list[dict] = []
        self.counts = {"symlinks": 0, "special": 0, "unreadable": 0, "sensitive": 0,
                       "badname": 0}
        self.lists: dict[str, list[str]] = {k: [] for k in self.counts}
        self.skipped: list[str] = []

    def note(self, kind: str, path: str):
        self.counts[kind] += 1
        if len(self.lists[kind]) < LIST_CAP:
            self.lists[kind].append(path)

    def count_text(self, kind: str) -> str:
        return str(self.counts[kind])


def _secret_by_content(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    return any(m in head for m in PRIVATE_MARKERS)


def _is_sensitive(path: Path, home: Path, is_file: bool) -> bool:
    name = path.name
    if name in SENSITIVE_DIRS or SECRET_NAME_RE.match(name):
        return True
    if is_file and MAYBE_KEY_RE.match(name) and _secret_by_content(path):
        return True
    try:
        rel = path.resolve().relative_to(home)
    except (ValueError, OSError):
        return False
    return any(part in SENSITIVE_DIRS for part in rel.parts)


def scan_local(paths: list[str]) -> Scan:
    """Walk dropped paths: sizes, links, special files, private files, bad names.
    Each item gets an ordered entry list: (rel, kind, size, mtime) with kind in
    dir / file / link_file / link_other / special (dirs come before their content)."""
    res = Scan()
    home = Path.home().resolve()
    for raw in dict.fromkeys(paths):
        p = Path(raw)
        if not p.is_absolute() or not p.name or not os.path.lexists(p):
            res.skipped.append(raw)
            continue
        if not valid_name(p.name) or any(c in raw for c in "\n\r"):
            res.note("badname", raw)
            continue
        item = {"path": str(p), "name": p.name, "dir": False, "entries": [], "files": 0,
                "bytes": 0}
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            res.note("symlinks", str(p))
            try:
                st = os.stat(p)
            except OSError:
                res.note("unreadable", str(p))
                continue
        if _is_sensitive(p, home, stat.S_ISREG(st.st_mode)):
            res.note("sensitive", str(p))
        if stat.S_ISREG(st.st_mode):
            item["entries"].append(("", "file", st.st_size, st.st_mtime))
            if not os.access(p, os.R_OK):
                res.note("unreadable", str(p))
        elif stat.S_ISDIR(st.st_mode):
            item["dir"] = True
            if not os.access(p, os.R_OK | os.X_OK):
                res.note("unreadable", str(p))
            _walk(p, item, res, home)
        else:
            res.note("special", str(p))
            continue
        item["files"] = sum(1 for e in item["entries"] if e[1] == "file")
        item["bytes"] = sum(e[2] for e in item["entries"] if e[1] == "file")
        res.items.append(item)
    return res


def _walk(top: Path, item: dict, res: Scan, home: Path):
    entries = item["entries"]
    for root, dirs, files in os.walk(top, followlinks=False,
                                     onerror=lambda e: res.note("unreadable", e.filename)):
        rel_root = os.path.relpath(root, top)
        rel_root = "" if rel_root == "." else rel_root
        for n in sorted(dirs) + sorted(files):
            fp = Path(root) / n
            rel = f"{rel_root}/{n}" if rel_root else n
            if not valid_name(n):
                res.note("badname", str(fp))
                if n in dirs:
                    dirs.remove(n)
                continue
            try:
                s = os.lstat(fp)
            except OSError:
                res.note("unreadable", str(fp))
                continue
            if stat.S_ISLNK(s.st_mode):
                res.note("symlinks", str(fp))
                try:
                    t = os.stat(fp)
                    kind = "link_file" if stat.S_ISREG(t.st_mode) else "link_other"
                    entries.append((rel, kind, t.st_size if kind == "link_file" else 0,
                                    t.st_mtime))
                except OSError:
                    entries.append((rel, "link_other", 0, 0))
            elif stat.S_ISDIR(s.st_mode):
                entries.append((rel, "dir", 0, 0))
                if n in SENSITIVE_DIRS:
                    res.note("sensitive", str(fp))
            elif stat.S_ISREG(s.st_mode):
                entries.append((rel, "file", s.st_size, s.st_mtime))
                if SECRET_NAME_RE.match(n) or (MAYBE_KEY_RE.match(n) and _secret_by_content(fp)):
                    res.note("sensitive", str(fp))
                if not os.access(fp, os.R_OK):
                    res.note("unreadable", str(fp))
            else:
                entries.append((rel, "special", 0, 0))
                res.note("special", str(fp))
        dirs.sort()


# ---------------------------------------------------------------- plans

def plan_upload(items: list[dict], dest: str, skip_junk: bool, follow_links: bool,
                junk: list[str]) -> dict:
    """Build sftp batch lines. Links to folders are never followed (loops, surprises);
    links to files are sent only when follow_links is on. Special files never."""
    base = remote_path(dest).rstrip("/") or "/"
    rx = junk_matcher(junk) if skip_junk else None
    lines, files, dirs = ["progress"], [], set()
    stats = {"files": 0, "bytes": 0, "junk": 0, "links_skipped": 0, "links_sent": 0}
    for item in items:
        top_remote = posixpath.join(base, item["name"])
        local_top = item["path"]
        if item["dir"]:
            lines.append(f"-mkdir {sftp_escape(top_remote)}")
            dirs.add(top_remote)
        skipped_dirs: set[str] = set()
        for rel, kind, size, mtime in item["entries"]:
            parent = posixpath.dirname(rel)
            if parent and parent in skipped_dirs:  # inside a skipped junk folder
                if kind == "dir":
                    skipped_dirs.add(rel)
                continue
            if rx is not None and rel and rx.match(posixpath.basename(rel)):
                stats["junk"] += 1
                if kind == "dir":
                    skipped_dirs.add(rel)
                continue
            if kind == "special":
                continue
            if kind == "link_other" or (kind == "link_file" and not follow_links):
                stats["links_skipped"] += 1
                continue
            local = os.path.join(local_top, rel) if rel else local_top
            remote = posixpath.join(top_remote, rel) if rel else top_remote
            if kind == "dir":
                lines.append(f"-mkdir {sftp_escape(remote)}")
                dirs.add(remote)
                continue
            if kind == "link_file":
                stats["links_sent"] += 1
            # -p keeps the modified time (and mode), so a later resume can tell a
            # complete file from a half-sent or different one.
            lines.append(f"put -p {sftp_escape(local)} {sftp_escape(remote)}")
            files.append((local, remote, size, mtime))
            stats["files"] += 1
            stats["bytes"] += size
    return {"lines": lines, "files": files, "dirs": dirs, "stats": stats, "all_files": files,
            "resumed": 0}


def ls_date(mtime: float) -> str:
    """The date text sftp's `ls -l` prints for this time (same client, same rules)."""
    now = time.time()
    fmt = "%b %e %H:%M" if now - 365 * 24 * 3600 / 2 < mtime <= now else "%b %e  %Y"
    return " ".join(time.strftime(fmt, time.localtime(mtime)).split())


def apply_resume(plan: dict, listing: dict[str, dict[str, tuple[int, str]]]) -> dict:
    """Drop files that are already complete on the device: same size AND same modified
    time (uploads use put -p). Anything else (missing, half-sent, different) is sent
    again in full; nothing is appended, so a different file can never be corrupted."""
    keep, skip_lines = [], set()
    for f in plan["files"]:
        local, remote, size, mtime = f
        got = listing.get(posixpath.dirname(remote) or ".", {}).get(posixpath.basename(remote))
        if got and got[0] == size and got[1] == ls_date(mtime):
            skip_lines.add(f"put -p {sftp_escape(local)} {sftp_escape(remote)}")
        else:
            keep.append(f)
    lines = [l for l in plan["lines"] if l not in skip_lines]
    stats = dict(plan["stats"], files=len(keep), bytes=sum(f[2] for f in keep))
    return dict(plan, lines=lines, files=keep, stats=stats,
                resumed=len(plan["files"]) - len(keep))


MAX_TREE_DEPTH, MAX_TREE_ENTRIES = 64, 500_000


def remote_tree(d: dict, entries: list[dict], timeout: int) -> dict:
    """Walk the picked remote items, one sftp session per folder level.
    Returns files [(rel, remote, size, date)], dirs [rel] and counts of skipped links /
    unsafe names. Links on the device are never followed (same rule as uploads)."""
    files, dirs = [], []
    counts = {"links": 0, "badname": 0, "too_deep": 0}
    level: list[tuple[str, str]] = []  # (rel, remote path) of folders to list
    for e in entries:
        if not valid_name(e["name"]):
            counts["badname"] += 1
        elif e["link"]:
            counts["links"] += 1
        elif e["dir"]:
            dirs.append(e["name"])
            level.append((e["name"], e["path"]))
        else:
            files.append((e["name"], e["path"], e["size"], " ".join(e["date"].split())))
    depth = 0
    while level:
        if depth >= MAX_TREE_DEPTH or len(files) + len(dirs) > MAX_TREE_ENTRIES:
            counts["too_deep"] += len(level)
            break
        r = core.sftp_batch(d, [f"-ls -lan {sftp_escape(rp)}" for _, rp in level],
                            timeout + len(level) // 2)
        by_dir: dict[str, list] = {}
        for line in r.stdout.splitlines():
            m = LS_RE.match(line)
            if m:
                p = m["path"].rstrip("/")
                by_dir.setdefault(posixpath.dirname(p), []).append((posixpath.basename(p), m))
        nxt = []
        for rel, rp in level:
            for name, m in by_dir.get(rp.rstrip("/"), []):
                if name in (".", ".."):
                    continue
                if not valid_name(name):
                    counts["badname"] += 1
                    continue
                child_rel, child_rp = f"{rel}/{name}", posixpath.join(rp, name)
                kind = m["perm"][0]
                if kind == "l":
                    counts["links"] += 1
                elif kind == "d":
                    dirs.append(child_rel)
                    nxt.append((child_rel, child_rp))
                elif kind == "-":
                    files.append((child_rel, child_rp, int(m["size"]),
                                  " ".join(m["date"].split())))
        level, depth = nxt, depth + 1
    return {"files": files, "dirs": dirs, "counts": counts}


def _inside_safely(base: str, rel: str) -> bool:
    """False if any existing part of base/rel (below base) is a symlink."""
    cur = base
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return False
    return True


def plan_download(tree: dict, local_dir: str, resume: bool) -> dict:
    """One `get -p` per file (keeps the device's modified time, so a later resume can tell
    complete files). Local folders are created before the job starts.
    Resume skips files that exist here with the same size and the same time."""
    lines, sizes, mkdirs = ["progress"], [], []
    stats = {"files": 0, "bytes": 0, "resumed": 0, "unsafe": 0}
    for rel in tree["dirs"]:
        if _inside_safely(local_dir, rel):
            mkdirs.append(os.path.join(local_dir, rel))
        else:
            stats["unsafe"] += 1
    for rel, remote, size, date in tree["files"]:
        local = os.path.join(local_dir, rel)
        if not _inside_safely(local_dir, rel):
            stats["unsafe"] += 1
            continue
        if resume and os.path.isfile(local):
            st = os.stat(local)
            if st.st_size == size and ls_date(st.st_mtime) == date:
                stats["resumed"] += 1
                continue
        lines.append(f"get -p {sftp_escape(remote)} {sftp_escape(local)}")
        sizes.append(size)
        stats["files"] += 1
        stats["bytes"] += size
    return {"lines": lines, "sizes": sizes, "mkdirs": mkdirs, "stats": stats}


def limit_kbit(mb_per_s: float) -> int:
    """sftp -l takes Kbit/s. 0 = no limit."""
    return int(round(mb_per_s * 8 * 1024)) if mb_per_s and mb_per_s > 0 else 0


# ---------------------------------------------------------------- remote listing

LS_RE = re.compile(r"^(?P<perm>[-dlbcps][-rwxsStT]{9})\S*\s+\S+\s+\d+\s+\d+\s+(?P<size>\d+)\s+"
                   r"(?P<date>\w{3}\s+\d+\s+[\d:]+)\s+(?P<path>.+)$")


def friendly_error(lines: list[str], rc) -> str:
    text = "\n".join(lines)
    low = text.lower()
    if "host key verification failed" in low or "identification has changed" in low:
        return "The device key is not trusted or has changed. Open the device again to check it."
    if "permission denied (publickey" in low or "permission denied (" in low:
        return "The key is not set up on this device (or it is locked). Use ⋯ → Set up key."
    if "connection refused" in low or "timed out" in low or "no route" in low \
            or "could not resolve" in low or "connection closed" in low:
        return "Can't reach the device. Is it on, with its SSH server running?"
    if "no space left" in low:
        return "The device is out of disk space."
    if "permission denied" in low:
        return "No permission there (read or write) on the device."
    if "not found" in low or "no such file" in low or "not a directory" in low:
        return "Folder or file not found on the device."
    last = lines[-1] if lines else f"exit code {rc}"
    return last[:300]


def _sftp_err(r: subprocess.CompletedProcess) -> list[str]:
    return [l for l in (r.stderr + "\n" + r.stdout).splitlines()
            if l.strip() and not l.startswith("sftp>")]


def remote_home(d: dict, timeout: int) -> str | None:
    r = core.sftp_batch(d, ["pwd"], timeout)
    m = re.search(r"Remote working directory: (.+)", r.stdout)
    return m.group(1).strip() if m and r.returncode == 0 else None


def list_remote(d: dict, path: str, timeout: int) -> tuple[bool, list[dict] | str]:
    """List a remote folder: (True, [{name, path, dir, link, size, date}]) or (False, msg)."""
    rp = remote_path(path) if path.startswith("~") or not path.startswith("/") else path
    r = core.sftp_batch(d, [f"ls -lan {sftp_escape(rp)}"], timeout)
    out, entries, is_dir = [], [], False
    for line in r.stdout.splitlines():
        if line.startswith("sftp>") or not line.strip():
            continue
        out.append(line)
        m = LS_RE.match(line)
        if not m:
            continue
        name = posixpath.basename(m["path"].rstrip("/"))
        if name == ".":
            is_dir = True
            continue
        if name == ".." or not valid_name(name):
            continue
        perm = m["perm"]
        entries.append({"name": name, "path": posixpath.join(rp, name), "dir": perm[0] == "d",
                        "link": perm[0] == "l", "size": int(m["size"]), "date": m["date"]})
    if is_dir:
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return True, entries
    if out and r.returncode == 0 and not r.stderr.strip():
        return False, "That path is a file, not a folder."
    return False, friendly_error(_sftp_err(r) or out, r.returncode)


def make_remote_dir(d: dict, path: str, timeout: int) -> tuple[bool, str]:
    rp = remote_path(path) if path.startswith("~") or not path.startswith("/") else path
    r = core.sftp_batch(d, [f"mkdir {sftp_escape(rp)}"], timeout)
    if r.returncode == 0:
        return True, "Folder created."
    return False, friendly_error(_sftp_err(r), r.returncode)


DF_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s*$")


def remote_free(d: dict, path: str, timeout: int) -> int | None:
    """Free bytes in that folder on the device, or None if the server can't tell
    (the statvfs sftp extension is missing, e.g. some Windows servers)."""
    rp = remote_path(path) if path.startswith("~") or not path.startswith("/") else path
    r = core.sftp_batch(d, [f"-df {sftp_escape(rp)}"], timeout)
    for line in r.stdout.splitlines():
        m = DF_RE.match(line)
        if m:
            return int(m.group(3)) * 1024  # sftp df prints 1K blocks
    return None


def local_free(path: str) -> int:
    return shutil.disk_usage(path).free


def remote_listing(d: dict, dirs, timeout: int) -> dict[str, dict[str, tuple[int, str]]]:
    """{dir: {name: (size, date text)}} for many folders in one sftp session.
    Missing folders are fine ('-ls' keeps going)."""
    dirs = sorted(set(dirs))
    if not dirs:
        return {}
    r = core.sftp_batch(d, [f"-ls -lan {sftp_escape(x)}" for x in dirs],
                        timeout + len(dirs) // 2)
    seen: dict[str, dict[str, tuple[int, str]]] = {}
    for line in r.stdout.splitlines():
        m = LS_RE.match(line)
        if m and m["perm"][0] != "d":
            p = m["path"].rstrip("/")
            seen.setdefault(posixpath.dirname(p) or ".", {})[posixpath.basename(p)] = \
                (int(m["size"]), " ".join(m["date"].split()))
    return seen


def verify_upload(d: dict, plan: dict, timeout: int) -> tuple[bool | None, str]:
    """Compare remote sizes with local ones. None = too big to check."""
    files = plan.get("all_files", plan["files"])
    by_dir: dict[str, dict[str, int]] = {}
    for _local, remote, size, _mtime in files:
        by_dir.setdefault(posixpath.dirname(remote) or ".", {})[posixpath.basename(remote)] = size
    if len(files) > VERIFY_MAX_FILES or len(by_dir) > VERIFY_MAX_DIRS:
        return None, "too many files to check sizes"
    lines = [f"-ls -lan {sftp_escape(dir_)}" for dir_ in by_dir]
    r = core.sftp_batch(d, lines, timeout + len(lines))
    seen: dict[str, dict[str, int]] = {}
    current = None
    for line in r.stdout.splitlines():
        if line.startswith("sftp> ls"):
            current = None
            continue
        m = LS_RE.match(line)
        if m:
            p = m["path"].rstrip("/")
            seen.setdefault(posixpath.dirname(p) or ".", {})[posixpath.basename(p)] = int(m["size"])
            current = p
    bad = 0
    for dir_, names in by_dir.items():
        got = seen.get(dir_, {})
        if not got:  # sftp may print dirs as "./x" vs "x"
            got = seen.get("./" + dir_, {}) or seen.get(dir_.lstrip("./"), {})
        for name, size in names.items():
            if got.get(name) != size:
                bad += 1
    if bad:
        return False, f"{bad} file(s) have a different size on the device"
    return True, f"{len(files)} file sizes match"


# ---------------------------------------------------------------- running sftp

class SftpJob(QtCore.QObject):
    """Runs one sftp batch in a pseudo-terminal (needed for its progress meter).
    'sftp> put/get' echo lines tell exactly which file is running."""
    progress = QtCore.Signal(float, str, str, int)  # fraction (-1 = unknown), file, speed, files done
    done = QtCore.Signal(bool, str)

    def __init__(self, d: dict, lines: list[str], sizes: list[int], parent=None,
                 limit: int = 0):
        super().__init__(parent)
        self.d, self.lines, self.sizes = d, lines, sizes
        self.limit = limit  # Kbit/s, 0 = no limit
        self.total = sum(sizes)
        self.proc: subprocess.Popen | None = None
        self.master = -1
        self.notifier = None
        self.tmp: Path | None = None
        self.buf, self.errors = "", []
        self.index, self.done_bytes, self.frac, self.cur_name = -1, 0, 0.0, None
        self.files_done = 0
        self.ended = self.cancelled = self.finishing = False

    def start(self):
        try:
            self.tmp = core.run_tmp("job-")
            batch = self.tmp / "batch"
            core.write_private(batch, "\n".join(self.lines) + "\n")
            cmd = [core.BIN["sftp"], "-F", str(core.SSH_CFG), "-o", "BatchMode=yes",
                   "-b", str(batch)]
            if self.limit > 0:
                cmd += ["-l", str(self.limit)]
            cmd.append(core.alias(self.d))
        except (OSError, ValueError) as e:
            self._cleanup()
            self._end(False, str(e))
            return
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 220, 0, 0))
        try:
            # setsid --ctty: own session with the pty as controlling terminal (needed
            # for the progress meter). Argument list only, never a shell.
            self.proc = subprocess.Popen([core.BIN["setsid"], "--ctty", *cmd], stdin=slave,
                                         stdout=slave, stderr=slave, close_fds=True,
                                         env=core.ssh_env())
        except OSError as e:
            os.close(master)
            os.close(slave)
            self._cleanup()
            self._end(False, str(e))
            return
        os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        self.notifier = QtCore.QSocketNotifier(master, QtCore.QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._read)

    def _read(self, *_):
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: all writers closed
            data = b""
        if not data:
            self._finish()
            return
        self.buf += data.decode("utf-8", "replace")
        parts = re.split(r"[\r\n]", self.buf)
        self.buf = parts.pop()
        for seg in parts:
            self._line(seg)

    def _line(self, seg: str):
        s = seg.strip()
        if not s:
            return
        if CMD_RE.match(s):
            if self.index >= 0:
                self.files_done += 1
                if self.index < len(self.sizes):
                    self.done_bytes += self.sizes[self.index]
            self.index += 1
            self.cur_name = None
            self._emit(0, "", "")
            return
        if s.startswith("sftp>") or s == "Progress meter enabled":
            return
        m = PROGRESS_RE.match(s)
        if not m:
            if len(self.errors) < 50:
                self.errors.append(s)
            return
        pct = min(int(m["pct"]), 100)
        name = m["name"].strip()
        if self.cur_name is not None and name != self.cur_name and not self.total:
            self.files_done += 1  # get -R: a new file inside the folder
        self.cur_name = name
        self._emit(pct, name, m["rate"])

    def _emit(self, pct: int, name: str, rate: str):
        if self.total:
            cur = self.sizes[self.index] if 0 <= self.index < len(self.sizes) else 0
            frac = (self.done_bytes + pct / 100 * cur) / self.total
            self.frac = max(self.frac, min(frac, 1.0))
            self.progress.emit(self.frac, name, rate, self.files_done)
        else:
            self.progress.emit(-1.0, name, rate, self.files_done)

    def _kill(self):
        try:
            if os.getpgid(self.proc.pid) == self.proc.pid:  # own session: kill sftp + ssh
                os.killpg(self.proc.pid, signal.SIGTERM)
            else:
                self.proc.terminate()
        except ProcessLookupError:
            pass

    def _cleanup(self):
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def _finish(self):
        if self.finishing:
            return
        self.finishing = True
        if self.notifier:
            self.notifier.setEnabled(False)
        if self.buf:
            self._line(self.buf)
            self.buf = ""
        if self.master >= 0:
            os.close(self.master)
            self.master = -1
        rc = None
        if self.proc:
            try:
                rc = self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill()
                rc = self.proc.wait()
        self._cleanup()
        if self.cancelled:
            self._end(False, "Cancelled")
        elif rc == 0:
            if self.index >= 0:
                self.files_done += 1
            self.progress.emit(1.0 if self.total else -1.0, "", "", self.files_done)
            self._end(True, "")
        else:
            self._end(False, friendly_error(self.errors, rc))

    def cancel(self):
        self.cancelled = True
        if self.proc is None:
            self._cleanup()
            self._end(False, "Cancelled")
            return
        if self.proc.poll() is None:
            self._kill()
        self._finish()

    def _end(self, ok: bool, msg: str):
        if not self.ended:
            self.ended = True
            self.done.emit(ok, msg)
