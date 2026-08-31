#!/usr/bin/env python3
"""Mudfish VPN tray icon, implemented directly against the StatusNotifierItem
and DBusMenu D-Bus interfaces (no Qt/GTK) via dbus-next.

Start/Stop/Quit are relayed to a root-privileged broker (mudfish-broker.py,
launched once via pkexec per tray session) over a peer-credential-verified
Unix socket, instead of shelling out to pkexec for every action.

The tray icon has four badge states: stopped (mudrun-headless is not
running), starting/stopping (pending, transitional), started
(mudrun-headless is up but no VPN item is active), and running
(mudrun-headless is up and a VPN item is active). The last distinction is
made by watching, via inotify, for the appearance/disappearance of
{mudfish_home}/var/.mudfish.vsm — the same file mudadm itself uses to
detect a running item — since that file is world-readable and needs no
broker/root involvement."""
import argparse
import asyncio
import os
import signal
import sys
import time
import webbrowser
from typing import Any, Callable, Literal

from inotify_simple import INotify, flags as inotify_flags
from PIL import Image, ImageDraw
from platformdirs import user_cache_dir, user_runtime_dir

from dbus_next.constants import BusType, PropertyAccess
from dbus_next.signature import Variant
from dbus_next.aio.message_bus import MessageBus
from dbus_next.service import ServiceInterface, dbus_property, method, signal as dbus_signal

import mudfish_home

# dbus_next "a(iiay)" IconPixmap payload: a list of [width, height, argb_bytes].
Pixmap = list[list[Any]]
MenuItemProps = dict[str, Any]
PendingState = Literal["starting", "stopping"]

BROKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker.py")
DEBUG = os.environ.get("MUDFISH_TRAY_DEBUG") == "1"
CACHE_DIR = user_cache_dir("mudfish-tray")
LOG_PATH = os.path.join(CACHE_DIR, "mudfish-tray.log")
BROKER_LOG_PATH = os.path.join(CACHE_DIR, "mudfish-broker.log")

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

# D-Bus signatures used as return-type annotations below. Simple ones (e.g.
# "s", "a(iiay)") are written inline as string literals, matching normal
# dbus_next style — but these three contain nested ( )/{ } that pyright's
# forward-reference parser reads as broken Python expression syntax (a hard
# parse error, not a suppressible diagnostic) when used as a literal. Naming
# them sidesteps that: dbus_next's runtime introspection just needs the
# annotation to evaluate to a plain str, which a name reference still does.
_TOOLTIP_SIG = "(sa(iiay)ss)"
_LAYOUT_SIG = "u(ia{sv}av)"
_GROUP_PROPS_SIG = "a(ia{sv})"
_ARRAY_STR_SIG = "as"  # "as" is also a Python keyword, so it hits the same parser issue

PENDING_TIMEOUT_S = 30  # give up waiting for the broker to confirm start/stop
CONFIG_URL = "http://localhost:8282"

# Menu item ids
ID_CONFIG = 1
ID_SEP1 = 2
ID_TOGGLE = 3
ID_SEP2 = 4
ID_QUIT = 5


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def rgba_to_argb_bytes(im: Image.Image) -> bytes:
    raw = im.tobytes()  # R,G,B,A per pixel
    out = bytearray(len(raw))
    out[0::4] = raw[3::4]  # A
    out[1::4] = raw[0::4]  # R
    out[2::4] = raw[1::4]  # G
    out[3::4] = raw[2::4]  # B
    return bytes(out)


def load_base_icon() -> Image.Image:
    size = (64, 64)
    path = os.path.join(mudfish_home.MUDFISH_SHARE_DIR, "mudrun_logo.png")
    if os.path.isfile(path):
        return Image.open(path).convert("RGBA").resize(size)
    return Image.new("RGBA", size, (100, 100, 100, 255))


def make_pixmap(base_img: Image.Image, color: tuple[int, int, int, int], fill_alpha: int | None = None) -> Pixmap:
    """Draw a status dot in the corner of base_img. With fill_alpha=None,
    the dot is solidly filled with color. Otherwise it's drawn as a ring
    (full-opacity outline) around a center filled at fill_alpha, blended
    over the icon beneath — used for "up but not fully active yet" states."""
    im = base_img.copy()
    w, h = im.size
    dot = int(w * 0.42)
    box = [w - dot, h - dot, w, h]

    if fill_alpha is None:
        draw = ImageDraw.Draw(im)
        draw.ellipse(box, fill=color)
        return [[w, h, rgba_to_argb_bytes(im)]]

    r, g, b = color[:3]
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    outline_width = max(1, dot // 8)
    draw.ellipse(box, fill=(r, g, b, fill_alpha), outline=(r, g, b, 255), width=outline_width)
    im = Image.alpha_composite(im, overlay)
    return [[w, h, rgba_to_argb_bytes(im)]]


class VsmWatcher:
    """Watches a directory via inotify for one file's creation/removal,
    so we can tell whether a VPN item is running without polling (e.g. via
    `ls`) or shelling out to `mudadm`."""

    _WATCH_FLAGS = inotify_flags.CREATE | inotify_flags.DELETE | inotify_flags.MOVED_TO | inotify_flags.MOVED_FROM

    def __init__(self, on_change: Callable[[bool], None]) -> None:
        self._on_change = on_change
        self._inotify: INotify | None = None
        self._dir: str | None = None
        self._filename: str | None = None
        self.exists = False

    def is_active(self) -> bool:
        return self._inotify is not None

    def start(self, path: str) -> None:
        self._dir, self._filename = os.path.split(path)
        self.exists = os.path.exists(path)

        try:
            inotify = INotify(nonblocking=True)
        except OSError as e:
            log(f"could not open inotify: {e}")
            return
        try:
            inotify.add_watch(self._dir, self._WATCH_FLAGS)
        except OSError as e:
            log(f"could not watch {self._dir} for VPN state: {e}")
            inotify.close()
            return

        self._inotify = inotify
        asyncio.get_running_loop().add_reader(inotify.fd, self._on_readable)

    def _on_readable(self) -> None:
        assert self._inotify is not None and self._dir is not None and self._filename is not None
        changed = any(event.name == self._filename for event in self._inotify.read(timeout=0))
        if not changed:
            return
        new_exists = os.path.exists(os.path.join(self._dir, self._filename))
        if new_exists != self.exists:
            self.exists = new_exists
            self._on_change(new_exists)

    def stop(self) -> None:
        if self._inotify is not None:
            asyncio.get_running_loop().remove_reader(self._inotify.fd)
            self._inotify.close()
            self._inotify = None


class Controller:
    """Holds shared mutable state used by both D-Bus interfaces."""

    def __init__(self) -> None:
        base = load_base_icon()
        self.pixmap_running = make_pixmap(base, (46, 204, 113, 255))
        self.pixmap_started = make_pixmap(base, (46, 204, 113, 255), fill_alpha=30)
        self.pixmap_stopped = make_pixmap(base, (231, 76, 60, 255))
        self.pixmap_pending = make_pixmap(base, (241, 196, 15, 255))

        self._fake_running = False  # DEBUG mode only
        self._broker_running = False  # authoritative in real mode, pushed by the broker
        self._pending: PendingState | None = None
        self._pending_timeout_handle: asyncio.TimerHandle | None = None
        self._quit_after_stop = False

        # VPN-item state: unrelated to mudrun-headless's own process state,
        # kept separately and never simulated in DEBUG mode (starting a VPN
        # item happens out of band, via the web configuration UI).
        self._vsm_watcher = VsmWatcher(self._on_vsm_change)
        self._vsm_exists = False

        self._broker_proc: asyncio.subprocess.Process | None = None
        self._broker_writer: asyncio.StreamWriter | None = None
        self._socket_path: str | None = None

        self.sni: "StatusNotifierItem | None" = None
        self.menu: "DBusMenu | None" = None
        self.quit_event: asyncio.Event | None = None

    def request_quit(self) -> None:
        if self.quit_event:
            self.quit_event.set()

    def request_quit_all(self) -> None:
        if DEBUG:
            if not self.is_running() and self._pending is None:
                self.request_quit()
                return
            log("Quit clicked: stopping mudfish before exiting tray")
            self._quit_after_stop = True
            self.stop_mudfish()
            return
        asyncio.ensure_future(self._quit_all_async())

    async def _quit_all_async(self) -> None:
        if self._broker_writer is None:
            self.request_quit()
            return
        if self.is_running() or self._pending is not None:
            log("Quit clicked: stopping mudfish before exiting tray")
            self._quit_after_stop = True
            self.stop_mudfish()
            return
        await self._send_broker_command("quit")
        self.request_quit()

    def is_running(self) -> bool:
        return self._fake_running if DEBUG else self._broker_running

    def is_vpn_running(self) -> bool:
        return self._vsm_exists

    def current_pixmap(self) -> Pixmap:
        if self._pending:
            return self.pixmap_pending
        if not self.is_running():
            return self.pixmap_stopped
        return self.pixmap_running if self.is_vpn_running() else self.pixmap_started

    def status_text(self) -> str:
        if self._pending == "starting":
            return "Starting…"
        if self._pending == "stopping":
            return "Stopping…"
        if not self.is_running():
            return "Stopped"
        return "Running" if self.is_vpn_running() else "Started"

    def tooltip_text(self) -> str:
        return f"Mudfish VPN — {self.status_text().lower()}"

    def menu_items(self) -> list[tuple[int, MenuItemProps]]:
        running = self.is_running()
        pending = self._pending is not None
        return [
            (ID_CONFIG, {"label": "Configuration", "enabled": running}),
            (ID_SEP1, {"type": "separator"}),
            (ID_TOGGLE, {"label": "Stop Mudfish" if running else "Start Mudfish", "enabled": not pending}),
            (ID_SEP2, {"type": "separator"}),
            (ID_QUIT, {"label": "Quit", "enabled": True}),
        ]

    def start_mudfish(self) -> None:
        self._enter_pending("starting")
        if DEBUG:
            log("DEBUG: pretending to start (no pkexec)")
            asyncio.get_running_loop().call_later(1.5, self._debug_set_running, True)
            return
        asyncio.ensure_future(self._send_broker_command("start"))

    def open_configuration(self) -> None:
        if not self.is_running():
            return
        webbrowser.open(CONFIG_URL)

    def stop_mudfish(self) -> None:
        self._enter_pending("stopping")
        if DEBUG:
            log("DEBUG: pretending to stop (no pkexec)")
            asyncio.get_running_loop().call_later(1.5, self._debug_set_running, False)
            return
        asyncio.ensure_future(self._send_broker_command("stop"))

    def _debug_set_running(self, value: bool) -> None:
        log(f"DEBUG: fake_running -> {value}")
        self._fake_running = value
        self._pending = None
        if self._quit_after_stop and not value:
            self._quit_after_stop = False
            self.request_quit()
            return
        self.notify_all()

    def _enter_pending(self, pending: PendingState) -> None:
        self._cancel_pending_timeout()
        self._pending = pending
        self.notify_all()
        if not DEBUG:
            self._pending_timeout_handle = asyncio.get_running_loop().call_later(
                PENDING_TIMEOUT_S, self._on_pending_timeout
            )

    def _cancel_pending_timeout(self) -> None:
        if self._pending_timeout_handle:
            self._pending_timeout_handle.cancel()
            self._pending_timeout_handle = None

    def _on_pending_timeout(self) -> None:
        self._pending_timeout_handle = None
        if not self._pending:
            return
        log(f"timed out waiting for broker to confirm '{self._pending}'")
        self._pending = None
        if self._quit_after_stop:
            log("giving up on quit-after-stop; leaving tray open")
            self._quit_after_stop = False
        self.notify_all()

    # --- VPN item state (mudfish.vsm watcher) -------------------------------

    def ensure_vsm_watching(self) -> None:
        if DEBUG or self._vsm_watcher.is_active():
            return
        self._vsm_watcher.start(os.path.join(mudfish_home.MUDFISH_VAR_DIR, ".mudfish.vsm"))
        self._vsm_exists = self._vsm_watcher.exists

    def _on_vsm_change(self, exists: bool) -> None:
        log(f"VPN item state -> {'running' if exists else 'stopped'}")
        self._vsm_exists = exists
        self.notify_all()

    def notify_all(self) -> None:
        if self.sni:
            self.sni.NewIcon()
            self.sni.NewToolTip()
            self.sni.NewStatus(self.status_text())
        if self.menu:
            self.menu.push_layout_update()

    # --- broker connection management -------------------------------------

    async def ensure_broker(self) -> bool:
        if self._broker_writer is not None:
            return True
        return await self._launch_and_connect_broker()

    async def _launch_and_connect_broker(self) -> bool:
        uid = os.getuid()
        runtime_dir = user_runtime_dir("mudfish-tray")
        try:
            os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        except OSError:
            runtime_dir = "/tmp"
        self._socket_path = os.path.join(runtime_dir, f"mudfish-tray-{os.getpid()}.sock")

        log(f"launching broker via pkexec, socket={self._socket_path}")
        try:
            self._broker_proc = await asyncio.create_subprocess_exec(
                "pkexec", BROKER_SCRIPT, self._socket_path, BROKER_LOG_PATH, str(uid), mudfish_home.MUDFISH_HOME
            )
        except Exception as e:
            log(f"failed to launch broker: {e!r}")
            return False

        for _ in range(150):  # up to ~15s for the auth dialog + broker startup
            if os.path.exists(self._socket_path):
                break
            if self._broker_proc.returncode is not None:
                log(f"pkexec/broker exited early (code {self._broker_proc.returncode}) — auth cancelled?")
                return False
            await asyncio.sleep(0.1)
        else:
            log("timed out waiting for broker socket to appear")
            return False

        try:
            reader, writer = await asyncio.open_unix_connection(self._socket_path)
        except OSError as e:
            log(f"failed to connect to broker socket: {e!r}")
            return False

        self._broker_writer = writer
        asyncio.ensure_future(self._read_broker(reader))
        return True

    async def _send_broker_command(self, cmd: str) -> None:
        if not await self.ensure_broker() or self._broker_writer is None:
            log(f"could not send '{cmd}': broker unavailable")
            self._cancel_pending_timeout()
            self._pending = None
            self.notify_all()
            return
        try:
            self._broker_writer.write((cmd + "\n").encode())
            await self._broker_writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            log(f"broker connection lost while sending '{cmd}'")
            self._broker_writer = None

    async def _read_broker(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                log("broker connection closed")
                self._broker_writer = None
                self._broker_running = False
                self._cancel_pending_timeout()
                self._pending = None
                self.notify_all()
                self.request_quit()
                return

            text = line.decode(errors="replace").strip()
            if text.startswith("state: "):
                new_state = text.split(": ", 1)[1] == "running"
                self._cancel_pending_timeout()
                self._pending = None
                self._broker_running = new_state
                if new_state:
                    self.ensure_vsm_watching()
                if self._quit_after_stop and not new_state:
                    self._quit_after_stop = False
                    await self._send_broker_command("quit")
                    self.request_quit()
                    return
                self.notify_all()
            elif text.startswith("error: "):
                log(f"broker error: {text}")
            else:
                log(f"broker: {text}")


class StatusNotifierItem(ServiceInterface):
    def __init__(self, controller: Controller) -> None:
        super().__init__("org.kde.StatusNotifierItem")
        self.controller = controller

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "mudfish-tray"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Mudfish VPN"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "i":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return self.controller.current_pixmap()

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> _TOOLTIP_SIG:
        return ["", [], "Mudfish VPN", self.controller.tooltip_text()]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return MENU_PATH

    @method()
    def Activate(self, x: "i", y: "i") -> None:
        pass

    @method()
    def SecondaryActivate(self, x: "i", y: "i") -> None:
        pass

    @method()
    def ContextMenu(self, x: "i", y: "i") -> None:
        pass

    @method()
    def Scroll(self, delta: "i", orientation: "s") -> None:
        pass

    @dbus_signal()
    def NewIcon(self) -> None:
        pass

    @dbus_signal()
    def NewToolTip(self) -> None:
        pass

    @dbus_signal()
    def NewStatus(self, status: str) -> "s":
        return status


class DBusMenu(ServiceInterface):
    def __init__(self, controller: Controller) -> None:
        super().__init__("com.canonical.dbusmenu")
        self.controller = controller
        self.revision = 0

    def _item_props(self, base_props: MenuItemProps, names_filter: list[str] | None) -> dict[str, Variant]:
        props: MenuItemProps = {"enabled": True, "visible": True, "type": "standard"}
        props.update(base_props)
        if names_filter:
            props = {k: v for k, v in props.items() if k in names_filter}
        return {k: Variant(_prop_sig(k), v) for k, v in props.items()}

    def _build_item(self, item_id: int, base_props: MenuItemProps, names_filter: list[str] | None) -> list[Any]:
        return [item_id, self._item_props(base_props, names_filter), []]

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> _ARRAY_STR_SIG:
        return []

    @method()
    def GetLayout(self, parent_id: "i", recursion_depth: "i", property_names: _ARRAY_STR_SIG) -> _LAYOUT_SIG:
        items = self.controller.menu_items()
        if parent_id == 0:
            children = [
                Variant("(ia{sv}av)", self._build_item(iid, props, property_names))
                for iid, props in items
            ]
            root = [0, {"children-display": Variant("s", "submenu")}, children]
            return [self.revision, root]
        for iid, props in items:
            if iid == parent_id:
                return [self.revision, self._build_item(iid, props, property_names)]
        return [self.revision, [parent_id, {}, []]]

    @method()
    def GetGroupProperties(self, ids: "ai", property_names: _ARRAY_STR_SIG) -> _GROUP_PROPS_SIG:
        items = dict(self.controller.menu_items())
        result = []
        for iid in ids:
            if iid in items:
                result.append([iid, self._item_props(items[iid], property_names)])
        return result

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        items = dict(self.controller.menu_items())
        props = self._item_props(items.get(item_id, {}), None)
        return props.get(name, Variant("s", ""))

    @method()
    def Event(self, item_id: "i", event_id: "s", data: "v", timestamp: "u") -> None:
        if event_id != "clicked":
            return
        if item_id == ID_TOGGLE:
            if self.controller.is_running():
                self.controller.stop_mudfish()
            else:
                self.controller.start_mudfish()
        elif item_id == ID_CONFIG:
            self.controller.open_configuration()
        elif item_id == ID_QUIT:
            self.controller.request_quit_all()

    @method()
    def AboutToShow(self, item_id: "i") -> "b":
        return False

    @dbus_signal()
    def LayoutUpdated(self, revision: int, parent: int) -> "ui":
        return [revision, parent]

    def push_layout_update(self) -> None:
        self.revision += 1
        self.LayoutUpdated(self.revision, 0)


def _prop_sig(name: str) -> str:
    return {"enabled": "b", "visible": "b"}.get(name, "s")


async def main_async() -> None:
    log(f"--- starting mudfish-tray.py (DEBUG={DEBUG}) ---")
    controller = Controller()

    bus = await MessageBus(bus_type=BusType.SESSION).connect()

    sni = StatusNotifierItem(controller)
    menu = DBusMenu(controller)
    controller.sni = sni
    controller.menu = menu

    bus.export(SNI_PATH, sni)
    bus.export(MENU_PATH, menu)

    service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
    await bus.request_name(service_name)
    log(f"requested bus name {service_name}")

    watcher_intf = None
    watcher_bus_name = "org.kde.StatusNotifierWatcher"
    try:
        introspection = await bus.introspect(watcher_bus_name, "/StatusNotifierWatcher")
        proxy = bus.get_proxy_object(watcher_bus_name, "/StatusNotifierWatcher", introspection)
        watcher_intf = proxy.get_interface("org.kde.StatusNotifierWatcher")
        # call_register_status_notifier_item is generated at runtime from the
        # introspected D-Bus interface, so it isn't visible to the type checker.
        await watcher_intf.call_register_status_notifier_item(service_name)  # type: ignore[attr-defined]
        log(f"registered with {watcher_bus_name}")
    except Exception as e:
        log(f"could not register with {watcher_bus_name}: {e!r}")

    if watcher_intf is None:
        log("ERROR: no StatusNotifierWatcher available, tray icon will not appear")

    if DEBUG:
        log("DEBUG mode: will NOT touch mudrun-headless or ask for a password")
    else:
        controller.ensure_vsm_watching()
        if not controller.is_running():
            controller.start_mudfish()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    controller.quit_event = stop_event

    def _handle_sigint() -> None:
        log("shutdown signal received")
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _handle_sigint)
    loop.add_signal_handler(signal.SIGTERM, _handle_sigint)

    await stop_event.wait()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mudfish VPN tray icon")
    parser.add_argument(
        "--mudfish-home",
        metavar="PATH",
        help="Mudfish install directory to use (e.g. /opt/mudfish/6.9.0) instead of "
             "auto-detecting the newest one under /opt/mudfish/*",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        mudfish_home.configure(args.mudfish_home)
    except mudfish_home.MudfishNotFoundError as e:
        print(f"mudfish-tray: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
