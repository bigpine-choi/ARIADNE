"""Local pilot configuration. Never place operational data in the source tree."""

import os
from pathlib import Path
import platform


def data_directory() -> Path:
    explicit = os.environ.get("ARIADNE_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if platform.system() == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "ARIADNE"
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "ariadne"


def default_database() -> Path:
    return data_directory() / "ariadne.sqlite3"
