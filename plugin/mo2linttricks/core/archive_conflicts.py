"""Archive (.archive) conflict detection for Cyberpunk 2077, as MO2 sees the game.

The rules follow rfuzzo's Archive Conflict Checker (red4-conflicts, MIT):
  * load order: entries of archive/pc/mod/modlist.txt first (in that order), then the
    remaining archives sorted by file name (byte order);
  * the FIRST archive in load order wins a file that several archives contain.
On top of that, the archives come from MO2's virtual data folder (each archive is
attributed to the mod that provides it), and the Cyberpunk plugin's
"enforce_archive_load_order" setting is honoured, since it rewrites modlist.txt
from MO2's priority before every launch.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .red4archive import ArchiveError, read_index


@dataclass
class ArchiveSource:
    name: str  # file name, e.g. "GunsensicalReloaded.archive"
    path: Path  # real path on disk
    mod: str  # MO2 mod name, "<overwrite>" or "<game>"


@dataclass
class ArchiveResult:
    source: ArchiveSource
    files: int = 0
    wins: list[int] = field(default_factory=list)
    loses: list[int] = field(default_factory=list)
    error: str = ""


@dataclass
class ConflictReport:
    order: list[ArchiveResult]  # load order, first wins
    conflicts: dict[int, list[int]]  # file hash -> indexes into order, winner first
    names: dict[int, str]  # resolved names (from archives' LXRS footers, plus name DB)
    unnamed_compressed: bool = False

    @property
    def conflicting_archives(self) -> list[ArchiveResult]:
        return [a for a in self.order if a.wins or a.loses]

    def name(self, h: int) -> str:
        return self.names.get(h, f"{h:016x}")


def visible_archives(layers: Iterable[tuple[str, Path]]) -> list[ArchiveSource]:
    """Merge archive/pc/mod across MO2 layers, lowest priority first (game, mods..., overwrite).

    Like MO2's virtual file system, a file name provided by a later layer replaces an earlier one.
    """
    seen: dict[str, ArchiveSource] = {}
    for owner, root in layers:
        folder = root / "archive" / "pc" / "mod"
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() == ".archive":
                seen[p.name.lower()] = ArchiveSource(p.name, p, owner)
    return list(seen.values())


def load_order(archives: list[ArchiveSource], modlist: list[str] | None) -> list[ArchiveSource]:
    by_name = {a.name: a for a in archives}
    rest = sorted(archives, key=lambda a: a.name.encode("utf-8"))
    if not modlist:
        return rest
    first = []
    for line in modlist:
        a = by_name.get(line.strip())
        if a and a not in first:
            first.append(a)
    return first + [a for a in rest if a not in first]


def enforced_modlist(mod_layers_by_priority: list[tuple[str, Path]], reverse: bool) -> list[str]:
    """What the Cyberpunk plugin writes to modlist.txt when enforce_archive_load_order is on."""
    layers = list(reversed(mod_layers_by_priority)) if reverse else mod_layers_by_priority
    names: list[str] = []
    for _, root in layers:
        folder = root / "archive" / "pc" / "mod"
        if folder.is_dir():
            names += [p.name for p in sorted(folder.glob("*.archive"))]
    return names


def compute(order: list[ArchiveSource]) -> ConflictReport:
    results: list[ArchiveResult] = []
    owners: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    compressed = False
    for i, src in enumerate(order):
        res = ArchiveResult(src)
        results.append(res)
        try:
            idx = read_index(src.path)
        except (OSError, ArchiveError) as e:
            res.error = str(e)
            continue
        res.files = len(idx.hashes)
        names.update(idx.names)
        compressed |= idx.names_compressed
        for h in idx.hashes:
            owners.setdefault(h, []).append(i)

    conflicts = {h: idxs for h, idxs in owners.items() if len(idxs) > 1}
    for h, idxs in conflicts.items():
        results[idxs[0]].wins.append(h)
        for j in idxs[1:]:
            results[j].loses.append(h)
    for r in results:
        r.wins.sort()
        r.loses.sort()
    return ConflictReport(results, conflicts, names, compressed)


# --- vanilla file names ------------------------------------------------------------

# red4lib's copy of CDPR's resource hash list (https://www.cyberpunk.net/en/modding-support).
# Pinned to a commit so the content can't change underneath us.
NAME_DB_URL = "https://raw.githubusercontent.com/rfuzzo/red4lib/cfa6713570e9bf859c29c05052035d38daa08d55/src/metadata-resources.zip"
NAME_DB_SHA256 = "2af3110e899211239f87c652cf9ffed73b874d288f5de53ff103f6111e429203"
NAME_DB_SIZE_MB = 30


def resolve_names(db_zip: Path, wanted: set[int]) -> dict[int, str]:
    """Stream the hash CSV (~170 MB unpacked) and keep only the hashes we need."""
    found: dict[int, str] = {}
    if not wanted:
        return found
    with zipfile.ZipFile(db_zip) as z:
        member = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(member) as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace")):
                if len(row) < 2:
                    continue
                try:
                    h = int(row[1])
                except ValueError:
                    continue
                if h in wanted:
                    found[h] = row[0]
                    if len(found) == len(wanted):
                        break
    return found
