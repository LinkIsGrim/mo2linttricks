#!/usr/bin/env bash
# Install mo2linttricks into an MO2 instance.
#
#   scripts/install.sh <instance-dir> [--cyberpunk-patch]
#
# Copies the Python plugin to <instance>/plugins/mo2linttricks, builds (if Go is
# available) and bundles the GameMode helper, and optionally installs the
# Cyberpunk 2077 plugin patch. Close MO2 first.
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
instance="${1:-}"
patch=false
[[ "${2:-}" == "--cyberpunk-patch" ]] && patch=true

if [[ -z "$instance" || ! -f "$instance/ModOrganizer.exe" ]]; then
    echo "usage: $0 <mo2-instance-dir> [--cyberpunk-patch]" >&2
    exit 1
fi
if pgrep -f 'ModOrganizer\.exe' >/dev/null; then
    echo "MO2 is running; close it first." >&2
    exit 1
fi

helper="$repo/build/mo2lt-gamemode"
if command -v go >/dev/null; then
    (cd "$repo/helper" && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o "$helper" ./cmd/mo2lt-gamemode)
fi
[[ -x "$helper" ]] || { echo "GameMode helper not built (need Go, or a prebuilt build/mo2lt-gamemode)" >&2; exit 1; }

dest="$instance/plugins/mo2linttricks"
rm -rf "$dest"
mkdir -p "$dest/bin"
cp -r "$repo/plugin/mo2linttricks/." "$dest/"
find "$dest" -name __pycache__ -prune -exec rm -rf {} +
# .exe name on purpose: Wine runs ELF binaries natively, but only resolves them by an .exe name
cp "$helper" "$dest/bin/mo2lt-gamemode.exe"
echo "installed plugin -> $dest"

if $patch; then
    games="$instance/plugins/basic_games/games"
    target="$games/game_cyberpunk2077.py"
    [[ -f "$target" ]] || { echo "no $target (not a basic_games instance?)" >&2; exit 1; }
    if ! grep -q 'patched for MO2 2.5.2' "$target"; then
        [[ -f "$target.orig" ]] || cp -p "$target" "$target.orig"
    fi
    cp "$repo/patches/cyberpunk2077/game_cyberpunk2077.py" "$target"
    rm -f "$games/__pycache__"/game_cyberpunk2077*.pyc
    echo "installed Cyberpunk 2077 plugin 3.0.1 (patched) -> $target"
fi
