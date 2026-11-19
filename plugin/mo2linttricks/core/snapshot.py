"""Turn an MO2 instance on disk into the set of files stored in the GitHub repo.

Repo layout (everything here is regenerated on every sync; the repo is a mirror):

    mo2linttricks.json              instance/game info + layout version
    mods.json                       one entry per mod: source (nexus/url/manual/...), ids, files
    mods/<mod>/meta.ini             MO2's own metadata (minus the huge nexusDescription)
    downloads/<archive>.meta        MO2's download metadata (Nexus mod/file ids, urls)
    profiles/<profile>/modlist.json load order (lowest priority first) + enabled state
    profiles/<profile>/<file>       raw profile files: modlist.txt, plugins.txt, loadorder.txt, *.ini ...

The snapshot contains no timestamps, so an unchanged instance produces an
identical snapshot and no commit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .inifile import as_bool, as_int, read_ini  # noqa: F401

LAYOUT_VERSION = 1

# Top-level paths this tool owns in the repo. Anything else (e.g. a README you
# write yourself, CI files) is left alone by sync.
MANAGED_PREFIXES = ("README.md", "rules.json", "mo2linttricks.json", "mods.json", "mods/", "downloads/", "profiles/")

PROFILE_FILE_SUFFIXES = (".txt", ".ini")
PROFILE_SKIP = {"saves"}
MAX_PROFILE_FILE = 2 * 1024 * 1024


@dataclass
class InstanceInfo:
    mods_dir: Path
    profiles_dir: Path
    downloads_dir: Path
    game_short_name: str
    game_name: str = ""
    game_nexus_name: str = ""
    mo2_version: str = ""
    include_downloads: bool = True
    user_rules: Path | None = None  # the instance's own load-order rules (mo2linttricks-rules.json)


@dataclass
class Snapshot:
    files: dict[str, bytes] = field(default_factory=dict)

    def add_json(self, path: str, data: object) -> None:
        self.files[path] = (json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n").encode()


def parse_modlist(text: str) -> list[dict]:
    """Parse MO2 modlist.txt. File order is highest priority first; we return lowest first."""
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        flag, name = line[0], line[1:]
        if flag not in "+-*" or not name:
            continue
        if flag == "*":
            kind = "foreign"  # unmanaged: DLC / files already in the game folder
        elif name.endswith("_separator"):
            kind = "separator"
        else:
            kind = "mod"
        entries.append({"name": name, "enabled": flag in "+*", "kind": kind})
    entries.reverse()
    for i, e in enumerate(entries):
        e["priority"] = i
    return entries


def _is_signed_cdn_url(url: str) -> bool:
    # Nexus hands out short-lived, signed CDN links that embed the account's user id.
    # They expire within a day, so they're useless for restore and shouldn't be stored.
    return "nexus-cdn.com" in url or ("expires=" in url and "user_id=" in url)


def _strip_download_meta(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        if line.startswith("url=") and _is_signed_cdn_url(line):
            continue
        out.append(line)
    return "".join(out)


def _strip_meta(text: str) -> str:
    # nexusDescription is the whole HTML description page on one line; it bloats
    # the repo and churns whenever Nexus edits the page.
    return "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith("nexusDescription=")
    )


def describe_mod(name: str, meta: dict[str, dict[str, str]], download_meta: dict[str, dict[str, str]] | None) -> dict:
    g = meta.get("General", {})
    files = meta.get("installedFiles", {})
    file_ids = sorted(
        {as_int(v) for k, v in files.items() if k.endswith("fileid") and as_int(v) > 0}
    )
    dl = (download_meta or {}).get("General", {})
    if not file_ids and as_int(dl.get("fileID")) > 0:
        file_ids = [as_int(dl.get("fileID"))]
    modid = as_int(g.get("modid")) or as_int(dl.get("modID"))
    url = g.get("url", "") if as_bool(g.get("hasCustomURL")) else ""
    download_url = dl.get("url", "")
    if _is_signed_cdn_url(download_url):
        download_url = ""
    repository = g.get("repository", "") or dl.get("repository", "")

    if name.endswith("_separator"):
        source = "separator"
    elif modid > 0 and repository in ("", "Nexus"):
        source = "nexus"
    elif url or download_url:
        source = "url"
    else:
        source = "manual"

    out = {
        "source": source,
        "version": g.get("version", ""),
        "gameName": g.get("gameName", "") or dl.get("gameName", ""),
        "modid": modid,
        "fileids": file_ids,
        "installationFile": g.get("installationFile", ""),
        "repository": repository,
        "url": url,
        "downloadUrl": download_url,
        "nexusFileName": dl.get("name", ""),
        "categories": g.get("category", ""),
        "comments": g.get("comments", ""),
    }
    return {k: v for k, v in out.items() if v not in ("", [], 0) or k in ("source",)}


def assign_merged_files(
    mod_metas: dict[str, dict[str, dict[str, str]]],
    downloads: dict[str, dict[str, dict[str, str]]],
    game: str,
) -> dict[str, list[dict]]:
    """Every Nexus file installed into each mod, including files MERGED into an existing mod.

    MO2 only keeps the last installed file in a mod's meta.ini, so merged parts (main file +
    optional add-ons + patches) are recovered from downloads/*.meta (installed=true):
      1. a file listed in a mod's meta.ini belongs to that mod;
      2. otherwise to the only MO2 mod with that Nexus mod id;
      3. otherwise to the mod named like the Nexus mod (modName), else the first such mod;
    duplicates (same file id) collapse, and older versions of the same named file are dropped.
    Order = install order guess: main files, then optional ones, the meta.ini file last.
    """
    by_modid: dict[int, list[str]] = {}
    claimed: dict[int, str] = {}
    last_file: dict[str, int] = {}
    for name, meta in mod_metas.items():
        g = meta.get("General", {})
        mid = as_int(g.get("modid"))
        if mid <= 0:
            continue
        by_modid.setdefault(mid, []).append(name)
        for k, v in meta.get("installedFiles", {}).items():
            if k.endswith("fileid") and as_int(v) > 0:
                claimed[as_int(v)] = name
                last_file[name] = as_int(v)

    files: dict[str, dict[int, dict]] = {}
    for archive, dl in downloads.items():
        d = dl.get("General", {})
        if not as_bool(d.get("installed")) or as_bool(d.get("uninstalled")):
            continue
        if game and d.get("gameName") and d.get("gameName", "").lower() != game.lower():
            continue
        mid, fid = as_int(d.get("modID")), as_int(d.get("fileID"))
        if mid <= 0 or fid <= 0:
            continue
        owners = by_modid.get(mid, [])
        if fid in claimed:
            owner = claimed[fid]
        elif len(owners) == 1:
            owner = owners[0]
        elif owners:
            owner = next((o for o in owners if o == d.get("modName")), sorted(owners)[0])
        else:
            continue
        files.setdefault(owner, {})[fid] = {
            "fileid": fid,
            "name": d.get("name", ""),
            "version": d.get("version", ""),
            "category": as_int(d.get("fileCategory")),
            "archive": archive,
        }

    out: dict[str, list[dict]] = {}
    for owner, by_fid in files.items():
        newest: dict[str, dict] = {}
        for f in sorted(by_fid.values(), key=lambda f: f["fileid"]):
            newest[f["name"] or str(f["fileid"])] = f  # same file title: keep the newest upload
        last = last_file.get(owner)
        ordered = sorted(newest.values(), key=lambda f: (f["fileid"] == last, f["category"] != 1, f["fileid"]))
        out[owner] = ordered
    return out


def build(info: InstanceInfo) -> Snapshot:
    snap = Snapshot()

    # downloads/*.meta, indexed by archive file name for matching mods to their source
    downloads: dict[str, dict[str, dict[str, str]]] = {}
    if info.downloads_dir.is_dir():
        for meta_path in sorted(info.downloads_dir.glob("*.meta")):
            archive = meta_path.name[: -len(".meta")]
            # "removed" downloads are only hidden in MO2's list; keep them, they may be
            # the only record of a mod's Nexus ids.
            downloads[archive] = read_ini(meta_path)
            if info.include_downloads:
                text = meta_path.read_text(encoding="utf-8", errors="replace")
                snap.files[f"downloads/{meta_path.name}"] = _strip_download_meta(text).encode()

    by_lower = {k.lower(): v for k, v in downloads.items()}
    mods: dict[str, dict] = {}
    metas: dict[str, dict[str, dict[str, str]]] = {}
    if info.mods_dir.is_dir():
        for mod_dir in sorted(p for p in info.mods_dir.iterdir() if p.is_dir()):
            meta_path = mod_dir / "meta.ini"
            meta = read_ini(meta_path) if meta_path.is_file() else {}
            metas[mod_dir.name] = meta
            inst = meta.get("General", {}).get("installationFile", "")
            dl = by_lower.get(Path(inst.replace("\\", "/")).name.lower()) if inst else None
            mods[mod_dir.name] = describe_mod(mod_dir.name, meta, dl)
            if meta_path.is_file():
                text = meta_path.read_text(encoding="utf-8", errors="replace")
                snap.files[f"mods/{mod_dir.name}/meta.ini"] = _strip_meta(text).encode()
    for name, files in assign_merged_files(metas, downloads, info.game_short_name).items():
        m = mods.get(name)
        if m is None:
            continue
        m["files"] = files
        m["fileids"] = sorted({*m.get("fileids", []), *(f["fileid"] for f in files)})
        if m.get("source") in ("manual", "url") and m.get("modid"):
            m["source"] = "nexus"
    snap.add_json("mods.json", mods)

    profiles = []
    enabled_in: dict[str, list[str]] = {}
    if info.profiles_dir.is_dir():
        for prof in sorted(p for p in info.profiles_dir.iterdir() if p.is_dir()):
            profiles.append(prof.name)
            for f in sorted(prof.iterdir()):
                if f.is_file() and f.suffix.lower() in PROFILE_FILE_SUFFIXES and f.stat().st_size <= MAX_PROFILE_FILE:
                    snap.files[f"profiles/{prof.name}/{f.name}"] = f.read_bytes()
            ml = prof / "modlist.txt"
            if ml.is_file():
                entries = parse_modlist(ml.read_text(encoding="utf-8", errors="replace"))
                snap.add_json(f"profiles/{prof.name}/modlist.json", entries)
                for e in entries:
                    if e["enabled"] and e["kind"] == "mod":
                        enabled_in.setdefault(e["name"], []).append(prof.name)

    snap.files["README.md"] = render_readme(info, mods, profiles, enabled_in).encode()
    if info.user_rules and info.user_rules.is_file():
        snap.files["rules.json"] = info.user_rules.read_bytes()
    snap.add_json(
        "mo2linttricks.json",
        {
            "layout": LAYOUT_VERSION,
            "game": info.game_short_name,
            "gameName": info.game_name,
            "gameNexusName": info.game_nexus_name,
            "mo2Version": info.mo2_version,
            "profiles": profiles,
        },
    )
    return snap


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def nexus_url(game_nexus_name: str, modid: int, fileids: list[int] | None = None) -> str:
    url = f"https://www.nexusmods.com/{game_nexus_name}/mods/{modid}"
    return url + (f"?tab=files&file_id={fileids[-1]}" if fileids else "")


def render_readme(info: InstanceInfo, mods: dict[str, dict], profiles: list[str], enabled_in: dict[str, list[str]]) -> str:
    title = info.game_name or info.game_short_name
    real = {n: m for n, m in mods.items() if m.get("source") != "separator"}
    out = [
        f"# {title} modlist",
        "",
        "Generated by [mo2linttricks](https://github.com/LinkIsGrim/mo2linttricks) on every sync; edits to this file are overwritten.",
        "",
        f"{len(real)} mods, profiles: {', '.join(profiles) or 'none'}.",
        "",
        "| Mod | Version | Source | Enabled in |",
        "|:--|:--|:--|:--|",
    ]
    for name in sorted(real, key=str.lower):
        m = real[name]
        src = m.get("source")
        if src == "nexus" and info.game_nexus_name:
            files = m.get("files") or []
            if len(files) > 1:  # merged mod: one link per Nexus file
                link = "<br>".join(
                    f"[{_md(f.get('name') or f['fileid'])}]({nexus_url(info.game_nexus_name, m['modid'], [f['fileid']])})" for f in files
                )
            else:
                link = f"[Nexus {m['modid']}]({nexus_url(info.game_nexus_name, m['modid'], m.get('fileids'))})"
        elif m.get("url") or m.get("downloadUrl"):
            u = m.get("url") or m.get("downloadUrl")
            link = f"[link]({u})"
        else:
            link = "manual"
        out.append(f"| {_md(name)} | {_md(m.get('version', ''))} | {link} | {_md(', '.join(enabled_in.get(name, [])))} |")
    nexus = []
    for n, m in sorted(real.items(), key=lambda kv: kv[0].lower()):
        if m.get("source") != "nexus" or not info.game_nexus_name:
            continue
        files = m.get("files") or []
        if len(files) > 1:
            for f in files:
                nexus.append(f"- {_md(n)} / {_md(f.get('name') or f['fileid'])}: {nexus_url(info.game_nexus_name, m['modid'], [f['fileid']])}")
        else:
            nexus.append(f"- {_md(n)}: {nexus_url(info.game_nexus_name, m['modid'], m.get('fileids'))}")
    if nexus:
        out += ["", "## Nexus links", "", *nexus]
    return "\n".join(out) + "\n"


def is_managed(path: str) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in MANAGED_PREFIXES)
