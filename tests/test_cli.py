import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planconform.cli import check, parse_diff, parse_plan, parse_report  # noqa: E402

BIN = ROOT / "bin" / "plan-conformance"

PLAN = """# Plan 007: Batch the lookup

- **Planned at**: commit `abc1234`, 2026-11-12

## Scope

**In scope** (the only files you should modify):
- `src/api.ts`
- `src/api.test.ts`

**Out of scope** (do NOT touch):
- `src/legacy.ts` — deprecated

## Done criteria

- [ ] `pnpm test` exits 0
- [ ] `pnpm typecheck` exits 0
"""

DIFF = """diff --git a/src/api.ts b/src/api.ts
index 1111111..2222222 100644
--- a/src/api.ts
+++ b/src/api.ts
@@ -1,2 +1,3 @@
 export function list() {
+  // batched
 }
diff --git a/src/api.test.ts b/src/api.test.ts
index 4444444..5555555 100644
--- a/src/api.test.ts
+++ b/src/api.test.ts
@@ -1,2 +1,2 @@
-test("old", () => {});
+test("batched", () => {});
"""

OUTSIDE_DIFF = DIFF + """diff --git a/src/cache.ts b/src/cache.ts
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/cache.ts
@@ -0,0 +1 @@
+export const cache = new Map();
"""


def run_cli(*args, **kw):
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        capture_output=True, text=True, cwd=str(ROOT), **kw,
    )


class ParseTests(unittest.TestCase):
    def test_plan_scope_split(self):
        p = parse_plan(PLAN)
        self.assertEqual(p["in_scope"], ["src/api.ts", "src/api.test.ts"])
        self.assertEqual(p["out_of_scope"], ["src/legacy.ts"])
        self.assertEqual(p["planned_sha"], "abc1234")
        self.assertEqual(p["done_criteria"], 2)
        self.assertIn("pnpm test", p["commands"])

    def test_plan_index_is_bookkeeping(self):
        p = parse_plan(PLAN.replace("- `src/api.test.ts`",
                                    "- `src/api.test.ts`\n- `plans/README.md` (status row only)"))
        self.assertNotIn("plans/README.md", p["in_scope"])

    def test_diff_parse_counts(self):
        files = parse_diff(OUTSIDE_DIFF)
        self.assertEqual([f["path"] for f in files],
                         ["src/api.ts", "src/api.test.ts", "src/cache.ts"])
        self.assertEqual(files[0]["adds"], 1)
        self.assertEqual(len(files[0]["hunks"]), 1)
        self.assertTrue(files[2]["new"])

    def test_diff_rename(self):
        text = ("diff --git a/old/x.ts b/new/x.ts\nsimilarity index 90%\n"
                "rename from old/x.ts\nrename to new/x.ts\n--- a/old/x.ts\n+++ b/new/x.ts\n")
        files = parse_diff(text)
        self.assertEqual(files[0]["path"], "new/x.ts")

    def test_report_parse(self):
        r = parse_report("STATUS: COMPLETE\nSTEPS:\n1. did it\nFILES CHANGED:\n"
                         "- `src/api.ts`\nNOTES: ok\n")
        self.assertEqual(r["status"], "COMPLETE")
        self.assertEqual(r["files"], ["src/api.ts"])
        self.assertTrue(r["steps"])


class RuleTests(unittest.TestCase):
    def test_clean_pass(self):
        rep = check(PLAN, DIFF, "", None, "abc1234")
        self.assertEqual(rep["summary"]["status"], "pass")
        self.assertEqual(rep["findings"], [])
        self.assertTrue(rep["summary"]["certifiable"])

    def test_pc001_empty_diff(self):
        rep = check(PLAN, "no diff here\n", None, None, "abc1234")
        self.assertIn("PC001", [f["rule"] for f in rep["findings"]])

    def test_pc002_undelivered(self):
        plan = PLAN.replace("- `src/api.test.ts`", "- `src/api.test.ts`\n- `src/missing.ts`")
        rep = check(plan, DIFF, None, None, "abc1234")
        self.assertEqual([f["rule"] for f in rep["findings"]], ["PC002"])
        self.assertIn("src/missing.ts", rep["findings"][0]["message"])

    def test_pc003_committed_out_of_scope(self):
        rep = check(PLAN, OUTSIDE_DIFF, None, None, "abc1234")
        hit = [f for f in rep["findings"] if f["rule"] == "PC003"]
        self.assertEqual(len(hit), 1)
        self.assertIn("src/cache.ts", hit[0]["message"])
        self.assertEqual(hit[0]["witness"]["kind"], "diff-hunk")

    def test_pc003_declared_out_of_scope(self):
        diff = DIFF + OUTSIDE_DIFF.replace("src/cache.ts", "src/legacy.ts")
        rep = check(PLAN, diff, None, None, "abc1234")
        hit = [f for f in rep["findings"] if f["rule"] == "PC003"]
        self.assertIn("explicitly out of scope", hit[0]["message"])

    def test_pc004_no_scope(self):
        rep = check("# Plan\n\n## Done criteria\n\n- [ ] `pnpm test`\n", DIFF, None, None, "abc1234")
        self.assertIn("PC004", [f["rule"] for f in rep["findings"]])
        self.assertFalse(rep["summary"]["certifiable"])

    def test_pc005_no_done_criteria(self):
        plan = PLAN.split("## Done criteria")[0]
        rep = check(plan, DIFF, None, None, "abc1234")
        self.assertIn("PC005", [f["rule"] for f in rep["findings"]])

    def test_pc006_false_claim(self):
        report = "STATUS: COMPLETE\nFILES CHANGED:\n- `src/api.ts`\n- `src/ghost.ts`\n"
        rep = check(PLAN, DIFF, None, report, "abc1234")
        hit = [f for f in rep["findings"] if f["rule"] == "PC006"]
        self.assertEqual(len(hit), 1)
        self.assertIn("src/ghost.ts", hit[0]["message"])

    def test_pc008_leftovers(self):
        rep = check(PLAN, DIFF, "?? notes.txt\n", None, "abc1234")
        self.assertIn("PC008", [f["rule"] for f in rep["findings"]])

    def test_pc009_missing_base_warns_only(self):
        rep = check(PLAN, DIFF, None, None, None)
        self.assertEqual([f["rule"] for f in rep["findings"]], ["PC009"])
        self.assertEqual(rep["summary"]["errors"], 0)
        self.assertFalse(rep["summary"]["certifiable"])

    def test_pc010_secret_path(self):
        diff = DIFF + OUTSIDE_DIFF.replace("src/cache.ts", ".env.local")
        rep = check(PLAN, diff, None, None, "abc1234")
        self.assertIn("PC010", [f["rule"] for f in rep["findings"]])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "plan.md").write_text(PLAN)
        (self.dir / "ok.diff").write_text(DIFF)
        (self.dir / "bad.diff").write_text(OUTSIDE_DIFF)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_clean_exit0_json(self):
        r = run_cli("--plan", str(self.dir / "plan.md"), "--diff", str(self.dir / "ok.diff"),
                    "--base", "abc1234", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["summary"]["status"], "pass")
        self.assertEqual(data["diff"]["files"][0]["path"], "src/api.ts")

    def test_cli_committed_out_of_scope_exit1(self):
        r = run_cli("--plan", str(self.dir / "plan.md"), "--diff", str(self.dir / "bad.diff"),
                    "--base", "abc1234", "--json")
        self.assertEqual(r.returncode, 1, r.stderr)
        rules = [f["rule"] for f in json.loads(r.stdout)["findings"]]
        self.assertIn("PC003", rules)

    def test_cli_missing_file_exit2(self):
        r = run_cli("--plan", str(self.dir / "nope.md"), "--diff", str(self.dir / "ok.diff"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read plan", r.stderr)

    def test_cli_bad_usage_exit2(self):
        r = run_cli("--diff", str(self.dir / "ok.diff"))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
