"""plan-conformance: check an executed plan's diff against the plan's own scope.

Design notes
------------
* Input is a *base...HEAD* unified diff, not a working-tree diff. That is the
  whole point: shadcn/improve issue #30 shows that a committed out-of-scope
  change disappears from `git diff --stat` / `git status` once the executor
  commits it.
* Everything is offline: plan markdown + diff text + optional status/report
  text. No network, no git invocation, no third-party imports.
* Every finding carries a witness: the smallest piece of input that still
  produces it.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

VERSION = "1.0.0"

RULE_ORDER = [
    "PC001", "PC002", "PC003", "PC004", "PC005",
    "PC006", "PC007", "PC008", "PC009", "PC010",
]

TITLES = {
    "PC001": "empty diff",
    "PC002": "in-scope file not delivered",
    "PC003": "file outside the plan's in-scope list",
    "PC004": "plan has no machine-readable in-scope list",
    "PC005": "plan has no machine-checkable done criteria",
    "PC006": "executor report claims a file the diff does not contain",
    "PC007": "diff contains a file the executor report does not mention",
    "PC008": "worktree has uncommitted or untracked leftovers",
    "PC009": "executor diff has no recorded base commit",
    "PC010": "diff touches a credential-like path",
}

SECRET_RE = re.compile(
    r"(^|/)(\.env(\..*)?|id_rsa|id_ed25519|.*\.pem|.*\.key|credentials\.json)$"
)
CMD_RE = re.compile(
    r"^(pnpm|npm|yarn|bun|npx|git|grep|rg|pytest|python3?|node|cargo|go|make|"
    r"deno|uv|ruff|tsc|eslint|prettier|docker)\b"
)
SHA_RE = re.compile(r"Planned at\**:?\s*commit\s*`?([0-9a-fA-F]{7,40})`?")
BACKTICK_RE = re.compile(r"`([^`]+)`")
HEADING_RE = re.compile(r"^#{2,3}\s+(.*)$")


def _norm(path: str) -> str:
    p = path.strip().strip('"').replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _bullet_paths(lines: list[str]) -> list[str]:
    """Backticked paths from markdown bullets that start with a path token."""
    out = []
    for raw in lines:
        if re.search(r"\((status|bookkeeping|index)", raw, re.I):
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", raw)
        if not m:
            continue
        body = m.group(1).strip()
        bm = BACKTICK_RE.search(body)
        if not bm:
            continue
        # Only bullets whose first component is a path-looking token.
        if body[0] != "`":
            # allow "`path` — note" but not "Depends on: `x`"
            continue
        token = _norm(bm.group(1))
        if not token or " " in token:
            continue
        if token.startswith("/") or ".." in token.split("/"):
            continue
        # execute mode explicitly tells the executor to skip the plan index.
        if token == "plans/README.md":
            continue
        out.append(token)
    return out


def parse_plan(text: str) -> dict:
    lines = text.splitlines()
    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break
    planned = None
    for line in lines:
        m = SHA_RE.search(line)
        if m:
            planned = m.group(1)
            break

    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        hm = HEADING_RE.match(line)
        if hm:
            current = hm.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)

    scope_lines = sections.get("scope", [])
    in_lines, out_lines = [], []
    mode = None
    for raw in scope_lines:
        low = raw.lower()
        if "in scope" in low:
            mode = "in"
            continue
        if "out of scope" in low:
            mode = "out"
            continue
        if raw.lstrip().startswith("#"):
            mode = None
            continue
        if mode == "in":
            in_lines.append(raw)
        elif mode == "out":
            out_lines.append(raw)

    in_scope = _bullet_paths(in_lines)
    out_scope = _bullet_paths(out_lines)

    done_lines = [ln for ln in sections.get("done criteria", []) if re.match(r"^\s*[-*]\s+\[[ xX]\]", ln)]
    commands = []
    for ln in done_lines:
        for tok in BACKTICK_RE.findall(ln):
            tok = tok.strip()
            if CMD_RE.match(tok) and not re.match(r"^git\s+(status|diff)$", tok):
                commands.append(tok)

    steps_verify = []
    for ln in lines:
        vm = re.search(r"\*\*Verify\*\*:\s*`([^`]+)`", ln)
        if vm:
            steps_verify.append(vm.group(1).strip())

    return {
        "title": title,
        "planned_sha": planned,
        "in_scope": in_scope,
        "out_of_scope": out_scope,
        "done_criteria": len(done_lines),
        "commands": commands,
        "step_verifications": steps_verify,
    }


def parse_diff(text: str) -> list[dict]:
    files: list[dict] = []
    cur = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            if m:
                cur = {"path": _norm(m.group(2)), "a_path": _norm(m.group(1)),
                       "adds": 0, "dels": 0, "hunks": [], "new": False, "deleted": False}
            else:
                cur = {"path": "", "a_path": "", "adds": 0, "dels": 0,
                       "hunks": [], "new": False, "deleted": False}
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur["new"] = True
        elif line.startswith("deleted file mode"):
            cur["deleted"] = True
        elif line.startswith("rename to "):
            cur["path"] = _norm(line[len("rename to "):])
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p == "/dev/null":
                cur["deleted"] = True
            else:
                cur["path"] = _norm(p[2:] if p.startswith("b/") else p)
        elif line.startswith("--- "):
            p = line[4:].strip()
            if p == "/dev/null":
                cur["new"] = True
            elif not cur["a_path"]:
                cur["a_path"] = _norm(p[2:] if p.startswith("a/") else p)
        elif line.startswith("@@"):
            cur["hunks"].append({"header": line.strip(), "adds": 0, "dels": 0})
        elif cur["hunks"] and line.startswith("+") and not line.startswith("+++"):
            cur["adds"] += 1
            cur["hunks"][-1]["adds"] += 1
        elif cur["hunks"] and line.startswith("-") and not line.startswith("---"):
            cur["dels"] += 1
            cur["hunks"][-1]["dels"] += 1
    return [f for f in files if f["path"]]


def parse_report(text: str) -> dict:
    files, status, steps = [], None, []
    in_files = False
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^STATUS:\s*(\w+)", s, re.I)
        if m:
            status = m.group(1).upper()
            in_files = False
            continue
        if re.match(r"^FILES CHANGED:\s*$", s, re.I):
            in_files = True
            continue
        if re.match(r"^(STEPS|STOPPED BECAUSE|NOTES):", s, re.I):
            in_files = False
            continue
        if in_files and s:
            for tok in BACKTICK_RE.findall(s) or [s]:
                tok = tok.strip().lstrip("-* ").strip()
                if tok and ("/" in tok or "." in tok.split("/")[-1]):
                    if not tok.lower().startswith("no files"):
                        files.append(_norm(tok))
            steps.append(s)
    return {"status": status, "files": files, "steps": steps}


def _match(path: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        p = _norm(pat)
        if p.endswith("/"):
            if path == p.rstrip("/") or path.startswith(p):
                return pat
            continue
        if any(ch in p for ch in "*?["):
            if fnmatch.fnmatchcase(path, p):
                return pat
        elif path == p:
            return pat
    return None


def check(plan_text: str, diff_text: str, status_text: str | None,
          report_text: str | None, base: str | None) -> dict:
    plan = parse_plan(plan_text)
    diff = parse_diff(diff_text)
    report = parse_report(report_text) if report_text is not None else None
    diff_paths = [f["path"] for f in diff]
    findings = []

    def add(rule: str, severity: str, message: str, witness: dict) -> None:
        findings.append({
            "rule": rule,
            "title": TITLES[rule],
            "severity": severity,
            "message": message,
            "witness": witness,
        })

    if not diff:
        add("PC001", "error",
            "diff contains no file changes; a clean, committed executor worktree also produces "
            "an empty bare `git diff` (see shadcn/improve#30)",
            {"kind": "diff", "text": "<empty>", "why": "no `diff --git` header anywhere in the input"})

    if not plan["in_scope"]:
        add("PC004", "error",
            "plan has no `**In scope**` bullet list of backticked paths, so scope cannot be checked",
            {"kind": "plan", "text": "<no in-scope bullets parsed>",
             "why": "no `- \\`path\\`` line under a `**In scope**` marker"})

    if plan["done_criteria"] == 0:
        add("PC005", "error",
            "plan has no `- [ ]` done criteria; completion is a judgement call, not a check",
            {"kind": "plan", "text": "<no '- [ ]' checkbox under '## Done criteria'>",
             "why": "executor cannot self-verify and reviewer cannot re-run anything"})

    # PC002: promised files that never appear in the diff.
    for pat in plan["in_scope"]:
        if _match(pat, plan["out_of_scope"]):
            continue
        matched = [p for p in diff_paths if _match(p, [pat])]
        if not matched:
            add("PC002", "error",
                f"in-scope path `{pat}` has no hunk in the diff: claimed work was not delivered",
                {"kind": "plan", "text": f"- `{pat}`", "why": "no diff file matches this in-scope path"})

    # PC003: the issue-#30 rule. Diff files must be covered by the in-scope list.
    for f in diff:
        hit_in = _match(f["path"], plan["in_scope"])
        if hit_in:
            continue
        declared = _match(f["path"], plan["out_of_scope"])
        if declared:
            why = f"`{declared}` is explicitly out of scope"
        else:
            why = "not covered by any in-scope entry"
        hunks = f["hunks"] or [{"header": f"[whole file: +{f['adds']} -{f['dels']}]", "adds": f["adds"], "dels": f["dels"]}]
        w = hunks[0]
        add("PC003", "error",
            f"diff modifies `{f['path']}` which the plan does not permit ({why}); "
            "this survives commit and is invisible to a bare `git diff --stat`",
            {"kind": "diff-hunk", "text": f"{f['path']} :: {w['header']}",
             "why": f"one hunk already proves `{f['path']}` is in the diff; "
                    f"the other {max(0, len(hunks) - 1)} hunk(s) are not needed",
             "minimized_from": len(hunks)})

    # PC006/PC007: report claims vs actual diff.
    if report is not None:
        if report["files"]:
            for claimed in report["files"]:
                if not any(_match(p, [claimed]) or _match(claimed, [p]) for p in diff_paths):
                    add("PC006", "error",
                        f"executor report lists `{claimed}` as changed but the diff has no such file",
                        {"kind": "report", "text": f"FILES CHANGED: {claimed}",
                         "why": "claim has no matching diff file; the report is unverified"})
            for f in diff:
                if not any(_match(f["path"], [c]) or _match(c, [f["path"]]) for c in report["files"]):
                    add("PC007", "warning",
                        f"diff changes `{f['path']}` but the executor report does not list it",
                        {"kind": "diff-file", "text": f"diff --git a/{f['path']}",
                         "why": "undocumented change; reviewer must read it explicitly"})
        if report["status"] == "COMPLETE" and plan["commands"] and not report["steps"]:
            add("PC006", "warning",
                "report says COMPLETE but carries no STEPS lines to audit",
                {"kind": "report", "text": "STATUS: COMPLETE",
                 "why": "no per-step evidence, so every claim rests on the status word alone"})

    # PC008: leftovers.
    if status_text is not None:
        leftovers = [ln for ln in status_text.splitlines()
                     if ln.strip() and not ln.startswith("##")]
        if leftovers:
            add("PC008", "error",
                f"worktree is not clean: {len(leftovers)} staged/unstaged/untracked entr(y/ies) "
                "will not appear in a base...HEAD diff",
                {"kind": "status", "text": leftovers[0].strip(),
                 "why": "one porcelain line is enough to show the worktree differs from HEAD",
                 "minimized_from": len(leftovers)})

    if base is None:
        add("PC009", "warning",
            "no --base recorded: this checker cannot prove the diff includes executor commits; "
            "generate it with `git diff <base>...HEAD`",
            {"kind": "meta", "text": "<--base not supplied>",
             "why": "empty base means the committed-change gap from shadcn/improve#30 is not certified closed"})

    for f in diff:
        if SECRET_RE.search(f["path"]):
            add("PC010", "error",
                f"diff touches credential-like path `{f['path']}`",
                {"kind": "diff-file", "text": f"diff --git a/{f['path']}",
                 "why": "path matches a credential filename pattern"})

    findings.sort(key=lambda x: (RULE_ORDER.index(x["rule"]), x["message"]))
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = len(findings) - errors
    return {
        "tool": "plan-conformance",
        "version": VERSION,
        "plan": {
            "title": plan["title"],
            "planned_sha": plan["planned_sha"],
            "in_scope": plan["in_scope"],
            "out_of_scope": plan["out_of_scope"],
            "done_criteria": plan["done_criteria"],
            "commands": plan["commands"],
            "step_verifications": plan["step_verifications"],
        },
        "diff": {
            "base": base,
            "files": [{"path": f["path"], "adds": f["adds"], "dels": f["dels"],
                       "hunks": len(f["hunks"]), "new": f["new"], "deleted": f["deleted"]}
                      for f in diff],
        },
        "report": report,
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "status": "fail" if errors else ("warn" if warnings else "pass"),
            "certifiable": bool(base) and bool(plan["in_scope"]) and plan["done_criteria"] > 0,
        },
        "findings": findings,
    }


def render_text(rep: dict) -> str:
    s = rep["summary"]
    out = [f"plan-conformance {rep['version']}  ->  {s['status'].upper()}",
           f"plan: {rep['plan']['title'] or '<untitled>'}"
           f"  in-scope={len(rep['plan']['in_scope'])}"
           f"  out-of-scope={len(rep['plan']['out_of_scope'])}"
           f"  done={rep['plan']['done_criteria']}",
           f"diff: {len(rep['diff']['files'])} file(s)  base={rep['diff']['base'] or '<none>'}",
           ""]
    if not rep["findings"]:
        out.append("no findings")
    for f in rep["findings"]:
        out.append(f"{f['rule']} {f['severity'].upper():7} {f['message']}")
        out.append(f"           witness[{f['witness']['kind']}]: {f['witness']['text']}")
    out.append("")
    out.append(f"{s['errors']} error(s), {s['warnings']} warning(s)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="plan-conformance",
        description="Check an executed plan's base...HEAD diff against the plan's own scope.",
    )
    ap.add_argument("--plan", required=True, help="plan markdown file (shadcn/improve format)")
    ap.add_argument("--diff", required=True,
                    help="unified diff from `git diff <base>...HEAD` (must include commits)")
    ap.add_argument("--status", help="optional `git status --porcelain -uall` output")
    ap.add_argument("--report", help="optional executor report (STATUS/FILES CHANGED/NOTES)")
    ap.add_argument("--base", help="base commit SHA the diff was taken from")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args(argv)

    try:
        plan_text = Path(args.plan).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read plan: {exc}", file=sys.stderr)
        return 2
    try:
        diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read diff: {exc}", file=sys.stderr)
        return 2
    if not plan_text.strip():
        print("error: plan is empty", file=sys.stderr)
        return 2

    status_text = report_text = None
    try:
        if args.status:
            status_text = Path(args.status).read_text(encoding="utf-8", errors="replace")
        if args.report:
            report_text = Path(args.report).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read optional input: {exc}", file=sys.stderr)
        return 2

    rep = check(plan_text, diff_text, status_text, report_text, args.base)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep))
    return 1 if rep["summary"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
