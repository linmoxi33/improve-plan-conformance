# Plan 001: Batch the user lookup in the order list

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `416e5df`, 2026-11-12

## Why this matters

`listOrders` issues one query per row for the owning user. On a 50-row page
that is 51 round trips; the endpoint is the slowest in the app.

## Current state

- `src/orders/api.ts` — order-list endpoint; contains the N+1 (lines 1–3)
- `src/orders/api.test.ts` — the only existing test for this endpoint

## Scope

**In scope** (the only files you should modify):
- `src/orders/api.ts`
- `src/orders/api.test.ts`
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch, even though they look related):
- `src/orders/legacy-api.ts` — deprecated path, scheduled for deletion
- Any change to the public response shape — clients depend on it

## Steps

### Step 1: Batch the user lookup

Load the page of orders, then fetch all owners in one `in` query and join in
memory.

**Verify**: `pnpm test -- orders` → all pass

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `pnpm typecheck` exits 0
- [ ] `pnpm test -- orders` exits 0
- [ ] No files outside the in-scope list are modified (`git status`)

## STOP conditions

Stop and report back (do not improvise) if the code at the locations in
"Current state" doesn't match the excerpt.
