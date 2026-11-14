#!/usr/bin/env python3
"""SSH Deck launcher. The app lives in sshdeck_app/ (core, transfer, ui)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sshdeck_app.ui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
