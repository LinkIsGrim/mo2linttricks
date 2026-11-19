# mo2linttricks

Mod Organizer 2 plugins for Linux setups made with [MO2-LINT](https://github.com/furglitch/modorganizer2-linux-installer) (most of it works on any MO2 2.5 instance).

- **Modlist Sync**: mirrors your modlist to a private GitHub repo, `mo2-mods-<game>`, automatically: load order, enabled state, and where every mod came from (Nexus mod and file IDs, URLs, archive names). MO2's own export drops the source; this keeps it.
- **Restore**: rebuilds a synced profile in an instance: downloads missing mods, runs their installers, then applies the order and enabled state.
- **GameMode**: puts the game, and only the game, into [Feral GameMode](https://github.com/FeralInteractive/gamemode) when MO2 launches it under Proton. It doesn't use `gamemoderun`, which breaks MO2.
- **Conflict resolver** (Cyberpunk 2077): MO2 only sees conflicts between loose files. Cyberpunk mods mostly clash in three other places: `.archive` files replacing the same game resource, TweakXL files setting the same TweakDB value, and redscript mods replacing the same function. The resolver reports all three in MO2's problems indicator and under **Tools > mo2linttricks > Conflict resolver**. For TweakXL it writes an xEdit-style patch mod. For archives it can switch the Cyberpunk plugin to MO2's mod priority.
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

Each sync is one commit, made through GitHub's Git Data API, so no git is needed inside Wine. If nothing changed, no commit is made. Commit messages list what changed (installed, removed, updated, downloaded, toggled, reordered); the first sync of a session compares against what's on GitHub.

Nexus's temporary, signed download links aren't stored, because they embed your Nexus user id and expire within a day. The mod and file IDs are kept instead.

Repo layout. Only these paths are managed; anything else you add (such as a `NOTES.md`) is left alone.

| Path | Contents |
|:--|:--|
| `README.md` | generated: a table of every mod with version, its Nexus file link or source link, and the profiles it's enabled in, plus a list of all Nexus links |
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

## Conflict resolver

### Archives

The `.archive` index reader is a Python port of [rfuzzo/red4lib](https://github.com/rfuzzo/red4lib) (MIT). On the 58 archives in a real modlist it produced the same file lists as red4lib itself. Only headers and file tables are read, so a scan takes milliseconds.

Load order follows what the game will actually use:

1. If the Cyberpunk plugin's `enforce_archive_load_order` is on: MO2's mod priority (reversed if `reverse_archive_load_order` is on), because the plugin rewrites `archive/pc/mod/modlist.txt` from it before every launch.
2. Otherwise: the virtual `archive/pc/mod/modlist.txt`, then the remaining archives by file name.
3. Otherwise: by file name.

The first archive in load order wins a contested file.

File names come from each archive's own name list (when it isn't Kraken-compressed), and on request from CDPR's resource hash list. That list is 30 MB, downloaded from red4lib at a pinned commit and checked against a SHA-256 hash. Only the hashes you need are looked up, and the results are cached in MO2's plugin data folder. Names that can't be resolved are shown as hashes.

Contested files whose content is byte-for-byte identical in every archive (common when related mods each ship the same shared file) are detected and don't count as conflicts. The index's own SHA-1 field can't be used for this, because WolvenKit writes one constant value for every file, so the stored data is compared instead.

Turning on "Use MO2 mod priority" sets the Cyberpunk plugin's `enforce_archive_load_order` and `reverse_archive_load_order`. From then on, the mod higher in MO2's list wins, the same as for loose files. Before changing anything it previews which contested files would change winner.

Notifications: "fully overridden archive" (every file loses, so the archive has no effect) always appears. "Any conflicts" can be turned off with `notify_conflicts`.

### TweakXL values

TweakXL (see `TweakImporter.cpp`) reads every `r6/tweaks` file. Files whose name starts with `_#$!` are read first and those starting with `^` last. Within each of those groups, files are read in directory order, which isn't defined, and a later value replaces an earlier one. So two mods setting the same property differently often leave the outcome arbitrary.

List operations (`!append`, `!remove`, `+=`, ...) combine and aren't conflicts. Neither are values that are equal everywhere: `1` and `1.0` count as equal.

The patch is the mod **mo2linttricks Patch**; keep it last. It contains `r6/tweaks/mo2linttricks/^mo2linttricks_patch.yaml`, which TweakXL reads after every normal file. For each contested value, the resolver writes the value from the mod highest in MO2's list, unless you pick another mod's value, a custom value, or "leave to TweakXL". Your choices are stored in the patch mod's `mo2linttricks-decisions.json`, so the patch can be regenerated after mods change. A notification appears when the patch is missing, out of date, or not last.

YAML and `.tweak` files are both parsed. Package-relative names follow TweakXL's `RedReader`. Only simple `.tweak` literals can be copied into the patch; complex ones are shown but can't be chosen. PyYAML 6.0.2 (pure Python, MIT) is vendored in `core/_vendor`.

### redscript

The resolver reports functions that several mods replace with `@replaceMethod` or `@replaceGlobal`; only one replacement runs. Byte-identical replacements are marked harmless. `@wrapMethod` combines with other mods, so it isn't listed.

## Development

```bash
for t in tests/test_*.py; do python3 -I $t; done   # core logic, no MO2 needed
(cd helper && go test ./...)
(cd plugin && mypy --config-file ../mypy.ini mo2linttricks)   # with mobase-stubs 2.5.2 + PyQt6
```

`plugin/mo2linttricks/core` has no MO2 or Qt imports. MO2 2.5 bundles Python 3.12, so keep the code 3.12-compatible.
