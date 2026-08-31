# Simple Mudfish Tray

A minimal system tray icon for [Mudfish VPN](https://mudfish.net/), meant as a
companion for immutable Linux distributions (e.g. Fedora Silverblue/Kinoite,
SteamOS) where Mudfish's official installer can't fully install its UI parts
through the system installer, because that requires writing to system paths
that are read-only on these distros. The `mudrun-headless` CLI itself still
installs and works fine — this project just fills in the missing tray icon
and desktop integration.

It talks directly to the StatusNotifierItem/DBusMenu D-Bus interfaces (no
Qt/GTK dependency) to show a tray icon that lets you start/stop
`mudrun-headless` and open the Mudfish web configuration UI. Starting and
stopping the VPN requires root, which is handled by a small broker process
launched once via `pkexec` and controlled over a peer-credential-verified
Unix socket, so you aren't prompted for a password on every click.

## Requirements

- Mudfish already installed under `/opt/mudfish`
- Python 3
- `pkexec` (polkit)
- A StatusNotifierItem-capable tray (KDE Plasma, GNOME with an
  AppIndicator-style extension, etc.)

## How to install

```bash
git clone https://github.com/Barbiero/simple-mudfish-tray.git
cd simple-mudfish-tray
./install.sh
```

This creates a Python virtualenv inside the project directory, installs the
few dependencies (`dbus-next`, `inotify_simple`, `pillow`, `platformdirs`)
into it, and writes
`~/.local/share/applications/mudfish-tray.desktop` so "Mudfish Tray" shows up
in your application launcher. No root or privileged access is needed for the
install itself — it only touches files under your home directory.

Launch it from your application launcher, or add it to your desktop
environment's autostart/startup applications if you want it running on
login.

## License

MIT — see [LICENSE](LICENSE).
