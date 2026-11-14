#!/usr/bin/env python3
"""Security and function tests for SSH Deck.

Runs fully in a throwaway sandbox: temp HOME / XDG folders and a non-root test sshd
on 127.0.0.1 with its own host key. The real ~/.ssh is never used.

Run:  python3 -I tests/test_security.py -v
"""
import getpass
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="sshdeck-test-"))
os.environ.update(
    HOME=str(SANDBOX / "home"),
    XDG_CONFIG_HOME=str(SANDBOX / "home" / ".config"),
    XDG_DATA_HOME=str(SANDBOX / "home" / ".local" / "share"),
    XDG_RUNTIME_DIR=str(SANDBOX / "run"),
    QT_QPA_PLATFORM="offscreen",
)
(SANDBOX / "home").mkdir()
(SANDBOX / "run").mkdir(mode=0o700)
sys.path.insert(0, str(APP_DIR))
from sshdeck_app import core as s, transfer as tr, ui as u  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402

s.resolve_bins()
s.BIN["notify-send"] = ""  # tests never pop up real desktop notifications
SSHD = shutil.which("sshd") or "/usr/bin/sshd"
SRV = SANDBOX / "srv"
PASSWORD = 'Zx9!pa ss"wo$(id)`id`;rd'  # nasty characters on purpose


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


PORT = free_port()
DEV = {"name": "local", "host": "127.0.0.1", "user": getpass.getuser(), "port": PORT,
       "dest": "~"}


def new_host_key():
    for f in ("hostkey", "hostkey.pub"):
        (SRV / f).unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(SRV / "hostkey")],
                   check=True)


def start_sshd():
    subprocess.run([SSHD, "-f", str(SRV / "sshd_config"), "-E", str(SRV / "log")], check=True)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("test sshd did not start")


def stop_sshd():
    pid = SRV / "pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 15)
        except ProcessLookupError:
            pass
        for _ in range(50):
            if not pid.exists():
                break
            time.sleep(0.1)
        time.sleep(0.3)


def authorize(line_prefix: str = s.KEY_OPTIONS):
    (SRV / "authorized_keys").write_text(
        f"ssh-ed25519 AAAAOTHERKEYONE other1\n{line_prefix} "
        f"{Path(str(s.KEY) + '.pub').read_text().strip()}\n"
        "# a comment line\nssh-rsa AAAAOTHERKEYTWO other2\n")


def trust():
    s.save_hosts([DEV], 5)
    s.write_private(s.kh_file(DEV), s.check_hostkey(DEV, 5)["lines"])


APP = None


def setUpModule():
    global APP
    SRV.mkdir()
    new_host_key()
    (SRV / "sshd_config").write_text(
        f"Port {PORT}\nListenAddress 127.0.0.1\nHostKey {SRV}/hostkey\nPidFile {SRV}/pid\n"
        f"AuthorizedKeysFile {SRV}/authorized_keys\nUsePAM no\nStrictModes no\n"
        "PasswordAuthentication yes\nKbdInteractiveAuthentication no\n"
        "PerSourcePenalties no\nSubsystem sftp /usr/lib/ssh/sftp-server\n")
    start_sshd()
    os.umask(0o077)
    s.ensure_dirs()
    s.ensure_key()
    APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def tearDownModule():
    s.AGENT.stop()
    stop_sshd()
    shutil.rmtree(SANDBOX, ignore_errors=True)


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def wait_for(cond, ms=20000):
    loop_until = time.monotonic() + ms / 1000
    while time.monotonic() < loop_until and not cond():
        APP.processEvents(QtCore.QEventLoop.AllEvents, 50)
        time.sleep(0.01)
    return cond()


def run_job(lines, sizes=None, cancel_after_ms=None, timeout_ms=60000):
    """Run one SftpJob inside a Qt event loop; return (ok, msg, progress list, job)."""
    job = tr.SftpJob(DEV, lines, sizes or [])
    loop = QtCore.QEventLoop()
    out, prog = {}, []
    job.progress.connect(lambda f, n, r, k: prog.append((f, k)))
    job.done.connect(lambda ok, msg: (out.update(ok=ok, msg=msg), loop.quit()))
    QtCore.QTimer.singleShot(timeout_ms, loop.quit)
    job.start()
    if cancel_after_ms is not None:
        QtCore.QTimer.singleShot(cancel_after_ms, job.cancel)
    if "ok" not in out:
        loop.exec()
    return out.get("ok"), out.get("msg"), prog, job


def upload(paths, dest, skip_junk=True, follow=False, junk=None):
    scan = tr.scan_local([str(p) for p in paths])
    plan = tr.plan_upload(scan.items, str(dest), skip_junk, follow,
                          junk if junk is not None else s.DEFAULT_SETTINGS["junk"])
    ok, msg, prog, job = run_job(plan["lines"], [f[2] for f in plan["files"]])
    return ok, msg, prog, plan, scan


# ------------------------------------------------------------------ input validation

class InputValidation(unittest.TestCase):
    def dev(self, **kw):
        return {**DEV, **kw}

    def test_good_values_pass(self):
        for kw in ({"name": "pc1"}, {"name": "pc-2"}, {"host": "192.0.2.10"},
                   {"host": "::1"}, {"host": "my-pc.local"}, {"user": "john.doe"},
                   {"user": "Admin_1"}, {"port": 1}, {"port": 65535}):
            self.assertIsNone(s.check_device(self.dev(**kw)), kw)

    def test_bad_names(self):
        for v in ("", "A b", "-x", "a/../b", "../x", "a\nb", "a" * 33, "ä", "a;rm",
                  "<b>x</b>", "a*", "a?", None, 5):
            self.assertIsNotNone(s.check_device(self.dev(name=v)), repr(v))

    def test_bad_hosts(self):
        for v in ("", "1.2.3.4\nHost *", "-oProxyCommand=x", "a b", "host;id", "$(id)",
                  "`id`", "x" * 300, "a..b", "%h", "host\tProxyCommand x", "a\rb",
                  "user@host", "host:22", None):
            self.assertIsNotNone(s.check_device(self.dev(host=v)), repr(v))

    def test_bad_users(self):
        for v in ("", "-oProxyCommand=x", "a b", "root\nHost *", "$(id)", "a@b", "a:b",
                  "a" * 65, "%u", "a\"b", None):
            self.assertIsNotNone(s.check_device(self.dev(user=v)), repr(v))

    def test_bad_ports(self):
        for v in (0, -1, 65536, "22", 22.0, True, False, None):
            self.assertIsNotNone(s.check_device(self.dev(port=v)), repr(v))

    def test_check_dest(self):
        for v in ("~", "~/Downloads", "~/My Files", "/srv/drop", "C:/Users/x", "rel/dir"):
            self.assertIsNone(s.check_dest(v), v)
        for v in ("", "   ", None, 5, "a\nb", "a\x00b", "a\x1b[31m", "~root/x", "~x",
                  "dir/*", "a?b", "[ab]", "x" * 1025):
            self.assertIsNotNone(s.check_dest(v), repr(v))

    def test_valid_name(self):
        for v in ("a", "a b", "a'b\"c", "#x", "ünï"):
            self.assertTrue(tr.valid_name(v), v)
        for v in ("", ".", "..", "a/b", "a\\b", "a\nb", "x" * 256):
            self.assertFalse(tr.valid_name(v), repr(v))


# ------------------------------------------------------------------ config and settings

class ConfigInjection(unittest.TestCase):
    def ssh_g(self, d):
        r = subprocess.run(["ssh", "-G", "-F", str(s.SSH_CFG), s.alias(d)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True)
        return dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)

    def test_generated_config_is_strict(self):
        s.save_hosts([DEV], 5)
        g = self.ssh_g(DEV)
        self.assertEqual(g["hostname"], "127.0.0.1")
        self.assertEqual(g["port"], str(PORT))
        self.assertEqual(g["stricthostkeychecking"], "true")
        self.assertEqual(g["identitiesonly"], "yes")
        self.assertEqual(g["forwardagent"], "no")
        self.assertEqual(g["userknownhostsfile"], str(s.kh_file(DEV)))
        self.assertNotIn("proxycommand", g)
        self.assertNotIn("localcommand", g)

    def test_only_expected_lines(self):
        s.save_hosts([DEV, {**DEV, "name": "two"}], 5)
        allowed = {"Host", "HostName", "Port", "User", "IdentityFile", "IdentitiesOnly",
                   "UserKnownHostsFile", "StrictHostKeyChecking", "ConnectTimeout",
                   "ServerAliveInterval", "ForwardAgent", "ForwardX11"}
        for line in s.SSH_CFG.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertIn(line.split()[0], allowed, line)

    def test_tampered_hosts_json(self):
        bad = [
            {"name": "ok", "host": "10.0.0.1", "user": "u", "port": 22, "dest": "~/ok"},
            {"name": "ok", "host": "10.0.0.2", "user": "u", "port": 22},
            {"name": "evil", "host": "1.2.3.4\nProxyCommand touch /tmp/x", "user": "u",
             "port": 22},
            {"name": "evil2", "host": "h", "user": "-oProxyCommand=x", "port": 22},
            {"name": "evil3", "host": "h", "user": "u", "port": True},
            {"name": "baddest", "host": "h", "user": "u", "port": 22, "dest": "x\ny"},
            "not a dict", 42, None,
        ]
        s.write_private(s.HOSTS_FILE, json.dumps(bad))
        hosts = s.load_hosts()
        self.assertEqual([(h["name"], h["dest"]) for h in hosts],
                         [("ok", "~/ok"), ("baddest", s.DEFAULT_DEST)])
        s.write_private(s.HOSTS_FILE, '{"not": "a list"}')
        self.assertEqual(s.load_hosts(), [])
        s.write_private(s.HOSTS_FILE, "garbage{{{")
        self.assertEqual(s.load_hosts(), [])

    def test_tampered_settings_json(self):
        s.write_private(s.SETTINGS_FILE, json.dumps({
            "terminal": "rm -rf ~", "file_manager": [], "check_every_seconds": 999999,
            "timeout_seconds": "5", "junk": ["a/b"], "download_dir": "x\ny",
            "unlock_minutes": True}))
        st = s.load_settings()
        for k in ("terminal", "file_manager", "check_every_seconds", "timeout_seconds",
                  "junk", "download_dir", "unlock_minutes"):
            self.assertEqual(st[k], s.DEFAULT_SETTINGS[k], k)
        self.assertEqual(json.loads(s.SETTINGS_FILE.read_text()), st)  # repaired on disk
        s.SETTINGS_FILE.unlink()


# ------------------------------------------------------------------ file safety

class FileSafety(unittest.TestCase):
    def test_permissions(self):
        s.save_hosts([DEV], 5)
        for d in (s.CFG_DIR, s.KH_DIR, s.RUN_DIR):
            self.assertEqual(mode(d), 0o700, d)
        for f in (s.KEY, s.SSH_CFG, s.HOSTS_FILE, s.INSTALL_PUB):
            self.assertEqual(mode(f), 0o600, f)

    def test_key_comment_and_install_line(self):
        pub = Path(str(s.KEY) + ".pub").read_text().split()
        self.assertEqual(pub[-1], "sshdeck")
        self.assertTrue(s.INSTALL_PUB.read_text().startswith("restrict,pty ssh-ed25519 "))

    def test_tmp_symlink_not_followed(self):
        target = SANDBOX / "victim.txt"
        target.write_text("original")
        path = s.CFG_DIR / "probe"
        (s.CFG_DIR / "probe.tmp").symlink_to(target)
        s.write_private(path, "new")
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(path.read_text(), "new")

    def test_final_symlink_replaced_not_followed(self):
        target = SANDBOX / "victim2.txt"
        target.write_text("original")
        path = s.CFG_DIR / "probe2"
        path.symlink_to(target)
        s.write_private(path, "new")
        self.assertEqual(target.read_text(), "original")
        self.assertFalse(path.is_symlink())

    def test_symlinked_config_dir_refused(self):
        link = SANDBOX / "linkdir"
        link.symlink_to(SANDBOX)
        with mock.patch.object(s, "CFG_DIR", link), mock.patch.object(s, "KH_DIR", link / "k"):
            with self.assertRaises(SystemExit):
                s.ensure_dirs()

    def test_run_tmp_private(self):
        d = s.run_tmp("t-")
        self.assertEqual(mode(d), 0o700)
        self.assertEqual(mode(s.RUN_DIR), 0o700)
        shutil.rmtree(d)

    def test_include_backup_and_order(self):
        cfg = s.USER_SSH_CFG
        cfg.parent.mkdir(mode=0o700, exist_ok=True)
        cfg.write_text("Host old\n    HostName 10.9.9.9\n")
        os.chmod(cfg, 0o644)
        bak = s.add_include()
        self.assertTrue(s.has_include())
        self.assertEqual(mode(bak), 0o600)
        self.assertEqual(mode(cfg), 0o600)
        self.assertEqual(bak.read_text(), "Host old\n    HostName 10.9.9.9\n")
        lines = cfg.read_text().splitlines()
        self.assertTrue(lines[1].startswith("Include "))
        r = subprocess.run(["ssh", "-G", "-F", str(cfg), "old"], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL)
        self.assertIn("hostname 10.9.9.9", r.stdout)

    def test_desktop_file(self):
        self.assertTrue(u.install_desktop_file())
        text = (SANDBOX / "home/.local/share/applications/sshdeck.desktop").read_text()
        self.assertNotIn("NoDisplay", text)
        self.assertIn('Exec="', text)
        self.assertIn(str(s.LAUNCHER), text)
        with mock.patch.object(s, "ICON", Path('/tmp/a"b.svg')):
            self.assertFalse(u.install_desktop_file())


# ------------------------------------------------------------------ host keys

class HostKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s.save_hosts([DEV], 5)
        s.kh_file(DEV).unlink(missing_ok=True)

    def test_1_unknown_host_refused_by_ssh(self):
        ok, err = s.test_key(DEV, 5)
        self.assertFalse(ok)
        self.assertIn("verification failed", err.lower())

    def test_2_new_then_same(self):
        r = s.check_hostkey(DEV, 5)
        self.assertEqual(r["state"], "new")
        self.assertTrue(r["new_fps"][0].startswith("SHA256:"))
        s.write_private(s.kh_file(DEV), r["lines"])
        self.assertEqual(s.check_hostkey(DEV, 5)["state"], "same")

    def test_3_changed_key_detected_and_refused(self):
        stop_sshd()
        new_host_key()
        start_sshd()
        r = s.check_hostkey(DEV, 5)
        self.assertEqual(r["state"], "changed")
        self.assertNotEqual(r["old_fps"], r["new_fps"])
        ok, err = s.test_key(DEV, 5)
        self.assertFalse(ok)
        self.assertIn("changed", err.lower())
        s.write_private(s.kh_file(DEV), r["lines"])

    def test_4_real_known_hosts_untouched(self):
        self.assertFalse((SANDBOX / "home/.ssh/known_hosts").exists())

    def test_keyline_filter(self):
        good = "127.0.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="
        self.assertTrue(s.KEYLINE_RE.match(good))
        for bad in ("@cert-authority * ssh-ed25519 AAAA", "@revoked * ssh-ed25519 AAAA",
                    good + " extra", "* ssh-ed25519 AAAA\n* ssh-rsa BBBB",
                    "h ssh-dss AAAA", "# comment", "h ssh-ed25519 AA AA"):
            self.assertFalse(s.KEYLINE_RE.match(bad), bad)

    def test_offline_port(self):
        self.assertEqual(s.check_hostkey({**DEV, "port": free_port()}, 2)["state"], "error")


# ------------------------------------------------------------------ passwords and keys

class PasswordAndKeys(unittest.TestCase):
    def test_askpass_reads_pipe_literally_no_exec(self):
        pipe = s.PasswordPipe(PASSWORD)
        try:
            r = subprocess.run([str(s.ASKPASS)], env={**os.environ,
                               "SSHDECK_PW_FIFO": str(pipe.path)},
                               capture_output=True, text=True, timeout=10)
        finally:
            pipe.close()
        self.assertEqual(r.stdout, PASSWORD + "\n")
        self.assertFalse(pipe.dir.exists(), "pipe folder must be removed")
        self.assertFalse((SANDBOX / "home/PWNED").exists())

    def test_pipe_close_without_reader(self):
        pipe = s.PasswordPipe("x")
        t0 = time.monotonic()
        pipe.close()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertFalse(pipe.thread.is_alive())

    def test_password_never_in_argv_env_or_disk(self):
        trust()
        (SRV / "authorized_keys").write_text("")
        calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **kw):
            calls.append((cmd, kw.get("env") or {}))
            return real_run(cmd, *a, **kw)

        with mock.patch.object(s.subprocess, "run", side_effect=spy):
            ok, msg = s.install_key(DEV, PASSWORD, 5)
        self.assertFalse(ok)  # a non-root test sshd cannot accept any password
        self.assertIn("password", msg.lower())
        self.assertNotIn(PASSWORD, msg)
        for cmd, env in calls:
            self.assertFalse(any(PASSWORD in str(c) for c in cmd), cmd)
            self.assertFalse(any(PASSWORD in str(v) for v in env.values()), "password in env")
        needle = PASSWORD.encode()
        for root in (SANDBOX / "home", SANDBOX / "run"):
            for p in root.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    self.assertNotIn(needle, p.read_bytes(), p)

    def test_proc_environ_private(self):
        self.assertEqual(mode(Path("/proc/self/environ")) & 0o077, 0)

    def test_restricted_key_allows_terminal_and_sftp(self):
        trust()
        authorize()
        self.assertTrue(s.test_key(DEV, 5)[0])
        ok, val = tr.list_remote(DEV, str(SANDBOX), 5)
        self.assertTrue(ok, val)

    def test_strip_our_key(self):
        blob = s.key_blob()
        text = (f"ssh-ed25519 AAAAOTHER a\nrestrict,pty ssh-ed25519 {blob} sshdeck\n"
                f"# ssh-ed25519 {blob} commented stays\nssh-ed25519 {blob} plain\n")
        new, n = s.strip_our_key(text, blob)
        self.assertEqual(n, 2)
        self.assertIn("AAAAOTHER", new)
        self.assertIn("commented stays", new)
        self.assertNotIn("sshdeck\n", new)

    def test_remove_key_from_device(self):
        """Over sftp only: our line goes, other keys stay, file mode 600.
        The batch runs with a 'cd' into a sandbox home that has its own .ssh."""
        trust()
        authorize()
        fake_home = SANDBOX / "fakehome"
        (fake_home / ".ssh").mkdir(parents=True, exist_ok=True)
        ak = fake_home / ".ssh/authorized_keys"
        shutil.copy(SRV / "authorized_keys", ak)
        real = s.sftp_batch

        def in_fake_home(d, lines, timeout):
            return real(d, [f"cd {tr.sftp_escape(str(fake_home))}"] + lines, timeout)

        with mock.patch.object(s, "sftp_batch", side_effect=in_fake_home), \
                mock.patch.object(s, "test_key", return_value=(False, "")):
            ok, msg = s.remove_key_from_device(DEV, 5)
        self.assertTrue(ok, msg)
        text = ak.read_text()
        self.assertNotIn(s.key_blob(), text)
        self.assertIn("AAAAOTHERKEYONE", text)
        self.assertIn("AAAAOTHERKEYTWO", text)
        self.assertIn("# a comment line", text)
        self.assertEqual(mode(ak), 0o600)
        self.assertFalse((fake_home / ".ssh/authorized_keys.sshdeck-new").exists())

    def test_passphrase_key_with_agent(self):
        """Encrypt the key, unlock it once in SSH Deck's own agent, use it with BatchMode."""
        trust()
        authorize()
        backup = SANDBOX / "keybackup"
        shutil.copy(s.KEY, backup)
        try:
            subprocess.run(["ssh-keygen", "-q", "-p", "-P", "", "-N", "test pass 1",
                            "-f", str(s.KEY)], check=True, capture_output=True)
            self.assertTrue(s.key_encrypted())
            self.assertFalse(s.test_key(DEV, 5)[0])  # locked: no prompt, just fails
            ask = SANDBOX / "ask.sh"
            ask.write_text('#!/bin/sh\nprintf "%s\\n" "test pass 1"\n')
            os.chmod(ask, 0o700)
            self.assertTrue(s.ensure_unlocked(1, askpass=ask))
            self.assertTrue(s.AGENT.has_key())
            self.assertEqual(mode(s.AGENT.sock.parent), 0o700)
            self.assertTrue(s.test_key(DEV, 5)[0])
            s.AGENT.lock_now()
            self.assertFalse(s.AGENT.has_key())
        finally:
            s.AGENT.stop()
            shutil.copy(backup, s.KEY)
            os.chmod(s.KEY, 0o600)
        self.assertFalse(s.key_encrypted())


# ------------------------------------------------------------------ scanning and plans

class ScanAndPlan(unittest.TestCase):
    def test_escape(self):
        self.assertEqual(tr.sftp_escape('a b"c*[x]#'), 'a\\ b\\"c\\*\\[x\\]\\#')
        with self.assertRaises(ValueError):
            tr.sftp_escape("a\nb")

    def test_remote_path(self):
        cases = {"~": ".", "~/": ".", "~/a": "a", "~//a": "a", "/x": "/x", "-x": "./-x",
                 "~/-x": "./-x", "rel": "rel"}
        for a, b in cases.items():
            self.assertEqual(tr.remote_path(a), b, a)
        self.assertEqual(tr.display_path("/data/u/x", "/data/u"), "~/x")
        self.assertEqual(tr.display_path("/data/u", "/data/u"), "~")
        self.assertEqual(tr.display_path("/data/user2", "/data/u"), "/data/user2")

    def test_scan_flags(self):
        root = SANDBOX / "scan"
        (root / "tree/sub").mkdir(parents=True)
        (root / "tree/a.txt").write_text("x" * 10)
        (root / "tree/sub/b.bin").write_bytes(b"y" * 20)
        (root / "tree/link").symlink_to("/etc/hostname")
        os.mkfifo(root / "tree/pipe")
        (root / "tree/.ssh").mkdir()
        (root / "tree/.ssh/id_ed25519").write_text("k")
        (root / "tree/ca.pem").write_text("-----BEGIN CERTIFICATE-----\nMIIB\n")
        (root / "tree/server.pem").write_text("-----BEGIN PRIVATE KEY-----\nMIIE\n")
        (root / "tree/vpn.ovpn").write_text("client\n<key>\nabc\n</key>\n")
        (root / "tree/secret.txt").write_text("s")
        os.chmod(root / "tree/secret.txt", 0)
        r = tr.scan_local([str(root / "tree"), "relative/path", str(root / "missing")])
        self.assertEqual(len(r.items), 1)
        self.assertEqual(r.items[0]["files"], 7)
        self.assertEqual(r.counts["symlinks"], 1)
        self.assertEqual([Path(x).name for x in r.lists["special"]], ["pipe"])
        sens = {Path(x).name for x in r.lists["sensitive"]}
        self.assertEqual(sens, {".ssh", "id_ed25519", "server.pem", "vpn.ovpn"})
        self.assertNotIn("ca.pem", sens, "public certificates are not secrets")
        self.assertTrue(any(x.endswith("secret.txt") for x in r.lists["unreadable"]))
        self.assertEqual(len(r.skipped), 2)
        os.chmod(root / "tree/secret.txt", 0o600)

    def test_counts_exact_past_cap(self):
        root = SANDBOX / "manylinks"
        root.mkdir()
        for i in range(tr.LIST_CAP + 25):
            (root / f"l{i}").symlink_to("/etc/hostname")
        r = tr.scan_local([str(root)])
        self.assertEqual(r.counts["symlinks"], tr.LIST_CAP + 25)
        self.assertEqual(len(r.lists["symlinks"]), tr.LIST_CAP)

    def test_plan_rules(self):
        root = SANDBOX / "plan"
        (root / "p/__pycache__").mkdir(parents=True)
        (root / "p/.git/objects").mkdir(parents=True)
        (root / "p/src").mkdir()
        (root / "p/src/m.py").write_text("print(1)")
        (root / "p/src/m.pyc").write_bytes(b"x")
        (root / "p/__pycache__/m.cpython.pyc").write_bytes(b"x")
        (root / "p/.git/objects/aa").write_bytes(b"x")
        (root / "outside.txt").write_text("OUT")
        (root / "p/flink").symlink_to(root / "outside.txt")
        (root / "p/dlink").symlink_to(root)
        os.mkfifo(root / "p/fifo")
        scan = tr.scan_local([str(root / "p")])
        junk = s.DEFAULT_SETTINGS["junk"]
        plan = tr.plan_upload(scan.items, "~/in", True, False, junk)
        puts = [l for l in plan["lines"] if l.startswith("put ")]
        self.assertEqual(len(puts), 1, puts)  # only src/m.py
        self.assertTrue(puts[0].endswith("in/p/src/m.py"))
        self.assertEqual(plan["stats"]["junk"], 3)  # __pycache__, .git, m.pyc
        self.assertEqual(plan["stats"]["links_skipped"], 2)
        self.assertEqual(plan["lines"][0], "progress")
        mk = [i for i, l in enumerate(plan["lines"]) if l.startswith("-mkdir") and "src" in l][0]
        self.assertLess(mk, plan["lines"].index(puts[0]), "mkdir before put")
        plan2 = tr.plan_upload(scan.items, "~/in", False, True, junk)
        self.assertEqual(plan2["stats"]["links_sent"], 1)  # file link only, never dir link
        self.assertEqual(plan2["stats"]["links_skipped"], 1)
        self.assertFalse(any("fifo" in l for l in plan2["lines"]))

    def test_plan_speed_big_tree(self):
        items = [{"path": "/x/big", "name": "big", "dir": True, "files": 0, "bytes": 0,
                  "entries": [(f"d{i}", "dir", 0, 0) for i in range(600)]
                  + [(f"d{i // 100}/f{i}.txt", "file", 10, 0) for i in range(60000)]}]
        t0 = time.monotonic()
        tr.plan_upload(items, "~", True, False, s.DEFAULT_SETTINGS["junk"])
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_friendly_errors(self):
        f = tr.friendly_error
        self.assertIn("key is not set up", f(["x: Permission denied (publickey)."], 1))
        self.assertIn("not trusted", f(["Host key verification failed."], 1))
        self.assertIn("No permission", f(['remote open "/x": Permission denied'], 1))
        self.assertIn("not found", f(['stat /x: No such file or directory'], 1))
        self.assertIn("reach", f(["ssh: connect to host x port 22: Connection refused"], 255))

    def test_download_plan_rejects_bad_names(self):
        with self.assertRaises(ValueError):
            tr.plan_download([{"name": "../evil", "path": "x", "dir": False, "size": 1}], "/tmp")
        p = tr.plan_download([{"name": "a b", "path": "/r/a b", "dir": True, "size": 0}], "/l")
        self.assertEqual(p["lines"][1], "get -R /r/a\\ b /l/a\\ b")
        self.assertFalse(p["known"])


# ------------------------------------------------------------------ live transfers

class TransferLive(unittest.TestCase):
    """Real sftp against the sandbox sshd with SSH Deck's own (restricted) key."""

    @classmethod
    def setUpClass(cls):
        trust()
        authorize()
        cls.remote = SANDBOX / "remote"
        cls.remote.mkdir(exist_ok=True)
        cls.src = SANDBOX / "src"
        cls.src.mkdir(exist_ok=True)

    def setUp(self):
        authorize()

    def test_list_remote(self):
        (self.remote / "listed/sub").mkdir(parents=True)
        (self.remote / "listed/one").write_text("1")
        (self.remote / 'listed/t"wo #x').write_text("22")
        (self.remote / "listed/ln").symlink_to("/etc/hostname")
        ok, entries = tr.list_remote(DEV, str(self.remote / "listed"), 5)
        self.assertTrue(ok, entries)
        by = {e["name"]: e for e in entries}
        self.assertEqual(set(by), {"one", 't"wo #x', "sub", "ln"})
        self.assertTrue(by["sub"]["dir"])
        self.assertTrue(by["ln"]["link"])
        self.assertEqual(by['t"wo #x']["size"], 2)
        self.assertEqual(entries[0]["name"], "sub", "folders first")
        ok, msg = tr.list_remote(DEV, str(self.remote / "nope"), 5)
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())
        ok, msg = tr.list_remote(DEV, str(self.remote / "listed/one"), 5)
        self.assertFalse(ok)
        self.assertIn("file", msg)

    def test_remote_home_and_mkdir(self):
        self.assertTrue(tr.remote_home(DEV, 5).startswith("/"))
        ok, msg = tr.make_remote_dir(DEV, str(self.remote / "made it"), 5)
        self.assertTrue(ok, msg)
        self.assertTrue((self.remote / "made it").is_dir())
        ok, msg = tr.make_remote_dir(DEV, str(self.remote / "no/such/deep"), 5)
        self.assertFalse(ok)

    def test_nasty_names_arrive_literally(self):
        # A shell would create "PWNED" in its working dir (home or the current dir).
        name = "a b;$(touch PWNED)`touch PWNED`'\"x #[1]*?{}"
        f = self.src / name
        f.write_bytes(os.urandom(300_000))
        (self.src / "a b;$(touch PWNED)`touch PWNED`'\"x #1").write_text("glob bait")
        dest = self.remote / "$(touch PWNED) d"
        dest.mkdir()
        ok, msg, prog, plan, _ = upload([f], dest)
        self.assertTrue(ok, msg)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), [name])
        self.assertEqual(sha(dest / name), sha(f))
        for where in (Path.cwd(), Path(os.path.expanduser("~" + getpass.getuser())),
                      SANDBOX, self.src, self.remote, dest):
            self.assertFalse((where / "PWNED").exists(), where)
        self.assertEqual(prog[-1][0], 1.0)
        good, note = tr.verify_upload(DEV, plan, 5)
        self.assertTrue(good, note)

    def test_folder_with_junk_and_links(self):
        tree = self.src / "proj"
        (tree / "x/y").mkdir(parents=True)
        (tree / "__pycache__").mkdir()
        (tree / "__pycache__/c.pyc").write_bytes(b"c")
        files = {"top.txt": 1000, "x/mid.bin": 200_000, "x/y/deep.bin": 50_000}
        for rel, n in files.items():
            (tree / rel).write_bytes(os.urandom(n))
        secret = self.src / "outside-secret.txt"
        secret.write_text("SECRET")
        (tree / "x/link.txt").symlink_to(secret)
        dest = self.remote / "got"
        dest.mkdir()
        ok, msg, prog, plan, _ = upload([tree], dest)
        self.assertTrue(ok, msg)
        for rel in files:
            self.assertEqual(sha(dest / "proj" / rel), sha(tree / rel), rel)
        self.assertFalse((dest / "proj/__pycache__").exists(), "junk skipped")
        self.assertFalse((dest / "proj/x/link.txt").exists(), "links skipped by default")
        fr = [p[0] for p in prog]
        self.assertTrue(all(b >= a for a, b in zip(fr, fr[1:])), "progress went back")
        self.assertEqual(prog[-1][1], 3, "files done count")
        self.assertTrue(tr.verify_upload(DEV, plan, 5)[0])
        (dest / "proj/x/mid.bin").write_bytes(b"short")
        good, note = tr.verify_upload(DEV, plan, 5)
        self.assertFalse(good)
        self.assertIn("1 file", note)

    def test_follow_links_when_asked(self):
        tree = self.src / "lnk"
        tree.mkdir()
        target = self.src / "target.txt"
        target.write_text("TARGET")
        (tree / "l.txt").symlink_to(target)
        dest = self.remote / "lnk-dest"
        dest.mkdir()
        ok, msg, _, plan, _ = upload([tree], dest, follow=True)
        self.assertTrue(ok, msg)
        self.assertEqual((dest / "lnk/l.txt").read_text(), "TARGET")
        self.assertFalse((dest / "lnk/l.txt").is_symlink())

    def test_missing_folder(self):
        f = self.src / "m.txt"
        f.write_text("m")
        ok, msg, _, _, _ = upload([f], self.remote / "no/such")
        self.assertFalse(ok)
        self.assertIn("not found", msg.lower())

    def test_download_file_and_folder(self):
        rsrc = self.remote / "dl"
        (rsrc / "inner").mkdir(parents=True)
        (rsrc / "inner/i.bin").write_bytes(os.urandom(70_000))
        (rsrc / "f q'#.txt").write_bytes(os.urandom(5000))
        local = SANDBOX / "downloads"
        local.mkdir()
        ok, entries = tr.list_remote(DEV, str(rsrc), 5)
        self.assertTrue(ok)
        plan = tr.plan_download(entries, str(local))
        ok, msg, prog, _ = run_job(plan["lines"],
                                   plan["sizes"] if plan["known"] else [0] * len(plan["sizes"]))
        self.assertTrue(ok, msg)
        self.assertEqual(sha(local / "f q'#.txt"), sha(rsrc / "f q'#.txt"))
        self.assertEqual(sha(local / "inner/i.bin"), sha(rsrc / "inner/i.bin"))
        self.assertEqual(mode(local / "inner/i.bin") & 0o077, 0, "umask 077 on downloads")

    def test_cancel_kills_everything(self):
        big = self.src / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(3 * 1024 ** 3)  # 3 GB sparse file: long enough to cancel
        dest = self.remote / "cancel"
        dest.mkdir()
        scan = tr.scan_local([str(big)])
        plan = tr.plan_upload(scan.items, str(dest), True, False, [])
        ok, msg, _, job = run_job(plan["lines"], [f[2] for f in plan["files"]],
                                  cancel_after_ms=400)
        self.assertFalse(ok)
        self.assertEqual(msg, "Cancelled")
        self.assertIsNotNone(job.proc.poll())
        r = subprocess.run(["pgrep", "-s", str(job.proc.pid)], capture_output=True)
        self.assertEqual(r.stdout.strip(), b"", "processes left in the sftp session")
        self.assertIsNone(job.tmp, "batch file folder removed")
        big.unlink()

    def test_no_key_message(self):
        (SRV / "authorized_keys").write_text("")
        f = self.src / "k.txt"
        f.write_text("k")
        ok, msg, _, _, _ = upload([f], self.remote)
        self.assertFalse(ok)
        self.assertIn("key is not set up", msg)

    def test_mtime_kept_and_ls_date_matches(self):
        f = self.src / "dated.txt"
        f.write_text("d")
        os.utime(f, (1_700_000_000, 1_700_000_000))  # an old date (year format)
        g = self.src / "recent.txt"
        g.write_text("r")
        dest = self.remote / "dated"
        dest.mkdir()
        ok, msg, _, _, _ = upload([f, g], dest)
        self.assertTrue(ok, msg)
        self.assertEqual(int((dest / "dated.txt").stat().st_mtime), 1_700_000_000)
        listing = tr.remote_listing(DEV, [str(dest)], 5)[str(dest)]
        self.assertEqual(listing["dated.txt"], (1, tr.ls_date(f.stat().st_mtime)))
        self.assertEqual(listing["recent.txt"], (1, tr.ls_date(g.stat().st_mtime)))

    def test_resume_sends_only_missing_or_changed(self):
        tree = self.src / "res"
        (tree / "sub").mkdir(parents=True)
        for i in range(6):
            (tree / f"sub/f{i}.bin").write_bytes(os.urandom(40_000))
        dest = self.remote / "resume"
        dest.mkdir()
        ok, msg, _, plan, scan = upload([tree], dest)
        self.assertTrue(ok, msg)
        got = dest / "res/sub"
        (got / "f0.bin").unlink()                                   # missing
        with open(got / "f1.bin", "r+b") as fh:                     # half-sent
            fh.truncate(10_000)
        (got / "f2.bin").write_bytes(os.urandom(40_000))            # same size, other time
        os.utime(got / "f2.bin", (1_600_000_000, 1_600_000_000))
        (tree / "sub/f3.bin").write_bytes(os.urandom(40_000))       # changed locally later
        later = time.time() + 3600
        os.utime(tree / "sub/f3.bin", (later - 7200, later - 7200))
        scan = tr.scan_local([str(tree)])
        plan = tr.plan_upload(scan.items, str(dest), True, False, [])
        dirs = {os.path.dirname(f[1]) for f in plan["files"]}
        res = tr.apply_resume(plan, tr.remote_listing(DEV, dirs, 5))
        sent = sorted(os.path.basename(f[1]) for f in res["files"])
        self.assertEqual(sent, ["f0.bin", "f1.bin", "f2.bin", "f3.bin"])
        self.assertEqual(res["resumed"], 2)
        self.assertEqual(res["stats"]["files"], 4)
        before = (got / "f4.bin").stat().st_ino, (got / "f5.bin").stat().st_ino
        ok, msg, _, _ = run_job(res["lines"], [f[2] for f in res["files"]])
        self.assertTrue(ok, msg)
        for i in range(6):
            self.assertEqual(sha(got / f"f{i}.bin"), sha(tree / f"sub/f{i}.bin"), i)
        self.assertEqual(((got / "f4.bin").stat().st_ino, (got / "f5.bin").stat().st_ino),
                         before, "complete files were not touched")
        self.assertTrue(tr.verify_upload(DEV, res, 5)[0])

    def test_remote_free_space(self):
        free = tr.remote_free(DEV, str(self.remote), 5)
        real = shutil.disk_usage(self.remote).free
        self.assertIsNotNone(free)
        self.assertLess(abs(free - real), max(real * 0.05, 64 * 1024 ** 2))
        self.assertIsNone(tr.remote_free(DEV, str(self.remote / "nope"), 5))
        self.assertGreater(tr.local_free(str(SANDBOX)), 0)

    def test_z_batch_files_cleaned(self):
        left = [p.name for p in s.RUN_DIR.iterdir() if p.name.startswith(("job-", "batch-"))]
        self.assertEqual(left, [])


# ------------------------------------------------------------------ GUI behaviour

class GuiBehaviour(unittest.TestCase):
    def setUp(self):
        trust()
        authorize()
        s.write_private(s.HOSTS_FILE, json.dumps([DEV]))
        self.w = u.MainWindow()
        self.w.show()

    def tearDown(self):
        for t in list(self.w.dev_tabs.values()):
            t.alive = False
        self.w.close()
        self.w.deleteLater()
        APP.processEvents()

    def test_gate_queues_instead_of_dropping(self):
        calls = []
        self.w.gate(DEV, lambda: calls.append(1))
        self.w.gate(DEV, lambda: calls.append(2))  # arrives while the first check runs
        self.assertTrue(wait_for(lambda: len(calls) == 2), calls)
        self.assertEqual(sorted(calls), [1, 2])
        calls.clear()
        self.w.gate(DEV, lambda: calls.append(3))  # cached now
        self.assertTrue(wait_for(lambda: calls == [3]))

    def test_closed_tab_ignores_late_results(self):
        tab = self.w.open_tab(self.w.hosts[0])
        hit = []
        self.w.run(lambda: time.sleep(0.3) or "late", tab.guard(lambda r: hit.append(r)))
        self.w.close_tab(self.w.tabs.indexOf(tab))
        wait_for(lambda: not self.w.pending, 3000)
        self.assertEqual(hit, [])

    def test_ping_skipped_when_hidden(self):
        row = self.w.list.itemWidget(self.w.list.item(0))
        wait_for(lambda: row.sock is None, 6000)
        self.w.hide()
        self.w.ping_all()
        self.assertIsNone(row.sock)
        self.w.show()
        self.w.ping_all()
        self.assertIsNotNone(row.sock)

    def test_full_send_flow(self):
        """Drop -> scan -> gate -> list -> confirm dialog (auto-accepted) -> sftp -> verify."""
        tab = self.w.open_tab(self.w.hosts[0])
        dest = SANDBOX / "remote/flow"
        dest.mkdir(parents=True, exist_ok=True)
        tab.dest.setText(str(dest))
        f = SANDBOX / "src/flow.txt"
        f.parent.mkdir(exist_ok=True)
        f.write_text("flow")

        def accept_dialog():
            for wdg in APP.topLevelWidgets():
                if isinstance(wdg, u.SendDialog) and wdg.isVisible():
                    wdg._accept("all")
                    return
            QtCore.QTimer.singleShot(50, accept_dialog)

        QtCore.QTimer.singleShot(50, accept_dialog)
        tab.send([str(f)])
        rows = lambda: [tab.tl.itemAt(i).widget() for i in range(tab.tl.count())
                        if isinstance(tab.tl.itemAt(i).widget(), u.TransferRow)]
        self.assertTrue(wait_for(lambda: rows() and rows()[0].finished, 30000))
        self.assertIn("✓", rows()[0].status.text())
        self.assertIn("sizes match", rows()[0].status.text())
        self.assertEqual((dest / "flow.txt").read_text(), "flow")

    def test_send_dialog_space_rules(self):
        f = SANDBOX / "src/space.bin"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"x" * 5000)
        scan = tr.scan_local([str(f)])
        junk = s.DEFAULT_SETTINGS["junk"]
        dlg = u.SendDialog(self.w, DEV, "~", scan, [], junk, free=1000)
        self.assertFalse(dlg.send_b.isEnabled(), "not enough space must block Send")
        self.assertIn("Not enough space", dlg.space.text())
        dlg = u.SendDialog(self.w, DEV, "~", scan, [], junk, free=10 ** 9)
        self.assertTrue(dlg.send_b.isEnabled())
        dlg = u.SendDialog(self.w, DEV, "~", scan, [], junk, free=None)
        self.assertTrue(dlg.send_b.isEnabled())
        self.assertIn("unknown", dlg.space.text())
        dlg = u.SendDialog(self.w, DEV, "~", scan, ["space.bin"], junk, free=1000)
        self.assertTrue(dlg.resume.isChecked(), "resume on when names clash")
        self.assertTrue(dlg.send_b.isEnabled(), "with resume, checked again later")
        dlg.resume.setChecked(False)
        self.assertFalse(dlg.send_b.isEnabled())

    def test_single_instance(self):
        path = s.RUN_DIR / "test-ui.sock"
        first = u.single_instance(path)
        self.assertIsNotNone(first)
        got = []
        first.newConnection.connect(lambda: got.append(1))
        self.assertIsNone(u.single_instance(path))
        self.assertTrue(wait_for(lambda: got, 3000))
        first.close()

    def test_guess_prefix_shape(self):
        p = u.guess_prefix([])
        if p:
            import ipaddress
            self.assertRegex(p, r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.$")
            self.assertTrue(ipaddress.ip_address(p + "1").is_private)

    def test_terminal_wrapper_no_injection(self):
        self.w.settings["terminal"] = []
        marker = SANDBOX / "pwned2"
        cmd = self.w.in_terminal(["echo", f"a; touch {marker}", f"$(touch {marker})"])
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.stdout, f"a; touch {marker} $(touch {marker})\n")
        self.assertFalse(marker.exists())


class StaticChecks(unittest.TestCase):
    def test_no_shell_true_anywhere(self):
        for f in (APP_DIR / "sshdeck_app").glob("*.py"):
            src = f.read_text()
            self.assertNotIn("shell=True", src, f)
            self.assertNotIn("os.system", src, f)


if __name__ == "__main__":
    unittest.main()
