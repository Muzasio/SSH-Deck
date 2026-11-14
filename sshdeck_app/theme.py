"""SSH Deck look: style sheet, colours, small widget helpers (no app logic)."""
from __future__ import annotations

import ipaddress

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

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
QToolButton#more::menu-indicator, QToolButton#gear::menu-indicator { image: none; width: 0; }
QToolButton#gear { padding: 6px 10px; border-radius: 8px; border: 1px solid @line;
    background: palette(button); }
QToolButton#gear:hover { border-color: palette(highlight); }
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


def gear_icon(color: QtGui.QColor, size: int = 18) -> QtGui.QIcon:
    """A settings gear drawn in code: sharp at any scale, follows the theme colour."""
    import math
    icon = QtGui.QIcon()
    for px in (size, size * 2, size * 3):  # plain sizes; Qt picks the best match
        pm = QtGui.QPixmap(px, px)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        c = px / 2
        teeth, r_out, r_in, r_hole = 8, px * 0.48, px * 0.37, px * 0.17
        pts = []
        for i in range(teeth):
            a = 2 * math.pi * i / teeth
            half = math.pi / teeth  # flat-topped teeth
            for ang, rad in ((a - half * 0.95, r_in), (a - half * 0.5, r_out),
                             (a + half * 0.5, r_out), (a + half * 0.95, r_in)):
                pts.append(QtCore.QPointF(c + rad * math.cos(ang), c + rad * math.sin(ang)))
        body = QtGui.QPainterPath()
        body.addPolygon(QtGui.QPolygonF(pts + pts[:1]))
        hole = QtGui.QPainterPath()
        hole.addEllipse(QtCore.QPointF(c, c), r_hole, r_hole)
        p.fillPath(body.subtracted(hole), color)
        p.end()
        icon.addPixmap(pm)
    return icon


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


def _more(lst: list[str], total: int, show: int = 4) -> list[str]:
    out = [f"   {p}" for p in lst[:show]]
    if total > show:
        out.append(f"   … and {total - show} more")
    return out
