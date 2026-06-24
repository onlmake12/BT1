The code confirms the claim. At line 62, `self.total_keys_num.checked_add(1)` binds the result to a local `total_keys_num` variable, but `self.total_keys_num` is never reassigned in `put()`. [1](#0-0) 

`self.total_keys_num` is only ever written in `shrink()` (line 111), which is never reached via the count path because the counter stays at its initial estimate. [2](#0-1) 

---

Audit Report

## Title
`RecentReject` Count Limit Never Enforced Due to Missing Write-Back of Incremented `total_keys_num` in `put()` — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` but is never written back to `self.total_keys_num`. The field remains at its initial RocksDB estimate (typically `0` for a fresh database) for the entire lifetime of the process. Because the count-limit guard is never triggered, `shrink()` is never called via the count path, and the on-disk rejected-transaction RocksDB database grows without bound within the TTL window. An unprivileged attacker can exploit this to exhaust node disk space and crash the node.

## Finding Description
`RecentReject` is initialized in `build()` with `total_keys_num` set to the RocksDB key-count estimate (typically `0` for a new database).

In `put()` (lines 62–69):
```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```
`self.total_keys_num.checked_add(1)` produces a new value bound to the local name `total_keys_num`. `self.total_keys_num` is never reassigned here. After initialization to `0`, it stays `0` permanently (unless `shrink()` is called, which it never is via this path).

Each call to `put()` evaluates `0 + 1 > count_limit`, i.e., `1 > count_limit`. Since `count_limit` is derived from `keep_rejected_tx_hashes_count` (default: a large number ≥ 1), this is always `false`. The overflow branch requires `u64::MAX` calls — practically unreachable. `shrink()` is therefore never invoked via the count path.

The only write-back of `self.total_keys_num` exists in `shrink()` at line 111, which is unreachable. The TTL (default 7 days) is the sole remaining bound on database size.

## Impact Explanation
An attacker who can reach the node's RPC endpoint or P2P relay can continuously submit transactions guaranteed to be rejected (e.g., double-spends of a known dead cell). Each rejection calls `put()`, writing one entry to RocksDB. Because `shrink()` is never triggered, entries accumulate over the full TTL window (up to 7 days by default). A sustained flood fills the node's disk, causing RocksDB write failures that propagate upward and halt the tx-pool service — crashing the CKB node.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
- The RPC `send_transaction` endpoint requires no special privilege.
- Generating a stream of rejected transactions is trivial: re-sign a transaction spending an already-spent cell to produce unique tx hashes.
- The bug is deterministic: every single call to `put()` fails to update the counter; no race condition or timing dependency is involved.
- The only natural mitigation is the TTL, but a sustained low-rate flood within the TTL window is sufficient to exhaust disk space.

Likelihood: **Medium-High**.

## Recommendation
In `put()`, write the incremented value back to `self.total_keys_num` before the comparison:
```rust
if let Some(new_total) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = new_total;   // ← missing write-back
    if self.total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```
Additionally, `shrink()` already resets `self.total_keys_num` via `estimate_total_keys_num()` (line 110–111), so no change is needed there.

## Proof of Concept
1. Start a CKB node with default configuration (`keep_rejected_tx_hashes_count = 10_000`, `keep_rejected_tx_hashes_days = 7`).
2. Obtain any live cell outpoint from the chain.
3. In a loop, construct a transaction that double-spends that outpoint (change only the witness each iteration to produce a unique tx hash) and submit it via the `send_transaction` RPC.
4. Each submission is rejected and recorded via `put()`.
5. Observe via `get_estimate_total_keys_num()` that `self.total_keys_num` never advances past its initial value, `shrink()` is never invoked, and the `recent_reject` RocksDB directory grows monotonically.
6. Continue until disk exhaustion causes RocksDB write failures and the tx-pool service halts.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L62-65)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
```

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```
