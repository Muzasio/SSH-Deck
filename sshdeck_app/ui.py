"""SSH Deck window: device list, one tab per device (send, browse, download)."""
from __future__ import annotations

import html
import ipaddress
import os
import posixpath
import subprocess
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

from . import core, transfer as tr

# Virtual / VPN interfaces skipped when guessing the home network
SKIP_IFACES = ("lo", "docker", "virbr", "veth", "br-", "tun", "tap", "wg",
               "tailscale", "vmnet", "vboxnet", "lxc", "zt")

# ---------------------------------------------------------------- theme

QSS = """
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 9px 16px; margin: 0 2px; border: none;
               border-bottom: 2px solid transparent; background: transparent; }
QTabBar::tab:selected { border-bottom: 2px solid palette(highlight); font-weight: 600; }
QTabBar::tab:hover:!selected { border-bottom: 2px solid @line; }
QFrame#card { background: palette(base); border: 1px solid @line; border-radius: 12px; }
QFrame#card[active="true"] { border: 1px solid palette(highlight); background: @tint; }
QFrame#drop { background: palette(base); border: 2px dashed @line; border-radius: 16px; }
QFrame#drop[active="true"] { border: 2px dashed palette(highlight); background: @tint; }
QPushButton, QToolButton#more { padding: 6px 14px; border-radius: 8px;
    border: 1px solid @line; background: palette(button); }
QPushButton:hover, QToolButton#more:hover { border-color: palette(highlight); }
QPushButton:disabled { color: @muted; }
QPushButton[primary="true"] { background: palette(highlight); color: palette(highlighted-text);
    border: 1px solid palette(highlight); font-weight: 600; }
QPushButton[primary="true"]:hover { background: @hover; }
QPushButton#seg { border-radius: 0; padding: 6px 18px; }
QPushButton#seg[pos="left"] { border-top-left-radius: 8px; border-bottom-left-radius: 8px; }
QPushButton#seg[pos="right"] { border-top-right-radius: 8px; border-bottom-right-radius: 8px;
    border-left: none; }
QPushButton#seg:checked { background: palette(highlight); color: palette(highlighted-text);
    border-color: palette(highlight); font-weight: 600; }
QToolButton#more::menu-indicator { image: none; width: 0; }
QToolButton#x { border: none; padding: 2px 6px; border-radius: 6px; }
QToolButton#x:hover { background: @tint; }
QLineEdit { padding: 6px 9px; border-radius: 8px; border: 1px solid @line;
            background: palette(base); }
QLineEdit:focus { border-color: palette(highlight); }
QTreeWidget { border: 1px solid @line; border-radius: 10px; background: palette(base);
              padding: 4px; }
QHeaderView::section { background: transparent; border: none; padding: 4px 6px;
                       color: @muted; font-weight: 600; }
QProgressBar { border: none; border-radius: 3px; background: @line;
               min-height: 6px; max-height: 6px; }
QProgressBar::chunk { border-radius: 3px; background: palette(highlight); }
QProgressBar[state="ok"]::chunk { background: #22c55e; }
QProgressBar[state="bad"]::chunk { background: #ef4444; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item, QListWidget::item:selected, QListWidget::item:hover {
    background: transparent; border: none; }
QScrollArea, QScrollArea > QWidget > QWidget { border: none; background: transparent; }
QLabel#title { font-size: 17pt; font-weight: 700; }
QLabel#h2 { font-size: 12pt; font-weight: 600; }
QLabel#muted { color: @muted; }
QLabel#warn { color: #f59e0b; }
QLabel#pill { border-radius: 9px; padding: 1px 10px; font-weight: 600; font-size: 9pt; }
"""

DOT = {True: "#22c55e", False: "#ef4444", None: "#9ca3af"}
PILL = {True: ("Online", "#22c55e"), False: ("Offline", "#ef4444"), None: ("Checking…", "#9ca3af")}
GREEN, RED = "#22c55e", "#ef4444"


def make_qss(app: QtWidgets.QApplication) -> str:
    pal = app.palette()
    t = pal.color(QtGui.QPalette.Text)
    hl = pal.color(QtGui.QPalette.Highlight)
    rgba = lambda c, a: f"rgba({c.red()},{c.green()},{c.blue()},{a})"
    return (QSS.replace("@muted", rgba(t, 150)).replace("@line", rgba(t, 40))
            .replace("@tint", rgba(hl, 30)).replace("@hover", hl.lighter(112).name()))


def dot_icon(state) -> QtGui.QIcon:
    pm = QtGui.QPixmap(16, 16)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor(DOT[state]))
    p.drawEllipse(3, 3, 10, 10)
    p.end()
    return QtGui.QIcon(pm)


def upload_pixmap(color: QtGui.QColor, size: int = 44) -> QtGui.QPixmap:
    """Arrow up into a tray, drawn with the theme's accent colour."""
    dpr = 2
    pm = QtGui.QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(QtGui.QPen(color, size / 11, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap,
                        QtCore.Qt.RoundJoin))
    u = size / 12
    P = QtCore.QPointF
    p.drawLine(P(6 * u, 1.5 * u), P(6 * u, 7.5 * u))
    p.drawPolyline([P(3.2 * u, 4.2 * u), P(6 * u, 1.5 * u), P(8.8 * u, 4.2 * u)])
    p.drawPolyline([P(1.5 * u, 7.5 * u), P(1.5 * u, 10.5 * u), P(10.5 * u, 10.5 * u),
                    P(10.5 * u, 7.5 * u)])
    p.end()
    return pm


def label(text: str = "", obj: str | None = None) -> QtWidgets.QLabel:
    w = QtWidgets.QLabel(text)
    if obj:
        w.setObjectName(obj)
    return w


def button(text: str, primary: bool = False, tip: str = "") -> QtWidgets.QPushButton:
    b = QtWidgets.QPushButton(text)
    b.setProperty("primary", primary)
    b.setCursor(QtCore.Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def card(obj: str = "card") -> QtWidgets.QFrame:
    f = QtWidgets.QFrame()
    f.setObjectName(obj)
    return f


def more_button(tip: str = "More") -> QtWidgets.QToolButton:
    more = QtWidgets.QToolButton()
    more.setObjectName("more")
    more.setText("⋯")
    more.setFixedWidth(40)
    more.setToolTip(tip)
    more.setCursor(QtCore.Qt.PointingHandCursor)
    more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
    more.setMenu(QtWidgets.QMenu(more))
    return more


def repolish(w: QtWidgets.QWidget):
    w.style().unpolish(w)
    w.style().polish(w)


def set_pill(pill: QtWidgets.QLabel, state):
    text, color = PILL[state]
    c = QtGui.QColor(color)
    pill.setText(text)
    pill.setStyleSheet(f"color: {color}; background: rgba({c.red()},{c.green()},{c.blue()},38);")


def color_label(w: QtWidgets.QLabel, text: str, color: str | None):
    w.setText(text)
    w.setStyleSheet(f"color: {color};" if color else "")


def local_paths(mime: QtCore.QMimeData) -> list[str]:
    if not mime.hasUrls():
        return []
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


def fp_text(fps: list[str]) -> str:
    return "\n".join(f"  {f}" for f in fps) or "  (none)"


CHECK_HELP = (
    "To check, run this on the device itself and compare:\n"
    "  Linux:    ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub\n"
    "  Windows:  ssh-keygen -lf C:\\ProgramData\\ssh\\ssh_host_ed25519_key.pub"
)


def guess_prefix(hosts: list[dict]) -> str:
    """'192.168.1.' from this PC's home network, else from the last saved device."""
    QNI = QtNetwork.QNetworkInterface
    for iface in QNI.allInterfaces():
        f = iface.flags()
        if (not f & QNI.InterfaceFlag.IsUp or not f & QNI.InterfaceFlag.IsRunning
                or f & QNI.InterfaceFlag.IsLoopBack or iface.name().startswith(SKIP_IFACES)):
            continue
        for entry in iface.addressEntries():
            if entry.ip().protocol() != QtNetwork.QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            ip = ipaddress.ip_address(entry.ip().toString())
            if ip.is_private and not ip.is_link_local:
                return str(ip).rsplit(".", 1)[0] + "."
    for d in reversed(hosts):
        try:
            if ipaddress.ip_address(d["host"]).version == 4:
                return d["host"].rsplit(".", 1)[0] + "."
        except ValueError:
            pass
    return ""


# ---------------------------------------------------------------- background tasks

class _Bridge(QtCore.QObject):
    """Lives in the GUI thread; workers emit on it, slots run in the GUI thread."""
    done = QtCore.Signal(int, object)


class Task(QtCore.QRunnable):
    def __init__(self, tid: int, fn, bridge: _Bridge):
        super().__init__()
        self.tid, self.fn, self.bridge = tid, fn, bridge

    def run(self):
        try:
            result = self.fn()
        except Exception as e:  # shown to the user, never crashes the GUI
            result = e
        self.bridge.done.emit(self.tid, result)


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
                 junk: list[str], free: int | None = None):
        super().__init__(parent)
        self.d, self.dest, self.scan, self.clash, self.junk = d, dest, scan, clash, junk
        self.free = free
        self.choice: str | None = None
        self.plan: dict | None = None
        n = len(scan.items)
        self.setWindowTitle("Send files")
        title = label(f"Send {n} item{'s' * (n != 1)} to {d['name']}?", "h2")
        self.summary = label("", "muted")
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        full = tr.plan_upload(scan.items, dest, False, True, junk)["stats"]
        self.skip = QtWidgets.QCheckBox()
        self.skip.setChecked(True)
        junk_n = tr.plan_upload(scan.items, dest, True, True, junk)["stats"]["junk"]
        self.skip.setText(f"Skip junk: {junk_n} item(s) like {', '.join(junk[:3])}…")
        self.skip.setVisible(junk_n > 0)
        self.follow = QtWidgets.QCheckBox(
            f"Also send the files that links point to ({scan.counts['symlinks']} link(s)). "
            "Links to folders are never followed.")
        self.follow.setVisible(scan.counts["symlinks"] > 0 and full["links_sent"] > 0)
        self.resume = QtWidgets.QCheckBox(
            "Resume: skip files already complete on the device (same size and same time, "
            "to the minute). Half-sent or changed files are sent again.")
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

    def recompute(self, *_):
        plan = tr.plan_upload(self.scan.items, self.dest, self.skip.isChecked(),
                              self.follow.isChecked(), self.junk)
        st = plan["stats"]
        big = st["files"] >= tr.BIG_FILES or st["bytes"] >= tr.BIG_BYTES
        lines = [f"{st['files']} files  ·  {tr.human(st['bytes'])}",
                 f"To: {self.d['user']}@{self.d['host']}:{self.dest}"]
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
        self.plan = tr.plan_upload(self.items_for(choice), self.dest, self.skip.isChecked(),
                                   self.follow.isChecked(), self.junk)
        self.accept()


# ---------------------------------------------------------------- device list

class DeviceRow(QtWidgets.QFrame):
    def __init__(self, win: "MainWindow", d: dict):
        super().__init__()
        self.setObjectName("card")
        self.setAcceptDrops(True)
        self.setToolTip("Double-click to open · drop files here to send them")
        self.win, self.d = win, d
        self.dot = QtWidgets.QLabel()
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(label(d["name"], "h2"))
        text.addWidget(label(f"{d['user']}@{d['host']}:{d['port']}", "muted"))
        open_b = button("Open", True, "Open in a tab: send, browse, download")
        open_b.clicked.connect(lambda: win.open_tab(self.d))
        term_b = button("Terminal", tip="Open a terminal on this device")
        term_b.clicked.connect(lambda: win.connect_device(self.d))
        more = more_button()
        menu = more.menu()
        for text_, fn in (("Browse in file manager", win.open_files),
                          ("Set up key…", win.setup_key),
                          ("Remove key from device…", win.remove_key),
                          ("Edit…", win.edit_device), ("Remove…", win.remove_device)):
            menu.addAction(text_, lambda f=fn: f(self.d))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 12, 10)
        lay.setSpacing(10)
        lay.addWidget(self.dot)
        lay.addLayout(text, 1)
        lay.addWidget(open_b)
        lay.addWidget(term_b)
        lay.addWidget(more)
        self.sock: QtNetwork.QTcpSocket | None = None
        self.set_online(win.state.get(d["name"]))

    def set_online(self, state):
        self.dot.setPixmap(dot_icon(state).pixmap(16, 16))
        self.dot.setToolTip(PILL[state][0])
        self.win.on_status(self.d["name"], state)

    def mouseDoubleClickEvent(self, e):
        self.win.open_tab(self.d)

    def _active(self, on: bool):
        self.setProperty("active", on)
        repolish(self)

    def dragEnterEvent(self, e):
        if local_paths(e.mimeData()):
            e.acceptProposedAction()
            self._active(True)

    def dragLeaveEvent(self, e):
        self._active(False)

    def dropEvent(self, e):
        self._active(False)
        paths = local_paths(e.mimeData())
        if paths:
            e.acceptProposedAction()
            tab = self.win.open_tab(self.d)
            tab.show_page(0)
            tab.send(paths)

    def ping(self, timeout_ms: int):
        if self.sock is not None:
            return
        sock = QtNetwork.QTcpSocket(self)
        self.sock = sock

        def finish(state: bool):
            if self.sock is sock:
                self.sock = None
                self.set_online(state)
                sock.abort()
                sock.deleteLater()

        sock.connected.connect(lambda: finish(True))
        sock.errorOccurred.connect(lambda _e: finish(False))
        # Context = this row, so the timer dies with the row on refresh.
        QtCore.QTimer.singleShot(timeout_ms, self, lambda: finish(False))
        sock.connectToHost(self.d["host"], self.d["port"])


# ---------------------------------------------------------------- transfers

class TransferRow(QtWidgets.QFrame):
    cancel_clicked = QtCore.Signal(object)

    def __init__(self, name: str, detail: str, icon: QtGui.QIcon, total_files: int = 0):
        super().__init__()
        self.setObjectName("card")
        self.job = None
        self.finished = self.cancelled = False
        self.total_files = total_files
        self.t0 = 0.0
        ic = QtWidgets.QLabel()
        ic.setPixmap(icon.pixmap(28, 28))
        nm = label(name)
        nm.setStyleSheet("font-weight: 600;")
        self.detail = label(detail, "muted")
        self.status = label("Waiting", "muted")
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.x = QtWidgets.QToolButton()
        self.x.setObjectName("x")
        self.x.setText("✕")
        self.x.setToolTip("Cancel")
        self.x.setCursor(QtCore.Qt.PointingHandCursor)
        self.x.clicked.connect(lambda: self.cancel_clicked.emit(self))
        top = QtWidgets.QHBoxLayout()
        top.addWidget(nm, 1)
        top.addWidget(self.status)
        mid = QtWidgets.QVBoxLayout()
        mid.setSpacing(4)
        mid.addLayout(top)
        mid.addWidget(self.bar)
        mid.addWidget(self.detail)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.addWidget(ic)
        lay.addLayout(mid, 1)
        lay.addWidget(self.x, 0, QtCore.Qt.AlignTop)

    def start(self, job):
        self.job = job
        self.t0 = time.monotonic()
        self.status.setText("Starting…")

    def on_progress(self, frac: float, name: str, rate: str, files_done: int):
        parts = []
        if frac >= 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(frac * 1000))
            parts.append(f"{int(frac * 100)}%")
        else:
            self.bar.setRange(0, 0)  # unknown size: moving bar
        if self.total_files > 1:
            parts.append(f"{min(files_done, self.total_files)}/{self.total_files} files")
        elif frac < 0 and files_done:
            parts.append(f"{files_done} files")
        if rate:
            parts.append(rate)
        self.status.setText("  ·  ".join(parts) or "Working…")
        if name:
            self.status.setToolTip(name)

    def set_status(self, text: str):
        self.status.setText(text)

    def finish(self, ok: bool, msg: str, word: str = "Sent"):
        self.finished = True
        self.x.hide()
        self.bar.setRange(0, 1000)
        if ok:
            secs = time.monotonic() - self.t0
            self.bar.setValue(1000)
            self.bar.setProperty("state", "ok")
            took = f"  ·  {secs:.0f}s" if secs >= 1 else ""
            self.status.setText(f"{word} ✓{took}" + (f"  ·  {msg}" if msg else ""))
        else:
            self.bar.setProperty("state", "bad")
            self.status.setText("Cancelled" if self.cancelled else "Failed")
            self.status.setToolTip(msg)
            if not self.cancelled:
                self.detail.setText(msg)
                self.detail.setStyleSheet(f"color: {RED};")
                self.detail.setWordWrap(True)
        repolish(self.bar)


# ---------------------------------------------------------------- device tab

class BrowsePanel(QtWidgets.QWidget):
    """Files on the device: navigate, new folder, download, 'send here'."""

    def __init__(self, tab: "DeviceTab"):
        super().__init__()
        self.tab, self.win = tab, tab.win
        self.home: str | None = None
        self.cwd: str | None = None
        self.loaded = False
        up = button("↑", tip="Up one folder")
        up.setFixedWidth(40)
        up.clicked.connect(self.go_up)
        self.path = QtWidgets.QLineEdit()
        self.path.setPlaceholderText("Folder on the device")
        self.path.returnPressed.connect(lambda: self.go(self.path.text().strip()))
        refresh = button("Refresh")
        refresh.clicked.connect(lambda: self.go(self.cwd or "~"))
        newf = button("New folder")
        newf.clicked.connect(self.new_folder)
        here = button("Send here", tip="Use this folder as the 'Send to' folder")
        here.clicked.connect(self.send_here)
        self.dl = button("Download", True, "Download the selected items to this PC")
        self.dl.clicked.connect(self.download)
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(up)
        bar.addWidget(self.path, 1)
        bar.addWidget(refresh)
        bar.addWidget(newf)
        bar.addWidget(here)
        bar.addWidget(self.dl)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size", "Modified"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.msg = label("", "muted")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addLayout(bar)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.msg)
        prov = QtWidgets.QFileIconProvider()
        self.icon_dir = prov.icon(QtGui.QAbstractFileIconProvider.IconType.Folder)
        self.icon_file = prov.icon(QtGui.QAbstractFileIconProvider.IconType.File)

    def first_load(self):
        if not self.loaded:
            self.loaded = True
            self.go(self.tab.dest.text().strip() or "~")

    def _abs(self, path: str) -> str:
        if self.home and (path == "~" or path.startswith("~/")):
            return posixpath.normpath(posixpath.join(self.home, path[2:]))
        return path

    def go(self, path: str):
        d, t = self.tab.d, self.win.settings["timeout_seconds"]
        color_label(self.msg, "Loading…", None)

        def work():
            home = self.home or tr.remote_home(d, t)
            target = path
            if home and (target == "~" or target.startswith("~/")):
                target = posixpath.normpath(posixpath.join(home, target[2:]))
            return home, target, tr.list_remote(d, target, t)

        self.win.gate(d, self.tab.guard(lambda: self.win.run(work, self.tab.guard(self._listed))),
                      self.tab.guard(lambda: color_label(self.msg, "", None)))

    def _listed(self, res):
        if isinstance(res, Exception):
            color_label(self.msg, f"✕ {res}", RED)
            return
        home, target, (ok, val) = res
        self.home = home or self.home
        if not ok:
            color_label(self.msg, f"✕ {val}", RED)
            return
        self.cwd = target
        self.path.setText(tr.display_path(target, self.home))
        self.tree.clear()
        for e in val:
            it = QtWidgets.QTreeWidgetItem([e["name"] + (" →" if e["link"] else ""),
                                            "" if e["dir"] else tr.human(e["size"]), e["date"]])
            it.setIcon(0, self.icon_dir if e["dir"] else self.icon_file)
            it.setData(0, QtCore.Qt.UserRole, e)
            it.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.tree.addTopLevelItem(it)
        self.tree.resizeColumnToContents(1)
        self.tree.resizeColumnToContents(2)
        n = len(val)
        color_label(self.msg, f"{n} item{'s' * (n != 1)}", None)

    def _open_item(self, item, _col):
        e = item.data(0, QtCore.Qt.UserRole)
        if e and (e["dir"] or e["link"]):
            self.go(e["path"])

    def go_up(self):
        if self.cwd:
            parent = posixpath.dirname(self.cwd.rstrip("/")) or "/"
            self.go(parent if self.cwd.startswith("/") else ".")

    def selected(self) -> list[dict]:
        return [i.data(0, QtCore.Qt.UserRole) for i in self.tree.selectedItems()]

    def send_here(self):
        if self.cwd:
            self.tab.dest.setText(tr.display_path(self.cwd, self.home))
            if self.tab.save_dest():
                self.tab.show_page(0)
                self.win.say(f"Send to: {self.tab.dest.text()}")

    def new_folder(self):
        if not self.cwd:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "New folder", "Folder name:")
        name = name.strip()
        if not ok or not name:
            return
        if not tr.valid_name(name) or any(c in name for c in "*?[]"):
            QtWidgets.QMessageBox.warning(self, "New folder", "That name is not allowed.")
            return
        d, t, path = self.tab.d, self.win.settings["timeout_seconds"], \
            posixpath.join(self.cwd, name)
        self.win.run(lambda: tr.make_remote_dir(d, path, t), self.tab.guard(
            lambda res: self._made(res)))

    def _made(self, res):
        ok, msg = res if isinstance(res, tuple) else (False, str(res))
        if ok:
            self.go(self.cwd)
        else:
            color_label(self.msg, f"✕ {msg}", RED)

    def download(self):
        entries = [e for e in self.selected() if e]
        if not entries:
            color_label(self.msg, "Select files or folders first.", None)
            return
        start = os.path.expanduser(self.win.settings["download_dir"])
        target = QtWidgets.QFileDialog.getExistingDirectory(self, "Download to", start)
        if not target:
            return
        clash = [e["name"] for e in entries if os.path.lexists(os.path.join(target, e["name"]))]
        if clash:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Download")
            box.setIcon(QtWidgets.QMessageBox.Warning)
            box.setText("Already in that folder on this PC:")
            box.setInformativeText("\n".join(_more(clash, len(clash), 6)))
            rep = box.addButton("Replace", QtWidgets.QMessageBox.AcceptRole)
            skip = box.addButton("Skip those", QtWidgets.QMessageBox.AcceptRole) \
                if len(clash) < len(entries) else None
            box.addButton(QtWidgets.QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() not in (rep, skip) or box.clickedButton() is None:
                return
            if box.clickedButton() is skip:
                entries = [e for e in entries if e["name"] not in clash]
        self.tab.queue_download(entries, target)


class DeviceTab(QtWidgets.QWidget):
    def __init__(self, win: "MainWindow", d: dict):
        super().__init__()
        self.win, self.d = win, d
        self.alive = True
        self.queue: list[tuple[TransferRow, object]] = []
        self.current: tuple[TransferRow, object] | None = None
        self.setAcceptDrops(True)

        # header
        head = card()
        self.title = label(d["name"], "title")
        self.pill = label("", "pill")
        self.addr = label("", "muted")
        tline = QtWidgets.QHBoxLayout()
        tline.setSpacing(10)
        tline.addWidget(self.title)
        tline.addWidget(self.pill, 0, QtCore.Qt.AlignVCenter)
        tline.addStretch(1)
        tcol = QtWidgets.QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addLayout(tline)
        tcol.addWidget(self.addr)
        term = button("Terminal", True, "Open a terminal on this device (Ctrl+T)")
        term.clicked.connect(lambda: win.connect_device(self.d))
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(18, 14, 14, 14)
        hl.addLayout(tcol, 1)
        self.seg_send = button("Send")
        self.seg_browse = button("Device files")
        for i, (b, pos) in enumerate(((self.seg_send, "left"), (self.seg_browse, "right"))):
            b.setObjectName("seg")
            b.setProperty("pos", pos)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, i=i: self.show_page(i))
        seg = QtWidgets.QHBoxLayout()
        seg.setSpacing(0)
        seg.addWidget(self.seg_send)
        seg.addWidget(self.seg_browse)
        hl.addLayout(seg)
        hl.addSpacing(8)
        hl.addWidget(term)

        # send page
        send_page = QtWidgets.QWidget()
        dest_card = card()
        self.dest = QtWidgets.QLineEdit(d.get("dest", core.DEFAULT_DEST))
        self.dest.setPlaceholderText("~/Downloads")
        self.dest.setToolTip("Folder on the device. ~ means the user's home folder.")
        self.dest.editingFinished.connect(self.save_dest)
        check = button("Check", tip="Check that this folder exists on the device")
        check.clicked.connect(self.check_dest)
        self.dest_msg = label("", "muted")
        dl = QtWidgets.QHBoxLayout(dest_card)
        dl.setContentsMargins(18, 10, 14, 10)
        dl.addWidget(label("Send to"))
        dl.addWidget(self.dest, 1)
        dl.addWidget(check)
        dl.addWidget(self.dest_msg)
        self.drop = card("drop")
        self.drop.setMinimumHeight(150)
        pick_files = button("Choose files…", tip="Ctrl+O")
        pick_files.clicked.connect(self.choose_files)
        pick_dir = button("Choose folder…")
        pick_dir.clicked.connect(self.choose_folder)
        picks = QtWidgets.QHBoxLayout()
        picks.addStretch(1)
        picks.addWidget(pick_files)
        picks.addWidget(pick_dir)
        picks.addStretch(1)
        arrow = QtWidgets.QLabel()
        arrow.setPixmap(upload_pixmap(self.palette().color(QtGui.QPalette.Highlight)))
        hint = label("Drop files or folders here", "h2")
        sub = label("Sent over SSH (sftp) to the folder above. Nothing is sent until you "
                    "confirm.", "muted")
        for w in (arrow, hint, sub):
            w.setAlignment(QtCore.Qt.AlignCenter)
        zl = QtWidgets.QVBoxLayout(self.drop)
        zl.addStretch(1)
        zl.addWidget(arrow)
        zl.addWidget(hint)
        zl.addWidget(sub)
        zl.addSpacing(6)
        zl.addLayout(picks)
        zl.addStretch(1)
        sl = QtWidgets.QVBoxLayout(send_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(12)
        sl.addWidget(dest_card)
        sl.addWidget(self.drop, 1)

        # browse page
        self.browse = BrowsePanel(self)
        self.pages = QtWidgets.QStackedWidget()
        self.pages.addWidget(send_page)
        self.pages.addWidget(self.browse)

        # transfers
        clear = button("Clear finished")
        clear.clicked.connect(self.clear_finished)
        th = QtWidgets.QHBoxLayout()
        th.addWidget(label("Transfers", "h2"))
        th.addStretch(1)
        th.addWidget(clear)
        self.empty = label("Nothing sent or downloaded yet.", "muted")
        holder = QtWidgets.QWidget()
        self.tl = QtWidgets.QVBoxLayout(holder)
        self.tl.setContentsMargins(0, 0, 0, 0)
        self.tl.setSpacing(8)
        self.tl.addWidget(self.empty)
        self.tl.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(head)
        lay.addWidget(self.pages, 3)
        lay.addLayout(th)
        lay.addWidget(scroll, 2)
        self.set_device(d)
        self.show_page(0)

    # ---- helpers
    def guard(self, fn):
        """Wrap a callback so it does nothing once this tab is closed."""
        def wrapped(*a, **k):
            if self.alive:
                return fn(*a, **k)
            return None
        return wrapped

    def show_page(self, i: int):
        self.pages.setCurrentIndex(i)
        self.seg_send.setChecked(i == 0)
        self.seg_browse.setChecked(i == 1)
        if i == 1:
            self.browse.first_load()

    def set_device(self, d: dict):
        self.d = d
        self.title.setText(d["name"])
        self.addr.setText(f"{d['user']}@{d['host']}:{d['port']}")
        self.dest.setText(d.get("dest", core.DEFAULT_DEST))

    def set_online(self, state):
        set_pill(self.pill, state)

    def running(self) -> bool:
        return bool(self.current or self.queue)

    # ---- destination
    def save_dest(self) -> str | None:
        dest = self.dest.text().strip()
        err = core.check_dest(dest)
        if err:
            color_label(self.dest_msg, "✕ " + err.split(": ", 1)[-1], RED)
            return None
        color_label(self.dest_msg, "", None)
        self.win.set_dest(self.d, dest)
        return dest

    def check_dest(self):
        dest = self.save_dest()
        if dest is None:
            return
        d, t = self.d, self.win.settings["timeout_seconds"]
        color_label(self.dest_msg, "Checking…", None)
        self.win.gate(d, self.guard(lambda: self.win.run(
            lambda: tr.list_remote(d, dest, t), self.guard(lambda r: self._checked(dest, r)))),
            self.guard(lambda: color_label(self.dest_msg, "", None)))

    def _checked(self, dest: str, res):
        ok, val = res if isinstance(res, tuple) else (False, str(res))
        if ok:
            color_label(self.dest_msg, f"✓ Found · {len(val)} items", GREEN)
            return
        color_label(self.dest_msg, "✕ " + val, RED)
        if "not found" in val.lower():
            ans = QtWidgets.QMessageBox.question(
                self, "Create folder?", f"“{dest}” does not exist on {self.d['name']}.\n"
                "Create it now? (only the last folder is created)")
            if ans == QtWidgets.QMessageBox.Yes:
                d, t = self.d, self.win.settings["timeout_seconds"]
                self.win.run(lambda: tr.make_remote_dir(d, dest, t),
                              self.guard(lambda r: self._created(dest, r)))

    def _created(self, dest: str, res):
        ok, msg = res if isinstance(res, tuple) else (False, str(res))
        color_label(self.dest_msg, ("✓ Created" if ok else "✕ " + msg), GREEN if ok else RED)

    # ---- drag and drop
    def _active(self, on: bool):
        self.drop.setProperty("active", on)
        repolish(self.drop)

    def dragEnterEvent(self, e):
        if local_paths(e.mimeData()):
            e.acceptProposedAction()
            self._active(True)
            self.show_page(0)
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if local_paths(e.mimeData()):
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._active(False)

    def dropEvent(self, e):
        self._active(False)
        paths = local_paths(e.mimeData())
        if paths:
            e.acceptProposedAction()
            self.send(paths)

    def choose_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Choose files to send")
        if paths:
            self.send(paths)

    def choose_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose a folder to send")
        if path:
            self.send([path])

    # ---- sending: scan -> trust/unlock -> list remote -> confirm -> queue
    def send(self, paths: list[str]):
        dest = self.save_dest()
        if dest is None:
            QtWidgets.QMessageBox.warning(self, "Send to", self.dest_msg.text())
            return
        d = self.d
        self.win.say("Looking at the files…")
        self.win.run(lambda: tr.scan_local(paths), self.guard(
            lambda scan: self._scanned(d, dest, scan)))

    def _scanned(self, d: dict, dest: str, scan):
        if isinstance(scan, Exception):
            QtWidgets.QMessageBox.warning(self, "Send", f"Could not read the files:\n{scan}")
            return
        if not scan.items:
            self.win.say("Nothing to send.")
            if scan.counts["special"] or scan.counts["badname"]:
                QtWidgets.QMessageBox.warning(self, "Send", "Nothing can be sent: only special "
                                              "files or unsafe names were dropped.")
            return
        t = self.win.settings["timeout_seconds"]
        self.win.gate(d, self.guard(lambda: self.win.run(
            lambda: (tr.list_remote(d, dest, t), tr.remote_free(d, dest, t)),
            self.guard(lambda res: self._listed(d, dest, scan, res)))))

    def _listed(self, d: dict, dest: str, scan: tr.Scan, res):
        if isinstance(res, Exception):
            res, free = (False, str(res)), None
        else:
            res, free = res
        ok, val = res
        if not ok:
            self.win.say("Ready")
            QtWidgets.QMessageBox.warning(self, "Send", f"{dest}\n\n{val}")
            return
        names = {e["name"] for e in val}
        clash = [i["name"] for i in scan.items if i["name"] in names]
        dlg = SendDialog(self, d, dest, scan, clash, self.win.settings["junk"], free)
        if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.plan:
            self.win.say("Cancelled.")
            return
        plan, items = dlg.plan, dlg.items_for(dlg.choice)
        if not plan["files"] and not plan["dirs"]:
            self.win.say("Nothing to send.")
            return
        if dlg.resume.isVisible() and dlg.resume.isChecked():
            t = self.win.settings["timeout_seconds"]
            self.win.say("Checking what is already on the device…")
            dirs = {posixpath.dirname(f[1]) or "." for f in plan["files"]}
            self.win.run(lambda: (tr.apply_resume(plan, tr.remote_listing(d, dirs, t)),
                                  tr.remote_free(d, dest, t)),
                         self.guard(lambda r: self._resumed(dest, items, r)))
            return
        self._queue_upload(dest, items, plan)

    def _resumed(self, dest: str, items: list[dict], res):
        if isinstance(res, Exception):
            QtWidgets.QMessageBox.warning(self, "Send", f"Could not check the device:\n{res}")
            return
        plan, free = res
        if free is not None and plan["stats"]["bytes"] > free:
            QtWidgets.QMessageBox.warning(
                self, "Not enough space", f"After skipping {plan['resumed']} complete file(s), "
                f"{tr.human(plan['stats']['bytes'])} are still needed, but only "
                f"{tr.human(free)} is free on {self.d['name']}.\nNothing was sent.")
            self.win.say("Not enough space on the device.")
            return
        if not plan["files"]:
            self.win.say(f"Everything is already on {self.d['name']} ({plan['resumed']} files).")
            QtWidgets.QMessageBox.information(
                self, "Send", f"All {plan['resumed']} file(s) are already complete on "
                f"{self.d['name']}. Nothing to send.")
            return
        self._queue_upload(dest, items, plan)

    def _queue_upload(self, dest: str, items: list[dict], plan: dict):
        st = plan["stats"]
        first = items[0]
        name = first["name"] if len(items) == 1 else f"{len(items)} items"
        icon = QtWidgets.QFileIconProvider().icon(QtCore.QFileInfo(first["path"]))
        detail = f"{st['files']} files · {tr.human(st['bytes'])}  →  {dest}"
        if plan.get("resumed"):
            detail += f"  ·  {plan['resumed']} already there, skipped"
        row = TransferRow(name, detail, icon, st["files"])
        self._enqueue(row, ("up", plan))

    def queue_download(self, entries: list[dict], target: str):
        if not entries:
            return
        try:
            plan = tr.plan_download(entries, target)
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Download", str(e))
            return
        if plan["known"]:
            need, free = sum(plan["sizes"]), tr.local_free(target)
            if need > free:
                QtWidgets.QMessageBox.warning(
                    self, "Not enough space", f"Needs {tr.human(need)}, but only "
                    f"{tr.human(free)} is free in {target}.\nNothing was downloaded.")
                return
        name = entries[0]["name"] if len(entries) == 1 else f"{len(entries)} items"
        prov = QtWidgets.QFileIconProvider()
        icon = prov.icon(QtGui.QAbstractFileIconProvider.IconType.Folder if entries[0]["dir"]
                         else QtGui.QAbstractFileIconProvider.IconType.File)
        size = "" if not plan["known"] else f" · {tr.human(sum(plan['sizes']))}"
        row = TransferRow(name, f"⬇ to {target}{size}", icon,
                          len(entries) if plan["known"] else 0)
        self._enqueue(row, ("down", plan))

    def _enqueue(self, row: TransferRow, job_spec):
        row.cancel_clicked.connect(self.cancel_row)
        self.tl.insertWidget(self.tl.count() - 1, row)
        self.empty.hide()
        self.queue.append((row, job_spec))
        self._next()

    def _next(self):
        if self.current or not self.queue:
            if not self.running():
                self.win.say("Ready")
            return
        row, (kind, plan) = self.queue.pop(0)
        if kind == "up":
            sizes = [f[2] for f in plan["files"]]
        else:
            sizes = plan["sizes"] if plan["known"] else [0] * len(plan["sizes"])
        job = tr.SftpJob(self.d, plan["lines"], sizes, self)
        job.progress.connect(row.on_progress)
        job.done.connect(lambda ok, msg, r=row, k=kind, p=plan: self._job_done(r, k, p, ok, msg))
        self.current = (row, job)
        row.start(job)
        self.win.say(("Sending to " if kind == "up" else "Downloading from ") + self.d["name"])
        job.start()

    def _job_done(self, row: TransferRow, kind: str, plan: dict, ok: bool, msg: str):
        if not self.alive:
            return
        if kind == "up" and ok and plan["files"]:
            row.set_status("Checking sizes…")
            d, t = self.d, self.win.settings["timeout_seconds"]
            self.win.run(lambda: tr.verify_upload(d, plan, t),
                         self.guard(lambda res: self._verified(row, res)))
            return
        self._finished(row, ok, msg, "Sent" if kind == "up" else "Downloaded")

    def _verified(self, row: TransferRow, res):
        good, note = res if isinstance(res, tuple) else (None, str(res))
        if good is False:
            self._finished(row, False, f"Sent, but {note}.", "Sent")
        else:
            self._finished(row, True, "sizes match" if good else "", "Sent")

    def _finished(self, row: TransferRow, ok: bool, msg: str, word: str):
        if self.current and self.current[0] is row:
            self.current = None
        row.finish(ok, msg, word)
        if not row.cancelled:
            self.win.notify(f"{word} {'✓' if ok else 'failed'} · {self.d['name']}",
                            msg if not ok else row.detail.text())
        self._next()

    def cancel_row(self, row: TransferRow):
        row.cancelled = True
        if self.current and self.current[0] is row:
            self.current[1].cancel()  # emits done -> _job_done
            return
        self.queue = [q for q in self.queue if q[0] is not row]
        row.finish(False, "Cancelled")

    def cancel_all(self):
        for row, _ in self.queue:
            row.cancelled = True
            row.finish(False, "Cancelled")
        self.queue = []
        if self.current:
            self.current[0].cancelled = True
            self.current[1].cancel()

    def clear_finished(self):
        for i in reversed(range(self.tl.count())):
            w = self.tl.itemAt(i).widget()
            if isinstance(w, TransferRow) and w.finished:
                self.tl.removeWidget(w)
                w.deleteLater()
        if not any(isinstance(self.tl.itemAt(i).widget(), TransferRow)
                   and not self.tl.itemAt(i).widget().finished
                   for i in range(self.tl.count())):
            self.empty.setVisible(not any(isinstance(self.tl.itemAt(i).widget(), TransferRow)
                                          for i in range(self.tl.count())))


# ---------------------------------------------------------------- main window

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = core.load_settings()
        self.hosts = core.load_hosts()
        core.save_hosts(self.hosts, self.settings["timeout_seconds"])
        self.pool = QtCore.QThreadPool.globalInstance()
        self.bridge = _Bridge(self)
        self.bridge.done.connect(self._task_done)
        self.pending: dict[int, tuple[Task, object]] = {}
        self.next_tid = 0
        self.waiting: dict[str, list] = {}       # device -> callbacks waiting for the gate
        self.trusted_until: dict[str, float] = {}
        self.keyjobs: set[str] = set()
        self.state: dict[str, bool | None] = {}
        self.dev_tabs: dict[str, DeviceTab] = {}
        self.children_procs: list[subprocess.Popen] = []  # started apps, reaped later

        self.setWindowTitle("SSH Deck")
        self.resize(940, 760)
        page = QtWidgets.QWidget()
        add = button("+  Add device", True, "Ctrl+N")
        add.clicked.connect(self.add_device)
        menu_b = more_button("Key and settings")
        m = menu_b.menu()
        m.addAction("Protect key with a passphrase…", self.protect_key)
        m.addAction("Unlock key now", self.unlock_key)
        m.addAction("Lock key now", self.lock_key)
        m.addSeparator()
        m.addAction("Refresh status (F5)", self.ping_all)
        m.addAction("Open settings file", self.open_settings)
        self.count = label("", "muted")
        hcol = QtWidgets.QVBoxLayout()
        hcol.setSpacing(1)
        hcol.addWidget(label("Devices", "title"))
        hcol.addWidget(self.count)
        top = QtWidgets.QHBoxLayout()
        top.addLayout(hcol, 1)
        top.addWidget(add, 0, QtCore.Qt.AlignBottom)
        top.addWidget(menu_b, 0, QtCore.Qt.AlignBottom)
        self.list = QtWidgets.QListWidget()
        self.list.setSpacing(4)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(QtCore.Qt.NoFocus)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.empty = label("No devices yet. Click “+ Add device”.", "muted")
        self.empty.setAlignment(QtCore.Qt.AlignCenter)
        pl = QtWidgets.QVBoxLayout(page)
        pl.setContentsMargins(16, 16, 16, 12)
        pl.setSpacing(12)
        pl.addLayout(top)
        pl.addWidget(self.list, 1)
        pl.addWidget(self.empty, 1)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.addTab(page, "Devices")
        for side in (QtWidgets.QTabBar.LeftSide, QtWidgets.QTabBar.RightSide):
            self.tabs.tabBar().setTabButton(0, side, None)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("Ready")

        for keys, fn in (("Ctrl+N", self.add_device), ("Ctrl+W", self.close_current),
                         ("Ctrl+Tab", lambda: self.cycle(1)),
                         ("Ctrl+Shift+Tab", lambda: self.cycle(-1)),
                         ("F5", self.ping_all), ("Ctrl+Q", self.close),
                         ("Ctrl+O", lambda: self._tab_do("choose_files")),
                         ("Ctrl+T", lambda: self._tab_do("terminal"))):
            QtGui.QShortcut(QtGui.QKeySequence(keys), self, fn)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.ping_all)
        self.timer.start(self.settings["check_every_seconds"] * 1000)
        self.refresh()

    # ---- helpers
    def say(self, msg: str):
        self.statusBar().showMessage(msg)

    def run(self, fn, callback):
        self.next_tid += 1
        task = Task(self.next_tid, fn, self.bridge)
        task.setAutoDelete(False)  # kept alive in self.pending until its result arrives
        self.pending[self.next_tid] = (task, callback)
        self.pool.start(task)

    def _task_done(self, tid: int, result):
        _task, callback = self.pending.pop(tid, (None, None))
        if callback:
            callback(result)

    def _start(self, cmd: list[str], env: dict | None = None):
        """Start a helper app (terminal, file manager, notification) without waiting;
        finished ones are collected so no zombie processes stay behind."""
        self.children_procs = [p for p in self.children_procs if p.poll() is None]
        self.children_procs.append(subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env))

    def notify(self, title: str, body: str):
        if self.isActiveWindow():
            return
        QtWidgets.QApplication.alert(self)
        if core.BIN.get("notify-send"):
            try:
                self._start([core.BIN["notify-send"], "-a", "SSH Deck", "-i", str(core.ICON),
                             "--", html.escape(title), html.escape(body)])
            except OSError:
                pass

    def save(self):
        core.save_hosts(self.hosts, self.settings["timeout_seconds"])

    def find(self, name: str) -> dict | None:
        return next((h for h in self.hosts if h["name"] == name), None)

    def set_dest(self, d: dict, dest: str):
        h = self.find(d["name"])
        if h is not None and h.get("dest") != dest:
            h["dest"] = dest
            d["dest"] = dest
            self.save()

    def refresh(self):
        self.list.clear()
        for d in self.hosts:
            item = QtWidgets.QListWidgetItem()
            row = DeviceRow(self, d)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        n = len(self.hosts)
        self.count.setText(f"{n} saved device{'s' * (n != 1)}" if n else "")
        self.list.setVisible(bool(n))
        self.empty.setVisible(not n)
        self.ping_all()

    def ping_all(self):
        if not self.isVisible() or self.isMinimized():
            return  # no status traffic while nobody looks
        for i in range(self.list.count()):
            row = self.list.itemWidget(self.list.item(i))
            if isinstance(row, DeviceRow):
                row.ping(self.settings["timeout_seconds"] * 1000)

    def showEvent(self, e):
        super().showEvent(e)
        QtCore.QTimer.singleShot(0, self, self.ping_all)

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QtCore.QEvent.WindowStateChange and not self.isMinimized():
            QtCore.QTimer.singleShot(0, self, self.ping_all)

    def on_status(self, name: str, state):
        self.state[name] = state
        tab = self.dev_tabs.get(name)
        if tab is not None:
            tab.set_online(state)
            self.tabs.setTabIcon(self.tabs.indexOf(tab), dot_icon(state))

    def spawn(self, cmd: list[str]):
        try:
            self._start(cmd, env=core.ssh_env())
        except OSError as e:
            QtWidgets.QMessageBox.critical(
                self, "Could not start", f"{cmd[0]}: {e}\n\nChange it in {core.SETTINGS_FILE}")

    def in_terminal(self, cmd: list[str], hold_always: bool = False) -> list[str]:
        # Keep the window open if SSH itself fails (exit 255), so the error is readable.
        cond = "true" if hold_always else '[ "$st" -eq 255 ]'
        script = (f'"$@"; st=$?; if {cond}; then '
                  'printf "\\nPress Enter to close."; read _; fi')
        return self.settings["terminal"] + ["sh", "-c", script, "sh"] + cmd

    def _tab_do(self, what: str):
        tab = self.tabs.currentWidget()
        if isinstance(tab, DeviceTab):
            if what == "terminal":
                self.connect_device(tab.d)
            else:
                tab.show_page(0)
                tab.choose_files()

    def cycle(self, step: int):
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    # ---- tabs
    def open_tab(self, d: dict) -> DeviceTab:
        tab = self.dev_tabs.get(d["name"])
        if tab is None:
            tab = DeviceTab(self, d)
            self.dev_tabs[d["name"]] = tab
            state = self.state.get(d["name"])
            self.tabs.addTab(tab, dot_icon(state), d["name"])
            tab.set_online(state)
        self.tabs.setCurrentWidget(tab)
        return tab

    def close_current(self):
        i = self.tabs.currentIndex()
        if i > 0:
            self.close_tab(i)

    def close_tab(self, index: int):
        tab = self.tabs.widget(index)
        if not isinstance(tab, DeviceTab):
            return
        if tab.running():
            ans = QtWidgets.QMessageBox.question(
                self, "Close tab", f"Transfers with {tab.d['name']} are still running.\n"
                "Stop them and close?")
            if ans != QtWidgets.QMessageBox.Yes:
                return
            tab.cancel_all()
        tab.alive = False  # late background results are ignored from now on
        self.tabs.removeTab(self.tabs.indexOf(tab))
        self.dev_tabs.pop(tab.d["name"], None)
        tab.deleteLater()

    def closeEvent(self, e):
        busy = [t for t in self.dev_tabs.values() if t.running()]
        if busy:
            ans = QtWidgets.QMessageBox.question(
                self, "Quit", "Transfers are still running. Stop them and quit?")
            if ans != QtWidgets.QMessageBox.Yes:
                e.ignore()
                return
            for t in busy:
                t.cancel_all()
        for t in self.dev_tabs.values():
            t.alive = False
        for p in self.children_procs:  # collect finished helpers; running apps stay open
            p.poll()
        core.AGENT.stop()
        e.accept()

    # ---- gate: host key trust (fingerprint box) + key unlock, shared by all actions
    def gate(self, d: dict, then, on_fail=None):
        """Run `then` once the device key is trusted and our key is usable.
        Calls that arrive while a check runs wait for it (nothing is dropped)."""
        name = d["name"]
        if name in self.waiting:
            self.waiting[name].append((then, on_fail))
            return
        self.waiting[name] = [(then, on_fail)]
        cached = time.monotonic() < self.trusted_until.get(name, 0.0)
        t, mins = self.settings["timeout_seconds"], self.settings["unlock_minutes"]
        if not cached:
            self.say(f"Checking the key of {name}…")
        self.run(lambda: (None if cached else core.check_hostkey(d, t),
                          core.ensure_unlocked(mins)),
                 lambda res: self._gate_result(d, res))

    def _gate_result(self, d: dict, res):
        name = d["name"]
        waiters = self.waiting.pop(name, [])
        ok = self._trust_ok(d, res)
        if ok and not res[1]:
            ok = False
            QtWidgets.QMessageBox.warning(self, name, "The SSH Deck key is locked (passphrase "
                                          "not entered). Nothing was done.")
        if ok:
            self.trusted_until[name] = time.monotonic() + self.settings["trust_cache_seconds"]
            self.say("Ready")
        for then, on_fail in waiters:
            if ok:
                then()
            elif on_fail:
                on_fail()

    def _trust_ok(self, d: dict, res) -> bool:
        if isinstance(res, Exception):
            QtWidgets.QMessageBox.warning(self, d["name"], str(res))
            return False
        trust = res[0]
        if trust is None or trust["state"] == "same":
            return True
        if trust["state"] == "error":
            self.say("Ready")
            QtWidgets.QMessageBox.warning(self, d["name"], trust["msg"])
            return False
        box = QtWidgets.QMessageBox(self)
        if trust["state"] == "new":
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setWindowTitle(f"New device: {d['name']}")
            box.setText(f"First connection to {d['host']}.\nIts key fingerprint is:")
            box.setInformativeText(fp_text(trust["new_fps"]) + "\n\n" + CHECK_HELP +
                                   "\n\nTrust this device?")
        else:
            box.setIcon(QtWidgets.QMessageBox.Warning)
            box.setWindowTitle(f"Key changed: {d['name']}")
            box.setText(f"⚠ The key of {d['host']} is DIFFERENT from last time.")
            box.setInformativeText(
                "Saved key:\n" + fp_text(trust["old_fps"]) +
                "\n\nKey now:\n" + fp_text(trust["new_fps"]) +
                "\n\nThis is normal if the device was reinstalled, its SSH was reset,\n"
                "or a different device now has this IP. Otherwise it can be an attack.\n\n"
                + CHECK_HELP + "\n\nTrust the new key?")
        box.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        box.setDefaultButton(QtWidgets.QMessageBox.No)
        box.button(QtWidgets.QMessageBox.Yes).setText("Trust")
        box.button(QtWidgets.QMessageBox.No).setText("Cancel")
        if box.exec() != QtWidgets.QMessageBox.Yes:
            self.say("Cancelled: key not trusted.")
            return False
        core.write_private(core.kh_file(d), trust["lines"])
        self.say(f"Key of {d['name']} trusted.")
        return True

    # ---- device actions
    def connect_device(self, d: dict):
        self.open_tab(d)

        def go():
            self.say(f"Opening terminal for {d['name']}")
            self.spawn(self.in_terminal([core.BIN["ssh"], "-F", str(core.SSH_CFG),
                                         core.alias(d)]))
        self.gate(d, go)

    def open_files(self, d: dict):
        if not core.has_include():
            ans = QtWidgets.QMessageBox.question(
                self, "File manager: one-time setup",
                "To open device files in the file manager, SSH Deck adds ONE line\n"
                "at the top of ~/.ssh/config:\n\n"
                f"  {core.include_line()}\n\n"
                "A backup of the file is made first. Your other entries are not changed.\n"
                "If you say No, files open in a terminal (sftp) instead.\n\n"
                "Tip: the “Device files” page in the device tab needs no setup.\n\nAdd the line?")
            if ans == QtWidgets.QMessageBox.Yes:
                try:
                    bak = core.add_include()
                    self.say(f"Line added. Backup: {bak}" if bak else "Line added.")
                except OSError as e:
                    QtWidgets.QMessageBox.critical(self, "Could not change ~/.ssh/config", str(e))
                    return

        def go():
            if core.has_include():
                self.spawn(self.settings["file_manager"] + [f"sftp://{core.alias(d)}/"])
            else:
                self.spawn(self.in_terminal([core.BIN["sftp"], "-F", str(core.SSH_CFG),
                                             core.alias(d)]))
            self.say(f"Opening files of {d['name']}")
        self.gate(d, go)

    def setup_key(self, d: dict, password: str | None = None):
        if password is None:
            password, ok = QtWidgets.QInputDialog.getText(
                self, f"Set up key: {d['name']}",
                f"Password of {d['user']} on {d['host']} (used once, not saved):",
                QtWidgets.QLineEdit.Password)
            if not ok or not password:
                return

        def go():
            self.keyjobs.add(d["name"])
            self.say(f"Installing key on {d['name']}…")
            t = self.settings["timeout_seconds"]
            self.run(lambda: core.install_key(d, password, t),
                     lambda res: self._key_done(d, res))
        self.gate(d, go)

    def _key_done(self, d: dict, res):
        self.keyjobs.discard(d["name"])
        if isinstance(res, Exception):
            res = (False, str(res))
        ok, msg = res
        self.say("Key ready." if ok else "Key setup failed.")
        (QtWidgets.QMessageBox.information if ok else QtWidgets.QMessageBox.warning)(
            self, d["name"], msg)

    def remove_key(self, d: dict, then=None):
        if then is None:
            ans = QtWidgets.QMessageBox.question(
                self, "Remove key from device",
                f"Remove SSH Deck's key from {d['user']}@{d['host']}?\n\n"
                "Only SSH Deck's line in ~/.ssh/authorized_keys is removed; other keys stay.\n"
                "After this, the device needs “Set up key” (password) again.")
            if ans != QtWidgets.QMessageBox.Yes:
                return

        def go():
            self.keyjobs.add(d["name"])
            t = self.settings["timeout_seconds"]
            self.run(lambda: core.remove_key_from_device(d, t),
                     lambda res: self._key_removed(d, res, then))
        self.gate(d, go, on_fail=(lambda: then(False, "not reachable")) if then else None)

    def _key_removed(self, d: dict, res, then):
        self.keyjobs.discard(d["name"])
        ok, msg = res if isinstance(res, tuple) else (False, str(res))
        if then:
            then(ok, msg)
            return
        (QtWidgets.QMessageBox.information if ok else QtWidgets.QMessageBox.warning)(
            self, d["name"], msg)

    def add_device(self):
        dlg = DeviceDialog(self, None, {h["name"] for h in self.hosts}, guess_prefix(self.hosts))
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        d = dlg.device()
        d["dest"] = core.DEFAULT_DEST
        self.hosts.append(d)
        self.save()
        self.refresh()
        if dlg.pw.text():
            self.setup_key(d, dlg.pw.text())

    def is_busy(self, d: dict) -> bool:
        tab = self.dev_tabs.get(d["name"])
        if d["name"] in self.waiting or d["name"] in self.keyjobs or \
                (tab is not None and tab.running()):
            self.say(f"{d['name']} is busy, please wait…")
            return True
        return False

    def edit_device(self, d: dict):
        if self.is_busy(d):
            return
        dlg = DeviceDialog(self, d, {h["name"] for h in self.hosts} - {d["name"]})
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        new = dlg.device()
        new["dest"] = d.get("dest", core.DEFAULT_DEST)
        old_kh = core.kh_file(d)
        if (new["host"], new["port"]) != (d["host"], d["port"]):
            old_kh.unlink(missing_ok=True)  # new address: trust it again
        elif new["name"] != d["name"] and old_kh.exists():
            old_kh.rename(core.kh_file(new))
        self.trusted_until.pop(d["name"], None)
        self.hosts[self.hosts.index(d)] = new
        tab = self.dev_tabs.pop(d["name"], None)
        if tab is not None:
            self.dev_tabs[new["name"]] = tab
            tab.set_device(new)
            self.tabs.setTabText(self.tabs.indexOf(tab), new["name"])
        self.save()
        self.refresh()
        if dlg.pw.text():
            self.setup_key(new, dlg.pw.text())

    def remove_device(self, d: dict):
        if self.is_busy(d):
            return
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Remove device")
        box.setText(f"Remove “{d['name']}” ({d['user']}@{d['host']}) from the list?")
        cb = QtWidgets.QCheckBox("Also remove SSH Deck's key from the device (device must be on)")
        box.setCheckBox(cb)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
        if box.exec() != QtWidgets.QMessageBox.Yes:
            return
        if cb.isChecked():
            def after(ok: bool, msg: str):
                if not ok:
                    ans = QtWidgets.QMessageBox.question(
                        self, "Remove device", f"The key could not be removed:\n{msg}\n\n"
                        "Remove the device from the list anyway?")
                    if ans != QtWidgets.QMessageBox.Yes:
                        return
                self._forget(d)
            self.remove_key(d, then=after)
        else:
            self._forget(d)

    def _forget(self, d: dict):
        tab = self.dev_tabs.get(d["name"])
        if tab is not None:
            self.close_tab(self.tabs.indexOf(tab))
        core.kh_file(d).unlink(missing_ok=True)
        self.trusted_until.pop(d["name"], None)
        if d in self.hosts:
            self.hosts.remove(d)
        self.save()
        self.refresh()

    # ---- key menu
    def protect_key(self):
        ans = QtWidgets.QMessageBox.question(
            self, "Protect key with a passphrase",
            "A terminal opens and asks for a new passphrase for the SSH Deck key.\n\n"
            "After that, SSH Deck asks for it once (dialog) and keeps the key unlocked for "
            f"{self.settings['unlock_minutes']} minutes, only while SSH Deck runs.\n\n"
            "To remove the passphrase later, do the same and leave it empty.\n\nContinue?")
        if ans == QtWidgets.QMessageBox.Yes:
            core.AGENT.lock_now()
            self.spawn(self.in_terminal([core.BIN["ssh-keygen"], "-p", "-f", str(core.KEY)],
                                        hold_always=True))

    def unlock_key(self):
        mins = self.settings["unlock_minutes"]
        self.run(lambda: core.ensure_unlocked(mins),
                 lambda ok: self.say("Key unlocked." if ok is True else "Key not unlocked."))

    def lock_key(self):
        core.AGENT.lock_now()
        self.trusted_until.clear()
        self.say("Key locked.")

    def open_settings(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(core.SETTINGS_FILE)))


# ---------------------------------------------------------------- app start

def install_desktop_file() -> bool:
    """App-menu entry; KDE/Wayland also takes the window logo from it."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = data / "applications" / f"{core.APP}.desktop"
    script = core.LAUNCHER
    if any(c in str(script) + str(core.ICON) + sys.executable for c in '"`$\\\n'):
        return False  # unsafe for the Exec line; skip, the app still works
    text = ("[Desktop Entry]\nType=Application\nName=SSH Deck\n"
            "Comment=Save devices and connect with SSH in one click\n"
            f'Exec="{sys.executable}" "{script}"\nIcon={core.ICON}\n'
            "Terminal=false\nCategories=Network;RemoteAccess;\n")
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def single_instance(path: Path) -> QtNetwork.QLocalServer | None:
    """None if SSH Deck already runs (it is asked to show itself), else our server."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(str(path))
    if sock.waitForConnected(500):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return None
    QtNetwork.QLocalServer.removeServer(str(path))
    server = QtNetwork.QLocalServer()
    server.setSocketOptions(QtNetwork.QLocalServer.UserAccessOption)
    server.listen(str(path))
    return server


def main() -> int:
    missing = core.resolve_bins()
    if missing:
        print(f"Missing program: {missing} (install OpenSSH / util-linux)", file=sys.stderr)
        return 1
    os.umask(0o077)
    core.ensure_dirs()
    core.ensure_key()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("SSH Deck")
    app.setWindowIcon(QtGui.QIcon(str(core.ICON)))
    if install_desktop_file():
        app.setDesktopFileName(core.APP)  # Wayland shows the icon from this file
    server = single_instance(core.RUN_DIR / "ui.sock")
    if server is None:
        return 0
    app.setStyleSheet(make_qss(app))
    w = MainWindow()

    def raise_window():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.waitForReadyRead(200)
            conn.close()
        w.showNormal()
        w.raise_()
        w.activateWindow()

    server.newConnection.connect(raise_window)
    w.show()
    rc = app.exec()
    core.AGENT.stop()
    return rc
