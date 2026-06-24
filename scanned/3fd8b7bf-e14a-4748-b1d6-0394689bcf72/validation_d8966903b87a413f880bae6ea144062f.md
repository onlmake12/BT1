The code confirms the claim. Looking at `put` (lines 62–69), `self.total_keys_num` is never assigned the incremented value — only a local binding `total_keys_num` is used for the comparison. The `shrink` method (lines 110–111) does correctly update `self.total_keys_num` after shrinking, but it is never reached because the guard condition never becomes true.

The test in `recent_reject.rs` (line 39) asserts `recent_reject.total_keys_num < 100` after 160 inserts against a limit of 100 — this passes trivially because the counter stays at 0 (the initial DB estimate), not because shrink was actually triggered. The test does not validate that shrink fired.

---

Audit Report

## Title
`RecentReject::put` Never Updates `self.total_keys_num`, Permanently Disabling the `count_limit` Shrink Guard — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, `self.total_keys_num.checked_add(1)` produces a local binding that is used only for comparison but is never written back to `self.total_keys_num`. Because the field is initialized once at startup from a DB estimate and never incremented thereafter, the condition `total_keys_num > self.count_limit` is permanently false (on a fresh or recently-started node the initial estimate is 0 or low). The `shrink` function is therefore never called proactively, allowing the on-disk RocksDB-with-TTL store to grow without bound until TTL expires entries naturally.

## Finding Description
`self.total_keys_num` is set once in `build` (line 44–51) from `estimate_num_keys_cf` across all shards. In `put` (lines 62–69):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

The local `total_keys_num` is `self.total_keys_num + 1`, but `self.total_keys_num` is never assigned this value. Every call to `put` therefore compares the same stale initial value (plus 1) against `count_limit`. On a fresh node the initial estimate is 0, so the comparison is always `1 > count_limit`, which is false for any reasonable `count_limit`. The overflow branch is also unreachable because the field never grows.

`shrink` (lines 104–113) does correctly re-estimate and assign `self.total_keys_num` after dropping a shard, but it is never invoked because the guard never fires.

The existing unit test (`test_basic`, line 39) asserts `recent_reject.total_keys_num < 100` after 160 inserts against a limit of 100. This assertion passes trivially because the counter stays at 0, not because shrink was triggered — the test does not verify that shrink actually ran or that any shard was dropped.

**Exploit path:**
1. An unprivileged P2P peer connects to the target node.
2. The peer continuously relays unique transactions that will be rejected (e.g., transactions spending non-existent cells, always-failure lock scripts, fee-rate failures — each with a unique input to avoid `Reject::Duplicated`).
3. Each rejection passes `should_recorded()` (all types except `Reject::Duplicated`) and is stored via `put_recent_reject` → `RecentReject::put`.
4. Because `self.total_keys_num` never increases, `shrink` is never called, and the RocksDB store grows at the rate of `rejected_tx_rate × TTL` until disk space is exhausted.

## Impact Explanation
The `count_limit` guard is the only proactive bound on the `RecentReject` store. With it broken, the store is bounded only by TTL expiry. For a long TTL (e.g., days), a sustained stream of unique rejected transactions can exhaust disk space. Disk exhaustion prevents the node from writing new blocks to the chain database, halting the node entirely. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Any unprivileged P2P peer can relay transactions. Generating unique transactions that will be rejected requires no special privilege, no keys, and no on-chain funds (spending non-existent cells is sufficient). The attack is cheap, requires only a persistent connection, and is present in every node running this code. The broken guard is not conditional on any configuration or runtime state.

## Recommendation
Add the missing assignment in `put` so the counter is updated before the comparison:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // missing assignment
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Additionally, update the unit test to assert that `shrink` actually fired (e.g., by verifying that a shard was dropped or that the DB key count is below `count_limit` via `estimate_total_keys_num`, not via the stale `total_keys_num` field).

## Proof of Concept
1. Run the existing `test_basic` test under a debugger or with added instrumentation: insert a `println!` or counter inside `shrink`. Observe that `shrink` is **never called** despite 160 inserts against a limit of 100, and that `total_keys_num` remains 0 throughout.
2. Alternatively, call `recent_reject.estimate_total_keys_num()` (the private DB-backed estimate) after the 160 inserts and compare it to `recent_reject.total_keys_num` — the former will reflect the actual key count while the latter stays at 0, confirming the counter is stale.
3. For a network-level PoC: start a node with a small `count_limit` (e.g., 10) and a long TTL; relay 11+ unique rejected transactions via P2P; confirm via `get_estimate_total_keys_num()` that it returns the initial value (0 or low) while the actual DB size exceeds `count_limit`.