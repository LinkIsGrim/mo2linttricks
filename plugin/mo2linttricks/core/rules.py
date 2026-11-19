"""Load-order rules (RimSort-style) for MO2 mod priority.

Rules come from a community file in the public mo2linttricks repo (downloaded on startup,
cached for offline use) plus the user's own rules, which override them. Mods are
identified by Nexus id, optionally with a file id for optional files that share their
parent's mod id (e.g. a compatibility add-on installed as its own MO2 mod):

    "nexus:23219"          any installed mod from Nexus mod 23219
    "nexus:13271:130530"   the MO2 mod that installed file 130530 of mod 13271

Semantics are in MO2 terms: "load_after": [B] means the mod must be LOWER in MO2's list
(higher priority) than B, so it wins loose files, archives (with the Cyberpunk plugin's
enforce+reverse archive order) and TweakXL patch defaults.

    {
      "schema": 1,
      "game": "cyberpunk2077",
      "mods": {
        "nexus:23219": {
          "name": "LONGER LOCKDOWN",
          "load_after": ["nexus:13271"],
          "load_before": [],
          "load_bottom": false,
          "incompatible_with": [{"id": "nexus:24604", "note": "..."}],
          "requires": ["nexus:4197"],
          "note": "...", "source": "https://..."
        }
      }
    }
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from pathlib import Path

from .inifile import as_int, read_ini

SCHEMA = 1


@dataclass
class LocalMod:
    name: str  # MO2 mod name
    modid: int = 0
    fileids: set[int] = field(default_factory=set)
    enabled: bool = True


def read_local_mod(name: str, folder: Path, enabled: bool) -> LocalMod:
    meta = read_ini(folder / "meta.ini") if (folder / "meta.ini").is_file() else {}
    g = meta.get("General", {})
    files = meta.get("installedFiles", {})
    fileids = {as_int(v) for k, v in files.items() if k.endswith("fileid") and as_int(v) > 0}
    return LocalMod(name, as_int(g.get("modid")), fileids, enabled)


@dataclass
class ModRules:
    load_after: list[str] = field(default_factory=list)
    load_before: list[str] = field(default_factory=list)
    load_bottom: bool = False
    incompatible_with: list[dict] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    name: str = ""
    note: str = ""
    source: str = ""
    origin: str = "community"  # or "user"


def _as_list(v) -> list:
    return list(v) if isinstance(v, list) else ([v] if v else [])


def parse(data: dict, origin: str) -> dict[str, ModRules]:
    if not isinstance(data, dict) or int(data.get("schema", 0)) > SCHEMA:
        return {}
    out: dict[str, ModRules] = {}
    for key, r in (data.get("mods") or {}).items():
        if not isinstance(r, dict):
            continue
        out[key] = ModRules(
            load_after=[str(x) for x in _as_list(r.get("load_after"))],
            load_before=[str(x) for x in _as_list(r.get("load_before"))],
            load_bottom=bool(r.get("load_bottom")),
            incompatible_with=[x if isinstance(x, dict) else {"id": str(x)} for x in _as_list(r.get("incompatible_with"))],
            requires=[str(x) for x in _as_list(r.get("requires"))],
            name=str(r.get("name", "")),
            note=str(r.get("note", "")),
            source=str(r.get("source", "")),
            origin=origin,
        )
    return out


def merge(community: dict[str, ModRules], user: dict[str, ModRules]) -> dict[str, ModRules]:
    """User rules extend community rules for the same key; user load_bottom/notes win."""
    out = {k: ModRules(**vars(v)) for k, v in community.items()}
    for k, u in user.items():
        if k not in out:
            out[k] = u
            continue
        c = out[k]
        c.load_after = list(dict.fromkeys(c.load_after + u.load_after))
        c.load_before = list(dict.fromkeys(c.load_before + u.load_before))
        c.incompatible_with = c.incompatible_with + [i for i in u.incompatible_with if i not in c.incompatible_with]
        c.requires = list(dict.fromkeys(c.requires + u.requires))
        c.load_bottom = u.load_bottom or c.load_bottom
        c.origin = "community+user"
    return out


def load_file(path: Path, origin: str) -> dict[str, ModRules]:
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")), origin)
    except (OSError, ValueError):
        return {}


def save_user_rule(path: Path, key: str, field_name: str, target: str, name: str = "") -> None:
    """Append one relation (load_after/load_before/incompatible_with/requires) to the user rules file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"schema": SCHEMA, "game": "cyberpunk2077", "mods": {}}
    entry = data.setdefault("mods", {}).setdefault(key, {})
    if name and not entry.get("name"):
        entry["name"] = name
    if field_name == "incompatible_with":
        lst = entry.setdefault(field_name, [])
        if not any((x.get("id") if isinstance(x, dict) else x) == target for x in lst):
            lst.append({"id": target})
    else:
        lst = entry.setdefault(field_name, [])
        if target not in lst:
            lst.append(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- matching ------------------------------------------------------------------------------


def key_for(mod: LocalMod, rules: dict[str, ModRules]) -> str:
    """The most specific rules key that describes a local mod (used when adding user rules)."""
    for f in sorted(mod.fileids):
        k = f"nexus:{mod.modid}:{f}"
        if k in rules:
            return k
    return f"nexus:{mod.modid}" if mod.modid else f"name:{mod.name}"


def matches(key: str, mod: LocalMod, specific: set[str]) -> bool:
    """Does a rules key refer to this local mod?

    "nexus:M" matches mods from Nexus mod M, except a mod that installed a file F for which
    a more specific "nexus:M:F" key exists (that mod is described by the specific key).
    """
    if key.startswith("name:"):
        return key[5:] == mod.name
    parts = key.split(":")
    if len(parts) < 2 or parts[0] != "nexus":
        return False
    try:
        mid = int(parts[1])
    except ValueError:
        return False
    if mod.modid != mid:
        return False
    if len(parts) == 3:
        try:
            return int(parts[2]) in mod.fileids
        except ValueError:
            return False
    return not any(f"nexus:{mid}:{f}" in specific for f in mod.fileids)


def resolve(key: str, mods: list[LocalMod], specific: set[str]) -> list[LocalMod]:
    return [m for m in mods if matches(key, m, specific)]


# --- checking --------------------------------------------------------------------------------


@dataclass
class Edge:
    before: str  # MO2 mod name that must be HIGHER in the list (lower priority)
    after: str  # MO2 mod name that must be LOWER in the list (wins)
    rule_key: str
    rules: ModRules


@dataclass
class Report:
    edges: list[Edge] = field(default_factory=list)
    violations: list[Edge] = field(default_factory=list)
    bottom: list[str] = field(default_factory=list)  # mods that should be at the bottom
    bottom_violations: list[str] = field(default_factory=list)
    incompatible: list[tuple[str, str, str]] = field(default_factory=list)  # (mod, other, note)
    missing: list[tuple[str, str]] = field(default_factory=list)  # (mod, required key)


def check(order: list[LocalMod], rules: dict[str, ModRules]) -> Report:
    """order: MO2 mods lowest priority first (top of MO2's list first)."""
    specific = {k for k in rules if k.count(":") == 2}
    enabled = [m for m in order if m.enabled]
    pos = {m.name: i for i, m in enumerate(order)}
    rep = Report()
    seen: set[tuple[str, str]] = set()
    for key, r in rules.items():
        subjects = resolve(key, enabled, specific)
        if not subjects:
            continue
        for s in subjects:
            for other in r.load_after:
                for o in resolve(other, enabled, specific):
                    if o.name != s.name and (o.name, s.name) not in seen:
                        seen.add((o.name, s.name))
                        rep.edges.append(Edge(o.name, s.name, key, r))
            for other in r.load_before:
                for o in resolve(other, enabled, specific):
                    if o.name != s.name and (s.name, o.name) not in seen:
                        seen.add((s.name, o.name))
                        rep.edges.append(Edge(s.name, o.name, key, r))
            if r.load_bottom:
                rep.bottom.append(s.name)
            for inc in r.incompatible_with:
                for o in resolve(str(inc.get("id", "")), enabled, specific):
                    if o.name != s.name:
                        pair = tuple(sorted((s.name, o.name)))
                        if not any(tuple(sorted(x[:2])) == pair for x in rep.incompatible):
                            rep.incompatible.append((s.name, o.name, str(inc.get("note", "") or r.note)))
            for req in r.requires:
                if not resolve(req, enabled, specific):
                    rep.missing.append((s.name, req))
    rep.violations = [e for e in rep.edges if pos[e.before] > pos[e.after]]
    if rep.bottom:
        non_bottom = [pos[m.name] for m in enabled if m.name not in rep.bottom]
        last_normal = max(non_bottom) if non_bottom else -1
        rep.bottom_violations = [b for b in rep.bottom if pos[b] < last_normal]
    return rep


def sort(order: list[LocalMod], report: Report) -> tuple[list[str], list[str]]:
    """Stable topological sort: satisfy the rules while moving as little as possible.

    Returns (new order of mod names lowest priority first, edges dropped to break cycles).
    Among mods whose constraints are met, the one that is currently highest in the list goes
    first, so unconstrained mods (and separators) keep their relative positions.
    """
    names = [m.name for m in order]
    pos = {n: i for i, n in enumerate(names)}
    bottom = set(report.bottom)
    succ: dict[str, set[str]] = {n: set() for n in names}
    indeg = {n: 0 for n in names}
    dropped: list[str] = []

    def add(a: str, b: str, label: str) -> None:
        if b in succ[a]:
            return
        # refuse an edge that closes a cycle
        stack, seen = [b], set()
        while stack:
            x = stack.pop()
            if x == a:
                dropped.append(label)
                return
            if x not in seen:
                seen.add(x)
                stack.extend(succ[x])
        succ[a].add(b)
        indeg[b] += 1

    for e in sorted(report.edges, key=lambda e: (e.rules.origin != "user", e.before, e.after)):
        add(e.before, e.after, f"{e.before} -> {e.after} ({e.rule_key})")
    for b in bottom:
        for n in names:
            if n not in bottom:
                add(n, b, f"{n} -> {b} (load_bottom)")

    heap = [(pos[n], n) for n in names if indeg[n] == 0]
    heapq.heapify(heap)
    out: list[str] = []
    while heap:
        _, n = heapq.heappop(heap)
        out.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(heap, (pos[m], m))
    return out, dropped


def moves(old: list[str], new: list[str]) -> list[str]:
    """Mods whose position relative to the others changed (for the preview)."""
    # longest increasing subsequence of old positions in new order = mods that stay put
    pos = {n: i for i, n in enumerate(old)}
    seq = [pos[n] for n in new]
    tails: list[int] = []
    prev = [-1] * len(seq)
    idx: list[int] = []
    import bisect

    for i, v in enumerate(seq):
        j = bisect.bisect_left(tails, v)
        if j == len(tails):
            tails.append(v)
            idx.append(i)
        else:
            tails[j] = v
            idx[j] = i
        prev[i] = idx[j - 1] if j else -1
    keep = set()
    k = idx[-1] if idx else -1
    while k >= 0:
        keep.add(new[k])
        k = prev[k]
    return [n for n in new if n not in keep]
