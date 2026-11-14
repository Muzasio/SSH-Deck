"""SSH Deck main window, device list and app start."""
from __future__ import annotations

import html
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

from . import core
from .devicetab import DeviceTab
from .dialogs import DeviceDialog, SettingsDialog
from .theme import (CHECK_HELP, PILL, button, dot_icon, fp_text, guess_prefix, label,
                    local_paths, make_qss, more_button, repolish)


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
        menu_b = more_button("Settings and key")
        m = menu_b.menu()
        m.addAction("Settings…", self.open_settings)
        m.addSeparator()
        m.addAction("Protect key with a passphrase…", self.protect_key)
        m.addAction("Unlock key now", self.unlock_key)
        m.addAction("Lock key now", self.lock_key)
        m.addSeparator()
        m.addAction("Refresh status (F5)", self.ping_all)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search devices")
        self.search.setToolTip("Filter by name, user or IP (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(230)
        self.search.textChanged.connect(self.apply_filter)
        self.count = label("", "muted")
        hcol = QtWidgets.QVBoxLayout()
        hcol.setSpacing(1)
        hcol.addWidget(label("Devices", "title"))
        hcol.addWidget(self.count)
        top = QtWidgets.QHBoxLayout()
        top.addLayout(hcol, 1)
        top.addWidget(self.search, 0, QtCore.Qt.AlignBottom)
        top.addSpacing(4)
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
                         ("Ctrl+T", lambda: self._tab_do("terminal")),
                         ("Ctrl+,", self.open_settings), ("Ctrl+F", self.focus_search)):
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
        self.search.setVisible(n >= 2)  # nothing to search with 0 or 1 device
        if n < 2:
            self.search.clear()
        self.apply_filter()
        self.ping_all()

    def apply_filter(self, *_):
        """Show only devices whose name, user or IP contain the search text."""
        q = self.search.text().strip().lower()
        shown = 0
        for i in range(self.list.count()):
            row = self.list.itemWidget(self.list.item(i))
            d = row.d if isinstance(row, DeviceRow) else {}
            hit = not q or any(q in str(d.get(k, "")).lower() for k in ("name", "user", "host"))
            self.list.item(i).setHidden(not hit)
            shown += hit
        if not self.hosts:
            self.empty.setText("No devices yet. Click “+ Add device”.")
        elif not shown:
            self.empty.setText(f"No devices match “{self.search.text().strip()}”. "
                               "Clear the search to see all devices.")
        self.list.setVisible(shown > 0)
        self.empty.setVisible(shown == 0)

    def focus_search(self):
        if self.search.isVisible():
            self.tabs.setCurrentIndex(0)
            self.search.setFocus()
            self.search.selectAll()

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
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec() != QtWidgets.QDialog.Accepted or dlg.result_settings is None:
            return
        self.settings = dlg.result_settings
        self.timer.setInterval(self.settings["check_every_seconds"] * 1000)
        self.save()  # connect timeout lives in the generated ssh_config
        self.trusted_until.clear()
        self.say("Settings saved.")


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
