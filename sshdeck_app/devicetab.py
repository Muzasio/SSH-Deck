"""SSH Deck device tab: send page, device files page, transfer list."""
from __future__ import annotations

import os
import posixpath
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import core, transfer as tr
from .dialogs import SendDialog
from .theme import (GREEN, RED, _more, button, card, color_label, label, local_paths,
                    repolish, set_pill, upload_pixmap)


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
        d, t = self.tab.d, self.win.settings["timeout_seconds"]
        color_label(self.msg, "Reading the folders on the device…", None)
        self.win.gate(d, self.tab.guard(lambda: self.win.run(
            lambda: tr.remote_tree(d, entries, t),
            self.tab.guard(lambda tree: self._walked(entries, target, tree)))))

    def _walked(self, entries: list[dict], target: str, tree):
        if isinstance(tree, Exception):
            color_label(self.msg, f"✕ {tree}", RED)
            return
        color_label(self.msg, "", None)
        clash = [e["name"] for e in entries if os.path.lexists(os.path.join(target, e["name"]))]
        mode = "all"
        if clash:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("Download")
            box.setIcon(QtWidgets.QMessageBox.Question)
            box.setText("Some items are already in that folder on this PC:")
            box.setInformativeText("\n".join(_more(clash, len(clash), 6)) +
                                   "\n\nResume skips files that are already complete here "
                                   "(same size and time) and gets the rest.")
            resume = box.addButton("Resume", QtWidgets.QMessageBox.AcceptRole)
            rep = box.addButton("Replace all", QtWidgets.QMessageBox.AcceptRole)
            skip = box.addButton("Skip those", QtWidgets.QMessageBox.AcceptRole) \
                if len(clash) < len(entries) else None
            box.addButton(QtWidgets.QMessageBox.Cancel)
            box.setDefaultButton(resume)
            box.exec()
            chosen = box.clickedButton()
            if chosen is None or chosen not in (resume, rep, skip):
                return
            mode = "resume" if chosen is resume else ("skip" if chosen is skip else "all")
        if mode == "skip":
            keep = {e["name"] for e in entries} - set(clash)
            tree = dict(tree, files=[f for f in tree["files"] if f[0].split("/")[0] in keep],
                        dirs=[x for x in tree["dirs"] if x.split("/")[0] in keep])
        self.tab.queue_download(entries, target, tree, resume=(mode == "resume"))


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
        dlg = SendDialog(self, d, dest, scan, clash, self.win.settings["junk"], free,
                         self.win.settings["speed_limit_mb_s"])
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

    def queue_download(self, entries: list[dict], target: str, tree: dict,
                       resume: bool = False):
        plan = tr.plan_download(tree, target, resume)
        st, c = plan["stats"], tree["counts"]
        skipped = []
        if c["links"]:
            skipped.append(f"{c['links']} link(s)")
        if c["badname"]:
            skipped.append(f"{c['badname']} unsafe name(s)")
        if st["unsafe"]:
            skipped.append(f"{st['unsafe']} item(s) under a link on this PC")
        if c["too_deep"]:
            skipped.append(f"{c['too_deep']} folder(s) nested too deep")
        if not plan["sizes"] and not plan["mkdirs"]:
            msg = (f"All {st['resumed']} file(s) are already complete here." if st["resumed"]
                   else "There is nothing to download.")
            QtWidgets.QMessageBox.information(self, "Download", msg)
            return
        free = tr.local_free(target)
        if st["bytes"] > free:
            QtWidgets.QMessageBox.warning(
                self, "Not enough space", f"Needs {tr.human(st['bytes'])}, but only "
                f"{tr.human(free)} is free in {target}.\nNothing was downloaded.")
            return
        try:
            for path in plan["mkdirs"]:
                os.makedirs(path, exist_ok=True)
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Download", f"Could not create a folder here:\n{e}")
            return
        name = entries[0]["name"] if len(entries) == 1 else f"{len(entries)} items"
        prov = QtWidgets.QFileIconProvider()
        icon = prov.icon(QtGui.QAbstractFileIconProvider.IconType.Folder if entries[0]["dir"]
                         else QtGui.QAbstractFileIconProvider.IconType.File)
        detail = f"{st['files']} files · {tr.human(st['bytes'])}  ·  saved to {target}"
        if st["resumed"]:
            detail += f"  ·  {st['resumed']} already here, skipped"
        if skipped:
            detail += "  ·  not downloaded: " + ", ".join(skipped)
        row = TransferRow(name, detail, icon, st["files"])
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
        sizes = [f[2] for f in plan["files"]] if kind == "up" else plan["sizes"]
        job = tr.SftpJob(self.d, plan["lines"], sizes, self,
                         limit=tr.limit_kbit(self.win.settings["speed_limit_mb_s"]))
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
