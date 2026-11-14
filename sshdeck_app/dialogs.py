"""SSH Deck dialogs: add/edit device, send box, settings."""
from __future__ import annotations

import os
import shlex
import shutil

from PySide6 import QtCore, QtWidgets

from . import core, transfer as tr
from .theme import GREEN, RED, _more, color_label, label


# ---------------------------------------------------------------- dialogs

class PrefixEdit(QtWidgets.QLineEdit):
    """On focus, put the cursor at the end instead of selecting the pre-filled text."""
    def focusInEvent(self, e):
        super().focusInEvent(e)
        QtCore.QTimer.singleShot(0, self, lambda: (self.deselect(), self.end(False)))


class DeviceDialog(QtWidgets.QDialog):
    def __init__(self, parent, device: dict | None, taken: set[str], prefix: str = ""):
        super().__init__(parent)
        self.taken = taken
        self.setWindowTitle("Edit device" if device else "Add device")
        self.name = QtWidgets.QLineEdit(device["name"] if device else "")
        self.name.setPlaceholderText("e.g. laptop")
        self.host = PrefixEdit(device["host"] if device else prefix)
        self.host.setPlaceholderText("e.g. 192.168.1.20")
        self.user = QtWidgets.QLineEdit(device["user"] if device else "")
        self.user.setPlaceholderText("login name on the device (the part before @)")
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(device["port"] if device else 22)
        self.pw = QtWidgets.QLineEdit()
        self.pw.setEchoMode(QtWidgets.QLineEdit.Password)
        self.pw.setPlaceholderText("optional: sets up a key, not saved")
        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("IP / host", self.host)
        form.addRow("User", self.user)
        form.addRow("Port", self.port)
        form.addRow("Password", self.pw)
        hint = label("Password is used once to install a key on the device, then forgotten.\n"
                     "Leave it empty if the key is already set up.", "muted")
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(hint)
        lay.addWidget(btns)
        self.resize(440, 0)

    def device(self) -> dict:
        return {"name": self.name.text().strip().lower(), "host": self.host.text().strip(),
                "user": self.user.text().strip(), "port": self.port.value()}

    def _ok(self):
        d = self.device()
        err = core.check_device(d)
        if not err and d["name"] in self.taken:
            err = "Name: another device already uses this name."
        if err:
            QtWidgets.QMessageBox.warning(self, "Check the fields", err)
            return
        self.accept()


def _more(lst: list[str], total: int, show: int = 4) -> list[str]:
    out = [f"   {p}" for p in lst[:show]]
    if total > show:
        out.append(f"   … and {total - show} more")
    return out


class SendDialog(QtWidgets.QDialog):
    """Confirm box before sending: summary, options (junk, links) and warnings."""

    def __init__(self, parent, d: dict, dest: str, scan: tr.Scan, clash: list[str],
                 junk: list[str], free: int | None = None, speed_mb_s: float = 0):
        super().__init__(parent)
        self.d, self.dest, self.scan, self.clash, self.junk = d, dest, scan, clash, junk
        self.free, self.speed = free, speed_mb_s
        self._plans: dict[tuple, dict] = {}  # computed once per option set (big folders)
        self.choice: str | None = None
        self.plan: dict | None = None
        n = len(scan.items)
        self.setWindowTitle("Send files")
        title = label(f"Send {n} item{'s' * (n != 1)} to {d['name']}?", "h2")
        self.summary = label("", "muted")
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        full = self._plan("all", False, True)["stats"]
        self.skip = QtWidgets.QCheckBox()
        self.skip.setChecked(True)
        junk_n = self._plan("all", True, True)["stats"]["junk"]
        self.skip.setText(f"Skip junk: {junk_n} item(s) like {', '.join(junk[:3])}…")
        self.skip.setVisible(junk_n > 0)
        self.follow = QtWidgets.QCheckBox(
            f"Also send the files that links point to ({scan.counts['symlinks']} link(s))")
        self.follow.setToolTip("Links to files are sent as normal files. Links to folders are "
                               "never followed.")
        self.follow.setVisible(scan.counts["symlinks"] > 0 and full["links_sent"] > 0)
        self.resume = QtWidgets.QCheckBox("Resume: skip files already complete on the device")
        self.resume.setToolTip("Complete = same size and same modified time (to the minute).\n"
                               "Half-sent or changed files are sent again in full.")
        self.resume.setChecked(bool(clash))
        self.resume.setVisible(bool(clash))
        self.space = label("")
        self.space.setWordWrap(True)
        for cb in (self.skip, self.follow, self.resume):
            cb.toggled.connect(self.recompute)

        warn: list[str] = []
        c, L = scan.counts, scan.lists
        if c["sensitive"]:
            warn.append("⚠ Looks private (private keys, passwords, secrets):")
            warn += _more(L["sensitive"], c["sensitive"])
        if c["symlinks"]:
            warn.append(f"• {c['symlinks']} link(s) are skipped unless you tick the box above.")
        if c["unreadable"]:
            warn.append(f"⚠ {c['unreadable']} item(s) can't be read; sending stops there:")
            warn += _more(L["unreadable"], c["unreadable"], 2)
        if c["special"]:
            warn.append(f"• {c['special']} special file(s) (pipes, sockets, devices) are skipped.")
        if c["badname"]:
            warn.append(f"• {c['badname']} item(s) with line breaks or unsafe names are skipped.")
        if clash:
            warn.append("Already on the device (will be replaced / merged):")
            warn += _more(clash, len(clash), 5)
        self.warn = label("\n".join(warn), "warn" if c["sensitive"] else None)
        self.warn.setWordWrap(True)
        self.warn.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.warn.setVisible(bool(warn))

        bb = QtWidgets.QDialogButtonBox()
        self.send_b = bb.addButton("Replace and send" if clash else "Send",
                                   QtWidgets.QDialogButtonBox.AcceptRole)
        self.new_b = None
        if clash and len(clash) < n:
            self.new_b = bb.addButton("Send new only", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self.send_b.clicked.connect(lambda: self._accept("all"))
        if self.new_b:
            self.new_b.clicked.connect(lambda: self._accept("new"))
        cancel.clicked.connect(self.reject)
        (cancel if c["sensitive"] else (self.new_b or self.send_b)).setDefault(True)
        self.send_b.setProperty("primary", not c["sensitive"])

        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(10)
        lay.addWidget(title)
        lay.addWidget(self.summary)
        lay.addWidget(self.space)
        lay.addWidget(self.skip)
        lay.addWidget(self.follow)
        lay.addWidget(self.resume)
        lay.addWidget(self.warn)
        lay.addWidget(bb)
        self.setMinimumWidth(520)
        self.recompute()

    def items_for(self, choice: str) -> list[dict]:
        if choice == "new":
            return [i for i in self.scan.items if i["name"] not in self.clash]
        return self.scan.items

    def _plan(self, choice: str, skip: bool, follow: bool) -> dict:
        key = (choice, skip, follow)
        if key not in self._plans:
            self._plans[key] = tr.plan_upload(self.items_for(choice), self.dest, skip, follow,
                                              self.junk)
        return self._plans[key]

    def recompute(self, *_):
        plan = self._plan("all", self.skip.isChecked(), self.follow.isChecked())
        st = plan["stats"]
        big = st["files"] >= tr.BIG_FILES or st["bytes"] >= tr.BIG_BYTES
        lines = [f"{st['files']} files  ·  {tr.human(st['bytes'])}",
                 f"To: {self.d['user']}@{self.d['host']}:{self.dest}"]
        if self.speed:
            lines.append(f"Speed limit: {self.speed:g} MB/s (change it in Settings)")
        if big:
            lines.append("Large send: this can take a long time.")
        self.summary.setText("\n".join(lines))
        fits = True
        if self.free is None:
            color_label(self.space, "Free space on the device: unknown (not checked).", None)
        elif st["bytes"] > self.free:
            if self.resume.isChecked():  # resume may need less; checked again before sending
                color_label(self.space, f"Free on the device: {tr.human(self.free)}. Needs up "
                            f"to {tr.human(st['bytes'])}; checked again after resume.",
                            "#f59e0b")
            else:
                fits = False
                color_label(self.space, f"✕ Not enough space on the device: needs "
                            f"{tr.human(st['bytes'])}, free {tr.human(self.free)}.", RED)
        else:
            color_label(self.space, f"Free on the device: {tr.human(self.free)}  ✓", GREEN)
        self.send_b.setEnabled(fits and (st["files"] > 0 or any(i["dir"] for i in self.scan.items)))
        if self.new_b:
            self.new_b.setEnabled(fits)

    def _accept(self, choice: str):
        self.choice = choice
        self.plan = self._plan(choice, self.skip.isChecked(), self.follow.isChecked())
        self.accept()


class SettingsDialog(QtWidgets.QDialog):
    """All settings in one plain form. Values go through the same checks as at start."""

    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.result_settings: dict | None = None
        s = settings

        def secs(lo, hi, value, suffix):
            w = QtWidgets.QSpinBox()
            w.setRange(lo, hi)
            w.setValue(value)
            w.setSuffix(suffix)
            return w

        self.every = secs(5, 3600, s["check_every_seconds"], " s")
        self.timeout = secs(1, 60, min(s["timeout_seconds"], 60), " s")
        self.trust = secs(0, 3600, s["trust_cache_seconds"], " s")
        self.trust.setSpecialValueText("Every time")
        self.unlock = secs(1, 1440, min(s["unlock_minutes"], 1440), " min")
        self.speed = QtWidgets.QDoubleSpinBox()
        self.speed.setRange(0, 10_000)
        self.speed.setDecimals(1)
        self.speed.setSingleStep(0.5)
        self.speed.setSuffix(" MB/s")
        self.speed.setSpecialValueText("No limit")
        self.speed.setValue(float(s["speed_limit_mb_s"]))
        self.dl = QtWidgets.QLineEdit(s["download_dir"])
        pick = QtWidgets.QPushButton("Choose…")
        pick.clicked.connect(self._pick)
        dl_row = QtWidgets.QHBoxLayout()
        dl_row.addWidget(self.dl, 1)
        dl_row.addWidget(pick)
        self.junk = QtWidgets.QPlainTextEdit("\n".join(s["junk"]))
        self.junk.setMinimumHeight(110)
        self.junk.setMaximumHeight(110)
        self.terminal = QtWidgets.QLineEdit(shlex.join(s["terminal"]))
        self.fileman = QtWidgets.QLineEdit(shlex.join(s["file_manager"]))
        self.error = label("")
        self.error.setWordWrap(True)

        for w in (self.every, self.timeout, self.trust, self.unlock, self.speed):
            w.setFixedWidth(150)
        sections = (
            ("Connection", [
                ("Check if devices are online every", self.every, None),
                ("Connect timeout", self.timeout, None),
                ("Check the device key again after", self.trust,
                 "SSH always checks it. This only skips the fingerprint box."),
                ("Keep a passphrase key unlocked for", self.unlock, None),
            ]),
            ("Transfers", [
                ("Speed limit", self.speed, "For sending and downloading."),
                ("Download folder", dl_row, None),
                ("Skip these names", self.junk,
                 "Used when “Skip junk” is ticked. One per line; * matches anything."),
            ]),
            ("Apps", [
                ("Terminal", self.terminal, "The ssh command is added at the end, "
                 "e.g. kitty -e"),
                ("File manager", self.fileman, "Opens sftp:// addresses."),
            ]),
        )
        # One grid for every section, so all labels and fields line up.
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        r = 0
        for i, (title, rows) in enumerate(sections):
            if i:
                grid.setRowMinimumHeight(r, 10)
                r += 1
            grid.addWidget(label(title, "h2"), r, 0, 1, 2)
            r += 1
            for text, field, hint in rows:
                name = label(text)
                tall = field is self.junk
                if tall:  # line the label up with the first line of the list
                    name.setContentsMargins(0, 7, 0, 0)
                grid.addWidget(name, r, 0, QtCore.Qt.AlignRight | (
                    QtCore.Qt.AlignTop if tall else QtCore.Qt.AlignVCenter))
                if isinstance(field, QtWidgets.QLayout):
                    grid.addLayout(field, r, 1)
                elif field in (self.every, self.timeout, self.trust, self.unlock, self.speed):
                    grid.addWidget(field, r, 1, QtCore.Qt.AlignLeft)
                else:
                    grid.addWidget(field, r, 1)
                r += 1
                if hint:  # own row under the field: Qt sizes wrapped text reliably here
                    h = label(hint, "muted")
                    h.setWordWrap(True)
                    grid.addWidget(h, r, 1)
                    r += 1
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.addLayout(grid)
        lay.addWidget(self.error)
        bb = QtWidgets.QDialogButtonBox()
        reset = bb.addButton("Reset to defaults", QtWidgets.QDialogButtonBox.ResetRole)
        bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        save = bb.addButton("Save settings", QtWidgets.QDialogButtonBox.AcceptRole)
        save.setProperty("primary", True)
        save.setDefault(True)
        reset.clicked.connect(self._reset)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setMinimumWidth(640)
        self.setMinimumHeight(self.sizeHint().height())  # never squeeze the fields

    def _pick(self):
        start = os.path.expanduser(self.dl.text().strip() or "~")
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Download folder", start)
        if path:
            home = os.path.expanduser("~")
            self.dl.setText("~" + path[len(home):] if path.startswith(home + "/") else path)

    def _reset(self):
        d = core.DEFAULT_SETTINGS
        self.every.setValue(d["check_every_seconds"])
        self.timeout.setValue(d["timeout_seconds"])
        self.trust.setValue(d["trust_cache_seconds"])
        self.unlock.setValue(d["unlock_minutes"])
        self.speed.setValue(d["speed_limit_mb_s"])
        self.dl.setText(d["download_dir"])
        self.junk.setPlainText("\n".join(d["junk"]))
        self.terminal.setText(shlex.join(d["terminal"]))
        self.fileman.setText(shlex.join(d["file_manager"]))
        color_label(self.error, "", None)

    def values(self) -> tuple[dict | None, str]:
        """(settings, "") or (None, what to fix)."""
        out = {}
        for key, field in (("terminal", self.terminal), ("file_manager", self.fileman)):
            name = "Terminal" if key == "terminal" else "File manager"
            try:
                cmd = shlex.split(field.text())
            except ValueError:
                return None, f"{name}: a quote is not closed."
            if not cmd:
                return None, f"{name}: type a command, e.g. {shlex.join(core.DEFAULT_SETTINGS[key])}"
            if shutil.which(cmd[0]) is None:
                return None, f"{name}: “{cmd[0]}” is not installed. Type a program you have."
            out[key] = cmd
        junk = [x.strip() for x in self.junk.toPlainText().splitlines() if x.strip()]
        bad = [x for x in junk if "/" in x or len(x) > 100]
        if bad:
            return None, f"Skip these names: “{bad[0]}” is a path; use a name like __pycache__."
        if len(junk) > 100:
            return None, "Skip these names: 100 names at most."
        dl = self.dl.text().strip()
        # The default is always fine (the picker falls back to a folder that exists).
        if not dl or (dl != core.DEFAULT_SETTINGS["download_dir"]
                      and not os.path.isdir(os.path.expanduser(dl))):
            return None, "Download folder: that folder does not exist on this PC."
        out.update(check_every_seconds=self.every.value(), timeout_seconds=self.timeout.value(),
                   trust_cache_seconds=self.trust.value(), unlock_minutes=self.unlock.value(),
                   speed_limit_mb_s=round(self.speed.value(), 1), download_dir=dl, junk=junk)
        return out, ""

    def _save(self):
        new, err = self.values()
        if new is None:
            color_label(self.error, "✕ " + err, RED)
            return
        self.result_settings = core.save_settings(new)
        self.accept()
