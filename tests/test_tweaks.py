import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "mo2linttricks"))

from core import redscript, tweaks  # noqa: E402

ECON = """Price.TechWeapon.value: 1.4\t\t\t# vanilla 1.1
Price.Arasaka.value: 1.0
Items.Gun:
  statModifiers:
    - !append Items.EconMod
QuestRewards.r1.value: 7000
"""

GUN = """Price.TechWeapon:
  $type: gamedataConstantStatModifier_Record
  value: 1.1
Price.Arasaka:
  value: 1
Items.Gun:
  statModifiers:
    - !append Items.GunMod
  magazineCapacity: 30
"""

LAL_TWEAK = """package QuestRewards
r1 : Base
{
    value = 50000;
}
"""

# top-level names are relative to the package (TweakXL RedReader.cpp: ComposeFlatName)
LAL_ITEMS_TWEAK = """package Items
Gun.magazineCapacity = 20;
Gun.tags += [ "Foo" ];
"""

LAST = "Items.Gun:\n  magazineCapacity: 25\n"


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class TweakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = Path(self.tmp.name)
        self.mods = [("Econ", r / "Econ"), ("LAL", r / "LAL"), ("Gun", r / "Gun")]
        write(r / "Econ", "r6/tweaks/econ/prices.yaml", ECON)
        write(r / "Gun", "r6/tweaks/zzz_gun/prices.yaml", GUN)
        write(r / "LAL", "r6/tweaks/lal.tweak", LAL_TWEAK)
        write(r / "LAL", "r6/tweaks/lal_items.tweak", LAL_ITEMS_TWEAK)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        return {c.key: c for c in tweaks.scan(self.mods).conflicts}

    def test_parse_formats_and_skip_list_ops(self):
        self.assertEqual(dict(tweaks.parse_yaml(GUN))["Items.Gun.magazineCapacity"], 30)
        self.assertNotIn("Items.Gun.statModifiers", dict(tweaks.parse_yaml(GUN)))  # !append merges
        parsed = dict(tweaks.parse_tweak(LAL_TWEAK))
        self.assertEqual(parsed["QuestRewards.r1.value"], "50000")
        parsed = dict(tweaks.parse_tweak(LAL_ITEMS_TWEAK))
        self.assertEqual(parsed["Items.Gun.magazineCapacity"], "20")
        self.assertNotIn("Items.Gun.tags", parsed)  # += merges
        self.assertEqual(tweaks.parse_tweak("Items.Gun.x = 1;\n"), [])  # no package: ignored by TweakXL

    def test_conflicts_identical_and_real(self):
        c = self.scan()
        self.assertTrue(c["Price.Arasaka.value"].identical)  # 1.0 == 1
        self.assertFalse(c["Price.TechWeapon.value"].identical)
        self.assertEqual({v.mod for v in c["Items.Gun.magazineCapacity"].values}, {"LAL", "Gun"})
        self.assertEqual(c["QuestRewards.r1.value"].default_choice().mod, "LAL")  # LAL above Econ
        self.assertIsNone(c["Price.TechWeapon.value"].runtime_winner())  # same group -> undefined

    def test_last_group_wins_at_runtime(self):
        write(Path(self.tmp.name) / "Econ", "r6/tweaks/^late.yaml", LAST)
        c = self.scan()["Items.Gun.magazineCapacity"]
        self.assertEqual(c.runtime_winner().mod, "Econ")
        self.assertEqual(c.default_choice().mod, "Gun")  # MO2 priority still decides the default

    def test_patch_roundtrip_and_decisions(self):
        s = tweaks.scan(self.mods)
        text, n = tweaks.build_patch(s, {})
        got = dict(tweaks.parse_yaml(text))
        self.assertEqual(n, len(s.real))
        self.assertEqual(got["Price.TechWeapon.value"], 1.1)  # Gun has the highest priority
        self.assertEqual(got["QuestRewards.r1.value"], 50000)  # converted from .tweak
        decisions = {
            "Price.TechWeapon.value": {"mod": "Econ"},
            "QuestRewards.r1.value": {"custom": "12345"},
            "Items.Gun.magazineCapacity": {"skip": True},
        }
        got = dict(tweaks.parse_yaml(tweaks.build_patch(s, decisions)[0]))
        self.assertEqual(got["Price.TechWeapon.value"], 1.4)
        self.assertEqual(got["QuestRewards.r1.value"], 12345)
        self.assertNotIn("Items.Gun.magazineCapacity", got)
        patch = Path(self.tmp.name) / "Patch"
        tweaks.write_patch(patch, s, decisions)
        self.assertEqual(tweaks.load_decisions(patch), decisions)
        self.assertTrue((patch / tweaks.PATCH_DIR / tweaks.PATCH_FILE).name.startswith("^"))
        self.assertIn("Price.TechWeapon.value", tweaks.patched_keys(patch))

    def test_unconvertible_tweak_value_not_chosen(self):
        write(Path(self.tmp.name) / "LAL", "r6/tweaks/lal2.tweak", "package Items\nGun.blueprint = Items.SomeRecord;\n")
        write(Path(self.tmp.name) / "Gun", "r6/tweaks/zzz_gun/bp.yaml", "Items.Gun.blueprint: Items.Other\n")
        c = self.scan()["Items.Gun.blueprint"]
        lal = next(v for v in c.values if v.mod == "LAL")
        self.assertFalse(lal.patchable)


class RedscriptTests(unittest.TestCase):
    def test_replacements(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            body = "@replaceMethod(Vendor)\nprivate final func RegenerateStock() -> Void {\n  if x { y(); }\n}\n"
            write(r / "A", "r6/scripts/a.reds", body)
            write(r / "B", "r6/scripts/b.reds", body)
            write(r / "C", "r6/scripts/c.reds", "@replaceMethod(Foo) func Bar() -> Void { a(); }\n@wrapMethod(Vendor) func RegenerateStock() -> Void { wrappedMethod(); }\n")
            write(r / "D", "r6/scripts/d.reds", "@replaceMethod(Foo)\npublic func Bar() -> Void { b(); }\n")
            got = {c.target: c for c in redscript.scan([(m, r / m) for m in "ABCD"])}
            self.assertTrue(got["Vendor::RegenerateStock"].identical)
            self.assertFalse(got["Foo::Bar"].identical)
            self.assertEqual(len(got["Vendor::RegenerateStock"].replacements), 2)  # wrap isn't counted


if __name__ == "__main__":
    unittest.main()
