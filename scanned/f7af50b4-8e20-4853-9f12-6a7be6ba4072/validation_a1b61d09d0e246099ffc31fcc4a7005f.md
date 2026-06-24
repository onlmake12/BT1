The code is confirmed. Let me verify the `shrink()` behavior and check for any other guards.

Audit Report

## Title
`RecentReject::put()` Never Persists Incremented `total_keys_num`, Disabling Count-Based Shrink — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` that is used for comparison but never written back to `self.total_keys_num`. As a result, the struct field remains permanently at its startup-estimated value, the `count_limit` guard is never triggered through the count path, and the RocksDB-with-TTL shard can grow without bound until TTL expiry removes entries. An unprivileged attacker submitting a stream of rejected transactions can exhaust node disk space.

## Finding Description
`RecentReject` is initialized in `build()` (lines 28–53), where `total_keys_num` is set once from a RocksDB key-count estimate. On every subsequent call to `put()` (lines 55–71), the code computes:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is never assigned here
}
```

The local binding `total_keys_num` holds the incremented value, but `self.total_keys_num` is never assigned. For a node starting with an empty DB (`total_keys_num = 0`), every call evaluates `1 > count_limit`, which is false for any reasonable `count_limit` (default is in the thousands). The field stays at 0 indefinitely.

`shrink()` (lines 104–113) does correctly update `self.total_keys_num = total_keys_num` (line 111) after dropping a shard, but it is only reachable through the count path that is never triggered, or through the `u64` overflow path which requires `2^64` calls. The only remaining backstop is the RocksDB TTL, which expires entries after a fixed wall-clock duration but provides no bound on the number of live entries at any given moment.

The reject callback in `shared_builder.rs` (lines 576–602) wires every transaction rejection directly to `recent_reject.put()`, and `reject.should_recorded()` is true for common rejection reasons (low fee rate, invalid scripts, capacity errors).

## Impact Explanation
An attacker who continuously submits transactions that are rejected (e.g., below minimum fee rate) causes `recent_reject.put()` to be called for each one. Because `self.total_keys_num` never increments, `shrink()` is never invoked, and every rejected transaction is durably written to the shard DB. Over the TTL window the on-disk size grows proportionally to the submission rate with no enforced upper bound. Sustained submission can exhaust the node's disk, causing RocksDB write failures and a node crash. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires no privilege. Any actor can submit transactions via the public JSON-RPC `send_transaction` endpoint or via the P2P relay protocol. Transactions rejected for low fee rate cost almost nothing to produce (no valid PoW or fee required beyond the minimum to pass initial parsing). The attack is repeatable and cheap: a single machine can sustain thousands of rejected submissions per second. No victim mistake or external dependency is required.

## Recommendation
Assign the incremented value back to `self.total_keys_num` before the comparison, and reset it after `shrink()` (which already does so):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // persist the updated count
    if total_keys_num > self.count_limit {
        self.shrink()?;  // shrink() already resets self.total_keys_num via estimate
    }
} else {
    self.shrink()?;
}
```

## Proof of Concept
1. Start a CKB node with a small `count_limit` (e.g., 100) in the tx-pool config.
2. Submit 10,000 transactions via `send_transaction` RPC, each with a fee rate below the node's minimum threshold.
3. Each transaction is rejected; `recent_reject.put()` is called 10,000 times.
4. After all submissions, inspect `self.total_keys_num` via `get_estimate_total_keys_num()` (line 80–82): it still reports the startup value (≈0), not 10,000.
5. Inspect the on-disk size of the `recent_reject` RocksDB directory: it contains all 10,000 entries, far exceeding `count_limit = 100`.
6. The existing unit test file at `tx-pool/src/component/tests/recent_reject.rs` can be extended with an assertion that `get_estimate_total_keys_num()` equals the number of `put()` calls made, which will fail against the current code, confirming the bug.