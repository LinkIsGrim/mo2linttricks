# mo2linttricks

Mod Organizer 2 plugins for Linux setups made with [MO2-LINT](https://github.com/furglitch/modorganizer2-linux-installer) (most of it works on any MO2 2.5 instance).

- **Modlist Sync**: mirrors your modlist to a private GitHub repo, `mo2-mods-<game>`, automatically: load order, enabled state, and where every mod came from (Nexus mod and file IDs, URLs, archive names). MO2's own export drops the source; this keeps it.
- **Restore**: rebuilds a synced profile in an instance: downloads missing mods, runs their installers, then applies the order and enabled state.
- **GameMode**: puts the game, and only the game, into [Feral GameMode](https://github.com/FeralInteractive/gamemode) when MO2 launches it under Proton. It doesn't use `gamemoderun`, which breaks MO2.
- **Cyberpunk 2077 patch**: the upstream Cyberpunk plugin 3.0.1 (it loads CET and RED4ext as forced-load libraries, so Root Builder isn't needed), with a one-line fix so it loads on MO2 2.5.2.

## Install

```bash
scripts/install.sh ~/Games/mo2-lint/mo2-cyberpunk2077-steam            # plugin + GameMode helper
scripts/install.sh ~/Games/mo2-lint/mo2-cyberpunk2077-steam --cyberpunk-patch
```

Close MO2 first. Building the helper needs Go. `mo2-lint update` re-extracts MO2 over the instance, so rerun with `--cyberpunk-patch` after updating; the mo2linttricks plugin folder itself survives updates.

## GitHub sign-in (one time)

Sync signs in with OAuth device flow using a GitHub OAuth App that you own, so no secret is shipped or stored.

1. Go to <https://github.com/settings/applications/new>. Use the name `mo2linttricks` and `http://localhost` for both the homepage and callback URLs (neither is used).
2. Tick **Enable Device Flow**, then register the app and copy its **Client ID**.
3. In MO2, open **Settings > Plugins > mo2linttricks Modlist Sync**, set `github_client_id`, then run **Tools > mo2linttricks > Sign in to GitHub**. MO2 shows a code; approve it on github.com.

The token is stored in `%APPDATA%\mo2linttricks\github.json` (inside the Wine prefix). It's never written to the instance or the repo. It has the `repo` scope, which GitHub requires to create and write private repos.

## Sync

When it syncs:

- about 30 seconds after the last change (installs, removals, enabling or disabling, reordering mods or plugins, finished downloads, profile changes);
- right before a game launches, if anything changed;
- at MO2 exit;
- once at startup, to catch edits made while MO2 was closed;
- whenever you run **Tools > mo2linttricks > Sync modlist now**.

Each sync is one commit, made through GitHub's Git Data API, so no git is needed inside Wine. If nothing changed, no commit is made.

Repo layout. Only these paths are managed; anything else you add, such as your own README, is left alone.

| Path | Contents |
|:--|:--|
| `mo2linttricks.json` | game, MO2 version, profile names |
| `mods.json` | per mod: `source` (`nexus` / `url` / `manual` / `separator`), version, Nexus `modid` + `fileids`, archive name, URLs |
| `mods/<mod>/meta.ini` | MO2's metadata for the mod (minus the bulky `nexusDescription`) |
| `downloads/<archive>.meta` | MO2's download metadata |
| `profiles/<profile>/modlist.json` | load order, lowest priority first, with enabled state and kind |
| `profiles/<profile>/*.txt, *.ini` | raw profile files (`modlist.txt`, `plugins.txt`, `loadorder.txt`, profile INIs) |

Settings (under **mo2linttricks Modlist Sync**): `repo_name` (empty means `mo2-mods-<game>`), `debounce_seconds`, `sync_downloads_meta`.

## Restore

Run **Tools > mo2linttricks > Restore modlist from GitHub**, pick a synced profile, then:

1. **Download missing.** Nexus files go through MO2's download manager. The Nexus API only gives direct links to Premium accounts; for other accounts the file's page opens in your browser instead. Click *Mod Manager Download* there and the dialog picks up the download. Mods sourced from a URL download from their link.
2. **Install downloaded.** Runs MO2's normal installer for each archive, one at a time.
3. **Apply order & enabled state.** Creates separators, sets mod priorities and enabled state, and applies the plugin load order for games that have one.

Changes go into the current profile. FOMOD choices aren't recorded, so you choose installer options again.

## GameMode

MO2's Python runs inside Wine, inside Steam's runtime container. Before MO2 launches the game (or its launcher), the plugin starts `bin/mo2lt-gamemode.exe`. That's a static Linux binary which Wine runs natively. It finds the game's process, registers it with GameMode over the D-Bus session bus, and exits when the game exits, so Steam's "running" state isn't held open. Its log is at `~/.local/state/mo2linttricks/gamemode.log`.

Verified inside the Steam Linux Runtime 4 container with GE-Proton11-12: the session bus is reachable, process IDs match the host's, and registration activates GameMode.

Settings (under **mo2linttricks GameMode**): `extra_targets` (more executable names), `all_executables`, `helper_path`.

`scripts/cp2077-gamemode-watcher` is the older Steam launch-option wrapper, kept as a fallback.

## Development

```bash
python3 -I tests/test_core.py                      # core logic, no MO2 needed
(cd helper && go test ./...)
mypy --python-version 3.12 --ignore-missing-imports --check-untyped-defs plugin/mo2linttricks   # with mobase-stubs 2.5.2 + PyQt6
```

`plugin/mo2linttricks/core` has no MO2 or Qt imports. MO2 2.5 bundles Python 3.12, so keep the code 3.12-compatible.
