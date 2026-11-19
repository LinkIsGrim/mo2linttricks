import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "mo2linttricks"))

from core import rules as R  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def mod(name, modid=0, files=(), enabled=True):
    return R.LocalMod(name, modid, set(files), enabled)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.rules = R.parse(
            {
                "schema": 1,
                "mods": {
                    "nexus:2": {"load_after": ["nexus:1"]},  # B after A
                    "nexus:1:11": {"load_after": ["nexus:3"]},  # A's compat file after C
                    "nexus:4": {"incompatible_with": [{"id": "nexus:3", "note": "nope"}], "requires": ["nexus:99"]},
                    "nexus:5": {"load_bottom": True},
                },
            },
            "community",
        )

    def test_specific_key_takes_the_split_out_mod(self):
        a, compat = mod("A", 1, [10]), mod("A compat", 1, [11])
        specific = {k for k in self.rules if k.count(":") == 2}
        self.assertTrue(R.matches("nexus:1", a, specific))
        self.assertFalse(R.matches("nexus:1", compat, specific))  # described by nexus:1:11 instead
        self.assertTrue(R.matches("nexus:1:11", compat, specific))

    def test_check_and_sort(self):
        order = [mod("Sep_separator"), mod("B", 2), mod("Bottom", 5), mod("C", 3), mod("A", 1, [10]), mod("A compat", 1, [11]), mod("X")]
        rep = R.check(order, self.rules)
        self.assertEqual({(e.before, e.after) for e in rep.violations}, {("A", "B")})
        self.assertEqual(rep.bottom_violations, ["Bottom"])
        new, dropped = R.sort(order, rep)
        self.assertEqual(dropped, [])
        self.assertLess(new.index("A"), new.index("B"))
        self.assertLess(new.index("C"), new.index("A compat"))
        self.assertEqual(new[-1], "Bottom")
        self.assertEqual(new[0], "Sep_separator")  # unconstrained items keep their place
        self.assertEqual(R.check([next(m for m in order if m.name == n) for n in new], self.rules).violations, [])
        self.assertEqual(set(R.moves([m.name for m in order], new)) & {"Sep_separator", "X", "C"}, set())

    def test_incompatible_missing_and_disabled(self):
        order = [mod("C", 3), mod("D", 4)]
        rep = R.check(order, self.rules)
        self.assertEqual([(a, b) for a, b, _ in rep.incompatible], [("D", "C")])
        self.assertEqual(rep.missing, [("D", "nexus:99")])
        rep = R.check([mod("C", 3), mod("D", 4, enabled=False)], self.rules)
        self.assertEqual(rep.incompatible, [])

    def test_cycle_is_broken_not_fatal(self):
        rules = R.parse({"schema": 1, "mods": {"nexus:1": {"load_after": ["nexus:2"]}, "nexus:2": {"load_after": ["nexus:1"]}}}, "community")
        order = [mod("A", 1), mod("B", 2)]
        new, dropped = R.sort(order, R.check(order, rules))
        self.assertEqual(sorted(new), ["A", "B"])
        self.assertEqual(len(dropped), 1)

    def test_user_rules_merge_and_save(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "rules.json"
            R.save_user_rule(p, "nexus:3", "load_after", "nexus:2", "C")
            R.save_user_rule(p, "nexus:3", "load_after", "nexus:2", "C")  # no duplicate
            user = R.load_file(p, "user")
            self.assertEqual(user["nexus:3"].load_after, ["nexus:2"])
            merged = R.merge(self.rules, user)
            self.assertEqual(merged["nexus:3"].origin, "user")
            merged = R.merge(self.rules, R.parse({"schema": 1, "mods": {"nexus:2": {"load_after": ["nexus:7"]}}}, "user"))
            self.assertEqual(merged["nexus:2"].load_after, ["nexus:1", "nexus:7"])

    def test_future_schema_ignored(self):
        self.assertEqual(R.parse({"schema": 99, "mods": {"nexus:1": {}}}, "community"), {})

    def test_shipped_rules_file_is_valid(self):
        data = json.loads((REPO / "rules" / "cyberpunk2077.json").read_text())
        parsed = R.parse(data, "community")
        self.assertGreater(len(parsed), 5)
        for key, r in parsed.items():
            self.assertRegex(key, r"^nexus:\d+(:\d+)?$")
            for ref in r.load_after + r.load_before + r.requires + [i["id"] for i in r.incompatible_with]:
                self.assertRegex(ref, r"^nexus:\d+(:\d+)?$")
            self.assertTrue(r.source, key)


if __name__ == "__main__":
    unittest.main()
