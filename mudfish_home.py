"""Locates the installed Mudfish version under /opt/mudfish and exposes its
bin/share/var subdirectories, so tray.py and broker.py share a single place
that knows how a Mudfish install is laid out on disk.

Auto-detects at import time, so MUDFISH_HOME/MUDFISH_BIN_DIR/MUDFISH_SHARE_DIR
/MUDFISH_VAR_DIR are usable right away. A caller that wants to honor a
--mudfish-home override should call configure(path) early, before anything
else reads these — tray.py does this in main(), and forwards the resolved
MUDFISH_HOME to broker.py so both processes agree on the same install.
"""
import os
from glob import glob

_INSTALL_ROOT = "/opt/mudfish"

MUDFISH_HOME = None
MUDFISH_BIN_DIR = None
MUDFISH_SHARE_DIR = None
MUDFISH_VAR_DIR = None


def _version_key(version_dir):
    # version_dir looks like /opt/mudfish/<version>; sort by the version's
    # numeric components so e.g. 6.10.0 correctly outranks 6.9.0 (plain
    # string sorting would put "6.10.0" before "6.9.0").
    version = os.path.basename(version_dir)
    return tuple(int(part) if part.isdigit() else part for part in version.split("."))


def _detect_home():
    matches = glob(os.path.join(_INSTALL_ROOT, "*"))
    return max(matches, key=_version_key, default=None)


def configure(home=None):
    """(Re-)resolve MUDFISH_HOME and its bin/share/var subdirectories: to an
    explicit override if given, otherwise by auto-detecting the
    highest-versioned install under /opt/mudfish."""
    global MUDFISH_HOME, MUDFISH_BIN_DIR, MUDFISH_SHARE_DIR, MUDFISH_VAR_DIR
    MUDFISH_HOME = home or _detect_home()
    MUDFISH_BIN_DIR = os.path.join(MUDFISH_HOME, "bin") if MUDFISH_HOME else None
    MUDFISH_SHARE_DIR = os.path.join(MUDFISH_HOME, "share") if MUDFISH_HOME else None
    MUDFISH_VAR_DIR = os.path.join(MUDFISH_HOME, "var") if MUDFISH_HOME else None
    return MUDFISH_HOME


configure()
