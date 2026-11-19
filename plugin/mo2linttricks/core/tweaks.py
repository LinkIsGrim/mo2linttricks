"""TweakXL value conflicts between mods, and the xEdit-style patch that resolves them.

How TweakXL applies files (psiberx/cp2077-tweak-xl, TweakImporter.cpp):
  * every *.yaml/*.yml/*.tweak under r6/tweaks is read; files whose NAME starts with one
    of `_#$!` are read first, names starting with `^` last, everything else in between;
  * within a group the order is the directory listing order, i.e. not defined;
  * a later value for the same record property replaces an earlier one, while list
    operations (`!append`, `!remove`, `+=`, ...) from every file are combined.

So two mods setting the same property to different values is a real conflict whose
outcome is often arbitrary. The patch is one file, `^mo2linttricks_patch.yaml`, read
after every normal file, that sets each contested property to the chosen value. By
default that is the value of the mod with the highest MO2 priority.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._vendor import yaml

FIRST_MARKERS = "_#$!"
LAST_MARKERS = "^"
PATCH_FILE = "^mo2linttricks_patch.yaml"
PATCH_DIR = "r6/tweaks/mo2linttricks"
DECISIONS_FILE = "mo2linttricks-decisions.json"


def file_group(path: Path) -> int:
    c = path.name[:1]
    return 0 if c and c in FIRST_MARKERS else 2 if c and c in LAST_MARKERS else 1


# --- YAML -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tagged:
    """A TweakXL list operation such as `!append Items.Foo`; kept so it can round-trip."""

    tag: str
    value: Any


class _Loader(yaml.SafeLoader):
    pass


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Tagged:
    if isinstance(node, yaml.ScalarNode):
        v: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        v = loader.construct_sequence(node, deep=True)
    else:
        v = loader.construct_mapping(node, deep=True)  # type: ignore[arg-type]
    return Tagged("!" + suffix, v)


_Loader.add_multi_constructor("!", _tagged)


class _Dumper(yaml.SafeDumper):
    pass


def _repr_tagged(dumper: yaml.SafeDumper, t: Tagged) -> yaml.Node:
    if isinstance(t.value, list):
        return dumper.represent_sequence(t.tag, t.value)
    if isinstance(t.value, dict):
        return dumper.represent_mapping(t.tag, t.value)
    return dumper.represent_scalar(t.tag, str(t.value))


_Dumper.add_representer(Tagged, _repr_tagged)


def _has_tag(v: Any) -> bool:
    if isinstance(v, Tagged):
        return True
    if isinstance(v, list):
        return any(_has_tag(x) for x in v)
    return False


def dump_value(v: Any) -> str:
    return yaml.dump(v, Dumper=_Dumper, default_flow_style=True, sort_keys=False, width=1 << 30).strip().removesuffix("...").strip()


@dataclass
class TweakValue:
    mod: str
    file: str  # path relative to the mod folder
    group: int
    value: Any  # Python value (YAML); for .tweak files the raw text
    source: str  # "yaml" | "tweak"
    patchable: bool = True  # can be written into the YAML patch as-is

    @property
    def display(self) -> str:
        text = self.value if self.source == "tweak" else dump_value(self.value)
        return " ".join(str(text).split())

    def norm(self) -> str:
        if self.source == "tweak":
            conv = convert_tweak_value(self.value)
            return _canon(conv) if conv is not None else "tweak:" + " ".join(self.value.split())
        return _canon(self.value)


def _canon(v: Any) -> str:
    """Comparable form: 1 and 1.0 are the same value to TweakDB."""

    def norm(x: Any) -> Any:
        if isinstance(x, bool):
            return x
        if isinstance(x, (int, float)):
            return float(x)
        if isinstance(x, list):
            return [norm(i) for i in x]
        if isinstance(x, dict):
            return {str(k): norm(i) for k, i in x.items()}
        if isinstance(x, Tagged):
            return {"$tag": x.tag, "v": norm(x.value)}
        return x

    return json.dumps(norm(v), sort_keys=True, default=str)


def parse_yaml(text: str) -> list[tuple[str, Any]]:
    """[(Record.property, value)] for plain value assignments; list operations are skipped."""
    out: list[tuple[str, Any]] = []
    # TweakXL's parser (yaml-cpp) tolerates tabs, e.g. before trailing comments; PyYAML doesn't.
    data = yaml.load(text.replace("\t", "    "), Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    if not isinstance(data, dict):
        return out
    for key, val in data.items():
        key = str(key)
        if isinstance(val, dict):
            for prop, pv in val.items():
                prop = str(prop)
                if prop in ("$type", "$instances", "$props") or _has_tag(pv):
                    continue
                out.append((f"{key}.{prop}", pv))
        elif "." in key and not _has_tag(val):
            out.append((key, val))
    return out


# --- .tweak (TweakDB script) ---------------------------------------------------------------

_NUM = re.compile(r"^-?\d+(\.\d+)?f?$")


def convert_tweak_value(raw: str) -> Any:
    """Simple .tweak literals -> YAML value; None when it can't be converted safely."""
    s = raw.strip()
    if _NUM.match(s):
        s = s.rstrip("f")
        return float(s) if "." in s else int(s)
    if s in ("true", "false"):
        return s == "true"
    m = re.fullmatch(r'[tnrl]?"((?:[^"\\]|\\.)*)"', s)
    if m:
        return m.group(1)
    return None


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def parse_tweak(text: str) -> list[tuple[str, str]]:
    """[(Record.property, raw value)] from TweakDB script; `+=`/`-=` (list ops) are skipped."""
    text = _strip_comments(text)
    out: list[tuple[str, str]] = []
    package = ""
    i, n = 0, len(text)
    record: str | None = None
    while i < n:
        if text[i].isspace() or text[i] == ";":
            i += 1
            continue
        m = re.compile(r"package\s+([\w.]+)").match(text, i)
        if m and record is None:
            package = m.group(1)
            i = m.end()
            continue
        m = re.compile(r"using\s+[^\n;]*").match(text, i)
        if m and record is None:
            i = m.end()
            continue
        if text[i] == "}":
            record = None
            i += 1
            continue
        m = re.compile(r"([\w.]+)\s*(?::\s*[\w.]+\s*)?\{").match(text, i)
        if m and record is None:
            record = ".".join(filter(None, [package, m.group(1)]))
            i = m.end()
            continue
        m = re.compile(r"([\w.]+)\s*(\+=|-=|=)").match(text, i)
        if not m:
            i += 1
            continue
        name, op = m.group(1), m.group(2)
        j, depth = m.end(), 0
        while j < n:
            c = text[j]
            if c in "[{(":
                depth += 1
            elif c in "]})":
                depth -= 1
            elif c == ";" and depth <= 0:
                break
            elif c == "\n" and depth <= 0 and record is not None and text[m.end():j].strip():
                break
            j += 1
        raw = text[m.end():j].strip()
        i = j + 1
        if op != "=":
            continue
        if record is not None:
            out.append((f"{record}.{name}", raw))
        elif package:  # TweakXL (RedReader.cpp) ignores top-level flats outside a package
            out.append((f"{package}.{name}", raw))
    return out


# --- scanning and conflicts ------------------------------------------------------------------


@dataclass
class TweakConflict:
    key: str  # Record.property
    values: list[TweakValue]  # lowest MO2 priority first

    @property
    def identical(self) -> bool:
        return len({v.norm() for v in self.values}) == 1

    def runtime_winner(self) -> TweakValue | None:
        """What the game uses today without a patch, or None if it depends on directory order."""
        top = max(v.group for v in self.values)
        in_top = [v for v in self.values if v.group == top]
        if len({v.norm() for v in in_top}) == 1:
            return in_top[-1]
        return None

    def default_choice(self) -> TweakValue | None:
        """Highest MO2 priority value that can be written to the patch."""
        for v in reversed(self.values):
            if v.patchable:
                return v
        return None


@dataclass
class TweakScan:
    conflicts: list[TweakConflict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def real(self) -> list[TweakConflict]:
        return [c for c in self.conflicts if not c.identical]


ParseCache = dict  # str(path) -> (mtime_ns, size, [(key, value, source)])


def _parse_file(p: Path, cache: ParseCache | None) -> list[tuple[str, Any, str]]:
    st = p.stat()
    key = str(p)
    hit = cache.get(key) if cache is not None else None
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() == ".tweak":
        parsed: list[tuple[str, Any, str]] = [(k, raw, "tweak") for k, raw in parse_tweak(text)]
    else:
        parsed = [(k, v, "yaml") for k, v in parse_yaml(text)]
    if cache is not None:
        cache[key] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def _from_json(v: Any) -> Any:
    """Values from the mo2lt-scan helper: {"$tag": t, "v": x} back to Tagged."""
    if isinstance(v, dict):
        if set(v) == {"$tag", "v"}:
            return Tagged(v["$tag"], _from_json(v["v"]))
        return {k: _from_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_from_json(x) for x in v]
    return v


def prefill_with_helper(mods_by_priority: list[tuple[str, Path]], cache: ParseCache, helper: Path, run) -> int:
    """Parse new/changed YAML files with the out-of-process Go helper and store them in `cache`.

    `run(cmd, input_bytes) -> stdout bytes` is injected (subprocess in MO2, fakes in tests).
    Returns the number of files parsed. Files the helper can't parse are left to Python.
    """
    stale_roots: list[str] = []
    for _mod, root in mods_by_priority:
        tweaks_dir = root / "r6" / "tweaks"
        if not tweaks_dir.is_dir():
            continue
        for p in tweaks_dir.rglob("*"):
            if p.suffix.lower() in (".yaml", ".yml") and p.is_file():
                st = p.stat()
                hit = cache.get(str(p))
                if not hit or hit[0] != st.st_mtime_ns or hit[1] != st.st_size:
                    stale_roots.append(str(root))
                    break
    if not stale_roots:
        return 0
    out = json.loads(run([str(helper)], json.dumps({"roots": stale_roots}).encode()))
    n = 0
    for f in out.get("files", []):
        if f.get("error"):
            continue
        p = Path(f["path"])
        try:
            st = p.stat()
        except OSError:
            continue
        cache[str(p)] = (st.st_mtime_ns, st.st_size, [(k, _from_json(v), "yaml") for k, v in f["entries"]])
        n += 1
    return n


def scan(mods_by_priority: list[tuple[str, Path]], cache: ParseCache | None = None) -> TweakScan:
    """mods_by_priority: [(mod name, mod folder)] lowest priority first (exclude the patch mod).

    `cache` (optional, mutated) maps file path -> parse result, keyed by mtime and size, so a
    rescan only parses files that changed: parsing ~1000 YAML files in pure Python takes ~25 s.
    """
    by_key: dict[str, list[TweakValue]] = {}
    result = TweakScan()
    for mod, root in mods_by_priority:
        tweaks = root / "r6" / "tweaks"
        if not tweaks.is_dir():
            continue
        for p in sorted(tweaks.rglob("*")):
            suffix = p.suffix.lower()
            if not p.is_file() or suffix not in (".yaml", ".yml", ".tweak"):
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            try:
                for key, val, source in _parse_file(p, cache):
                    patchable = source == "yaml" or convert_tweak_value(val) is not None
                    by_key.setdefault(key, []).append(TweakValue(mod, rel, file_group(p), val, source, patchable))
            except Exception as e:  # a broken file shouldn't hide every other conflict
                result.errors.append(f"{mod}: {rel}: {e}")
    for key, vals in sorted(by_key.items()):
        if len({v.mod for v in vals}) > 1:
            result.conflicts.append(TweakConflict(key, vals))
    return result


# --- decisions and patch --------------------------------------------------------------------

# decisions: {"Record.prop": {"mod": "<mod name>"} | {"custom": "<yaml value>"} | {"skip": true}}


def load_decisions(patch_root: Path) -> dict[str, dict]:
    try:
        return json.loads((patch_root / DECISIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def resolve(conflict: TweakConflict, decision: dict | None) -> tuple[Any, str] | None:
    """(value, explanation) to write for one conflict, or None to leave it to TweakXL."""
    if decision and decision.get("skip"):
        return None
    if decision and "custom" in decision:
        return yaml.load(str(decision["custom"]), Loader=_Loader), "custom value"  # noqa: S506
    chosen = None
    if decision and decision.get("mod"):
        chosen = next((v for v in reversed(conflict.values) if v.mod == decision["mod"] and v.patchable), None)
    if chosen is None:
        chosen = conflict.default_choice()
        how = "highest MO2 priority"
    else:
        how = "chosen"
    if chosen is None:
        return None
    value = convert_tweak_value(chosen.value) if chosen.source == "tweak" else chosen.value
    return value, f"{how}: {chosen.mod}"


def build_patch(scan_result: TweakScan, decisions: dict[str, dict]) -> tuple[str, int]:
    """YAML text for the patch file and the number of values it sets."""
    lines = [
        "# Generated by mo2linttricks (Tools > mo2linttricks > Conflict resolver). Do not edit:",
        "# choices are kept in " + DECISIONS_FILE + " and this file is rewritten from them.",
        "# The ^ file name makes TweakXL apply it after every normal tweak file.",
        "",
    ]
    count = 0
    for c in scan_result.real:
        r = resolve(c, decisions.get(c.key))
        if r is None:
            continue
        value, how = r
        others = ", ".join(f"{v.mod}={v.display}" for v in c.values)
        lines.append(f"# {how} | {others}"[:400])
        lines.append(yaml.dump({c.key: value}, Dumper=_Dumper, default_flow_style=False, sort_keys=False, width=1 << 30).rstrip())
        count += 1
    return "\n".join(lines) + "\n", count


def write_patch(patch_root: Path, scan_result: TweakScan, decisions: dict[str, dict]) -> int:
    text, count = build_patch(scan_result, decisions)
    target = patch_root / PATCH_DIR / PATCH_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    (patch_root / DECISIONS_FILE).write_text(json.dumps(decisions, indent=2, sort_keys=True), encoding="utf-8")
    return count


def patched_keys(patch_root: Path) -> set[str]:
    p = patch_root / PATCH_DIR / PATCH_FILE
    if not p.is_file():
        return set()
    try:
        return {k for k, _ in parse_yaml(p.read_text(encoding="utf-8"))}
    except Exception:
        return set()
