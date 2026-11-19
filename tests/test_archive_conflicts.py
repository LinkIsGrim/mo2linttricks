import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "mo2linttricks"))

from core import archive_conflicts as ac  # noqa: E402
from core import red4archive as ra  # noqa: E402


def make_archive(path: Path, names: list[str], lxrs: bool = True, compressed_lxrs: bool = False) -> None:
    """Write a minimal but structurally valid RDAR archive (header, LXRS, index, entries)."""
    hashes = [ra.fnv1a64(n) for n in names]
    custom = b""
    if lxrs:
        blob = b"".join(n.encode() + b"\0" for n in names)
        zsize = len(blob) - 1 if compressed_lxrs else len(blob)
        custom = struct.pack("<IIIIi", ra.LXRS_MAGIC, 1, len(blob), zsize, len(names)) + blob[:zsize]
    index_pos = ra.HEADER_EXTENDED_SIZE + len(custom)
    entries = b"".join(ra.ENTRY.pack(h, 0, 1, 0, 1, 0, 0, b"\0" * 20) for h in hashes)
    index = ra.INDEX.pack(8, len(entries), 0, len(hashes), 0, 0) + entries
    header = ra.HEADER.pack(ra.HEADER_MAGIC, 12, index_pos, len(index), 0, 0, index_pos + len(index))
    buf = io.BytesIO()
    buf.write(header + struct.pack("<I", len(custom)))
    buf.write(b"\0" * (ra.HEADER_EXTENDED_SIZE - buf.tell()))
    buf.write(custom)
    buf.write(index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


class ArchiveReadTests(unittest.TestCase):
    def test_read_index_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.archive"
            make_archive(p, [r"base\a.ent", r"mod\b.mesh"])
            idx = ra.read_index(p)
            self.assertEqual(idx.hashes, sorted([ra.fnv1a64(r"base\a.ent"), ra.fnv1a64(r"mod\b.mesh")]))
            self.assertEqual(idx.names[ra.fnv1a64(r"mod\b.mesh")], r"mod\b.mesh")
            make_archive(p, [r"x\y"], compressed_lxrs=True)
            idx = ra.read_index(p)
            self.assertTrue(idx.names_compressed)
            self.assertEqual(idx.names, {})

    def test_not_an_archive(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.archive"
            p.write_bytes(b"nope" * 20)
            with self.assertRaises(ra.ArchiveError):
                ra.read_index(p)

    def test_fnv_matches_cdpr_hash_list(self):
        self.assertEqual(ra.fnv1a64(r"base\anim_cooked.cookedanims"), 17138494147559456028)


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.game, self.mods, self.ow = r / "game", r / "mods", r / "overwrite"
        make_archive(self.mods / "Gun" / "archive/pc/mod/zzGun.archive", [r"base\gun.ent", r"base\shared.mesh"])
        make_archive(self.mods / "Patch" / "archive/pc/mod/aaPatch.archive", [r"base\shared.mesh"])
        make_archive(self.mods / "Other" / "archive/pc/mod/mmOther.archive", [r"base\other.ent"])
        # same file name in two mods: the higher-priority mod's copy is the visible one
        make_archive(self.mods / "Old" / "archive/pc/mod/dup.archive", [r"base\shared.mesh"])
        make_archive(self.mods / "New" / "archive/pc/mod/dup.archive", [r"base\new.ent"])
        self.layers = [("<game>", self.game)] + [(m, self.mods / m) for m in ("Gun", "Patch", "Other", "Old", "New")] + [("<overwrite>", self.ow)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_vfs_merge(self):
        arch = {a.name: a.mod for a in ac.visible_archives(self.layers)}
        self.assertEqual(arch, {"zzGun.archive": "Gun", "aaPatch.archive": "Patch", "mmOther.archive": "Other", "dup.archive": "New"})

    def test_alphabetical_first_wins(self):
        order = ac.load_order(ac.visible_archives(self.layers), None)
        self.assertEqual([a.name for a in order], ["aaPatch.archive", "dup.archive", "mmOther.archive", "zzGun.archive"])
        rep = ac.compute(order)
        shared = ra.fnv1a64(r"base\shared.mesh")
        self.assertEqual([order[i].name for i in rep.conflicts[shared]], ["aaPatch.archive", "zzGun.archive"])
        by = {r.source.name: r for r in rep.order}
        self.assertEqual(by["aaPatch.archive"].wins, [shared])
        self.assertEqual(by["zzGun.archive"].loses, [shared])
        self.assertEqual(rep.name(shared), r"base\shared.mesh")
        self.assertEqual(len(rep.conflicting_archives), 2)

    def test_modlist_txt_order(self):
        order = ac.load_order(ac.visible_archives(self.layers), ["zzGun.archive", "missing.archive"])
        self.assertEqual(order[0].name, "zzGun.archive")
        rep = ac.compute(order)
        self.assertEqual({r.source.name: len(r.wins) for r in rep.order}["zzGun.archive"], 1)

    def test_enforced_modlist(self):
        mod_layers = [(m, self.mods / m) for m in ("Gun", "Patch")]
        self.assertEqual(ac.enforced_modlist(mod_layers, False), ["zzGun.archive", "aaPatch.archive"])
        self.assertEqual(ac.enforced_modlist(mod_layers, True), ["aaPatch.archive", "zzGun.archive"])

    def test_broken_archive_reported(self):
        bad = self.mods / "Bad" / "archive/pc/mod/bad.archive"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"garbage")
        rep = ac.compute(ac.load_order(ac.visible_archives(self.layers + [("Bad", self.mods / "Bad")]), None))
        self.assertTrue(next(r for r in rep.order if r.source.name == "bad.archive").error)

    def test_resolve_names(self):
        db = Path(self.tmp.name) / "db.zip"
        with zipfile.ZipFile(db, "w") as z:
            z.writestr("metadata-resources.csv", f"base\\x.ent,{ra.fnv1a64('base\\\\x.ent')}\nbase\\y.ent,5\n")
        self.assertEqual(ac.resolve_names(db, {5}), {5: "base\\y.ent"})


if __name__ == "__main__":
    unittest.main()
