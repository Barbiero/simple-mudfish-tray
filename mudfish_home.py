"""Locates the installed Mudfish version under /opt/mudfish and exposes its
bin/share/var subdirectories, so tray.py and broker.py share a single place
that knows how a Mudfish install is laid out on disk.

Nothing is auto-detected at import time: a caller must call configure(),
which either honors an explicit --mudfish-home override or auto-detects the
highest-versioned install under /opt/mudfish, and raises MudfishNotFoundError
if neither yields a usable install. Once configure() returns successfully,
MUDFISH_HOME/MUDFISH_BIN_DIR/MUDFISH_SHARE_DIR/MUDFISH_VAR_DIR are guaranteed
to be set — tray.py calls configure() in main(), before anything else reads
these, and forwards the resolved MUDFISH_HOME to broker.py so both processes
agree on the same install.
"""
import os
from glob import glob

_INSTALL_ROOT = "/opt/mudfish"

MUDFISH_HOME: str
MUDFISH_BIN_DIR: str
MUDFISH_SHARE_DIR: str
MUDFISH_VAR_DIR: str


class MudfishNotFoundError(RuntimeError):
    """Raised when no valid Mudfish install could be resolved."""


def _version_key(version_dir: str) -> tuple[int | str, ...]:
    # version_dir looks like /opt/mudfish/<version>; sort by the version's
    # numeric components so e.g. 6.10.0 correctly outranks 6.9.0 (plain
    # string sorting would put "6.10.0" before "6.9.0").
    version = os.path.basename(version_dir)
    return tuple(int(part) if part.isdigit() else part for part in version.split("."))


def _detect_home() -> str | None:
    matches = glob(os.path.join(_INSTALL_ROOT, "*"))
    return max(matches, key=_version_key, default=None)


def configure(home: str | None = None) -> str:
    """(Re-)resolve MUDFISH_HOME and its bin/share/var subdirectories: to an
    explicit override if given, otherwise by auto-detecting the
    highest-versioned install under /opt/mudfish. Raises MudfishNotFoundError
    if no install can be resolved, or the resolved one is missing one of its
    bin/share/var subdirectories."""
    global MUDFISH_HOME, MUDFISH_BIN_DIR, MUDFISH_SHARE_DIR, MUDFISH_VAR_DIR

    resolved = home or _detect_home()
    if not resolved or not os.path.isdir(resolved):
        if home:
            raise MudfishNotFoundError(f"--mudfish-home {home!r} is not a directory")
        raise MudfishNotFoundError(f"no Mudfish install found under {_INSTALL_ROOT}")

    bin_dir = os.path.join(resolved, "bin")
    share_dir = os.path.join(resolved, "share")
    var_dir = os.path.join(resolved, "var")
    for name, path in (("bin", bin_dir), ("share", share_dir), ("var", var_dir)):
        if not os.path.isdir(path):
            raise MudfishNotFoundError(f"{resolved!r} is not a valid Mudfish install: missing {name}/ directory")

    MUDFISH_HOME = resolved
    MUDFISH_BIN_DIR = bin_dir
    MUDFISH_SHARE_DIR = share_dir
    MUDFISH_VAR_DIR = var_dir
    return MUDFISH_HOME
