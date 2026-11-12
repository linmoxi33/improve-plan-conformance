# plan-conformance

Check that an agent actually did what its execution plan allowed — from a **committed** `base...HEAD` diff, not a working-tree `git diff` — and give every verdict a one-hunk witness.

Every plan written by `shadcn/improve` already contains a machine-readable contract: an `**In scope**` file list, an `**Out of scope**` list, and `- [ ]` done criteria. The skill's own review step then checks that contract with a bare `git diff --stat`. That command cannot see a change the executor already committed. This tool reads the contract and the real diff, offline, and says exactly which clause failed.

Upstream: https://github.com/shadcn/improve

## The gap (with real links)

`shadcn/improve` is an agent skill: the expensive model audits a repo and writes self-contained plans, a cheaper model executes one in an isolated git worktree, and the advisor reviews the diff.

- Open issue: [shadcn/improve#30 — "execute scope check misses changes already committed by the executor"](https://github.com/shadcn/improve/issues/30). Reproduction in the issue: the plan permits only `allowed.txt`; the executor commits a new `outside-plan.txt`; `git diff --stat` and `git status --porcelain` are both **empty**; only `git diff <execution-base> HEAD --` names the file.
- The review step it points at: [`closing-the-loop.md#L55`](https://github.com/shadcn/improve/blob/cac56e1ebd3c279aa9153616cfeac7b174ab90f9/skills/improve/references/closing-the-loop.md#L55) — "Scope compliance: `git -C <worktree> diff --stat` against the plan's in-scope list."
- The same blind spot in the plan template's done criteria: [`plan-template.md#L135`](https://github.com/shadcn/improve/blob/cac56e1ebd3c279aa9153616cfeac7b174ab90f9/skills/improve/references/plan-template.md#L135) — "No files outside the in-scope list are modified (`git status`)".

Reproduced locally while building this tool (git 2.45-era repo, 3-file executor commit):

```
$ git diff --stat            # what the upstream review runs
(empty)
$ git diff 416e5df...HEAD --stat   # what this tool consumes
 src/orders/api.test.ts | 2 +-
 src/orders/api.ts      | 4 +++-
 src/orders/cache.ts    | 1 +
 3 files changed, 5 insertions(+), 2 deletions(-)
```

`src/orders/cache.ts` is in none of the plan's in-scope entries. This tool reports it as `PC003` with the exact hunk as witness. That is the whole reason it exists.

## What it does / does not do

**Does**

- Parses the plan's `**In scope**` / `**Out of scope**` bullets (backticked paths, globs, `dir/` prefixes) and `- [ ]` done criteria.
- Parses a unified `git diff <base>...HEAD` (new/deleted/renamed files, quoted paths, multiple hunks).
- Cross-checks the optional executor report (`STATUS` / `FILES CHANGED`) and optional `git status --porcelain -uall` leftovers.
- Emits deterministic JSON; every finding carries the smallest witness (one hunk / one plan line) that still reproduces it.
- Exits non-zero on real conformance errors, so it drops into CI or an agent loop.

**Does not**

- Run git, tests, or the plan's commands. It never claims a command passed.
- Judge code quality, or whether the diff implements the plan's *intent*.
- Replace the advisor's review; it only makes scope/report claims mechanically checkable.

## Install

Zero dependencies, Python 3.9+.

```bash
git clone <this repo> && cd improve-plan-conformance
python3 -m unittest discover -s tests        # 20 tests
```

## Usage

```bash
# in the executor worktree, BEFORE any checkout/reset:
BASE=$(git rev-parse <execution-base>)
git diff "$BASE"...HEAD > /tmp/exec.diff
git status --porcelain -uall > /tmp/status.txt

./bin/plan-conformance \
  --plan plans/001-fix-n-plus-one.md \
  --diff /tmp/exec.diff \
  --status /tmp/status.txt \
  --report /tmp/executor-report.txt \
  --base "$BASE"
```

Try the bundled example:

```bash
./bin/plan-conformance \
  --plan examples/in/plan-001-fix-n-plus-one.md \
  --diff examples/in/exec-committed.diff \
  --status examples/in/status-clean.txt \
  --report examples/in/executor-report.txt \
  --base 416e5dfd0d1f6f21c4356b29433f6b003eae0d56
# -> PC003: diff modifies `src/orders/cache.ts` ...   exit 1
```

The same run with `--json` is checked in at [`examples/out/report.json`](examples/out/report.json).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | No error-severity findings (warnings may still be present; see `summary.status == "warn"`) |
| `1` | At least one error finding |
| `2` | Usage error, unreadable input, or empty plan |

### Rules

| Rule | Severity | Triggers when |
|---|---|---|
| `PC001` | error | diff has no file changes (often a bare `git diff` after the executor committed) |
| `PC002` | error | an in-scope path has no hunk — claimed work not delivered |
| `PC003` | error | a diff file is outside the in-scope list (declared out-of-scope is called out separately) |
| `PC004` | error | plan has no machine-readable in-scope list |
| `PC005` | error | plan has no `- [ ]` done criteria |
| `PC006` | error | report claims a file the diff does not contain |
| `PC007` | warning | diff changes a file the report omits |
| `PC008` | error | `git status` shows staged/unstaged/untracked leftovers |
| `PC009` | warning | `--base` missing, so committed changes are not certified visible |
| `PC010` | error | diff touches a credential-like path (`.env*`, `*.pem`, `id_rsa`, …) |

`summary.certifiable` is `true` only when a base commit was supplied **and** the plan has an in-scope list and done criteria.

### JSON shape (abridged)

```json
{
  "tool": "plan-conformance",
  "version": "1.0.0",
  "plan": {"title": "Plan 001: ...", "in_scope": ["src/orders/api.ts"], "out_of_scope": ["src/orders/legacy-api.ts"], "done_criteria": 3},
  "diff": {"base": "416e5df...", "files": [{"path": "src/orders/cache.ts", "adds": 1, "dels": 0, "hunks": 1, "new": true}]},
  "summary": {"errors": 1, "warnings": 1, "status": "fail", "certifiable": true},
  "findings": [{"rule": "PC003", "severity": "error", "message": "...", "witness": {"kind": "diff-hunk", "text": "src/orders/cache.ts :: @@ -0,0 +1 @@", "why": "one hunk already proves ...", "minimized_from": 1}}]
}
```

## 本轮创新

**First plan↔diff conformance checker that consumes the committed `base...HEAD` range and attaches a per-finding witness.** Prior tools in my ledger are static format linters: they read a config and apply rules. This one reads two artifacts (a plan and an execution diff) and adjudicates *delivery*: which promised file never appeared, which changed file was never permitted, which report line the diff contradicts. The witness design is the second new bit: for `PC003` the tool does not dump the file — it emits one hunk and states that dropping every other hunk leaves the finding true, so a reviewer can check the verdict in seconds. The upstream's own gap report (#30) is exactly the committed-change blind spot, and the example diff in this repo is a real `git diff base...HEAD` from a throwaway repo that reproduces it.

## Limits

- Scope matching is path-based. A plan that says "only add a helper inside `src/api.ts`" cannot be checked at symbol level.
- `PC002` is skipped for `plans/README.md`, because execute mode explicitly tells the executor not to update the plan index.
- The plan parser expects the upstream template's `**In scope**` / `**Out of scope**` markers; a free-form plan yields `PC004` rather than a guess.
- `PC006`/`PC007` are only as good as the executor's report format; a report without a `FILES CHANGED:` block is not cross-checked.
- The tool does not verify that done criteria actually ran — it verifies the plan has them and that the diff respects scope. A passing run is not proof the tests passed.

MIT © 2026 linmoxi33
