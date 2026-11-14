"""SSH Deck user interface (kept as one import point for the launcher and tests)."""
from .devicetab import BrowsePanel, DeviceTab, TransferRow  # noqa: F401
from .dialogs import DeviceDialog, PrefixEdit, SendDialog, SettingsDialog  # noqa: F401
from .theme import guess_prefix, make_qss  # noqa: F401
from .window import (DeviceRow, MainWindow, install_desktop_file, main,  # noqa: F401
                     single_instance)
