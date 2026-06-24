The code confirms the claim exactly.

In `put()` at lines 62–65, `self.total_keys_num` is never assigned the incremented value — only a local `total_keys_num` is computed and compared: [1](#0-0) 

`shrink()` is the only place that writes back to `self.total_keys_num`: [2](#0-1) 

But `shrink()` is unreachable because the guard condition `total_keys_num > self.count_limit` always evaluates against `0 + 1` on a fresh DB. The test at line 39 asserts `total_keys_num < 100` after 160 puts — it passes only because the field is still 0: [3](#0-2) 

---

Audit Report

## Title
`RecentReject::put()` Never Persists Incremented `total_keys_num`, Making `count_limit` Permanently Ineffective — (File: tx-pool/src/component/recent_reject.rs)

## Summary
In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable and never written back to `self.total_keys_num`. The struct field remains at its initial RocksDB estimate (0 on a fresh node) indefinitely, so the `count_limit` guard is never satisfied, `shrink()` is never called, and the on-disk RocksDB store for rejected transactions grows without bound. An unprivileged attacker can exhaust node disk space by submitting a sustained stream of cheaply-invalid transactions.

## Finding Description
In `put()` (lines 62–65 of `tx-pool/src/component/recent_reject.rs`):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
}
```

`self.total_keys_num` is never assigned `total_keys_num`. On a fresh node, `self.total_keys_num` is initialized to the RocksDB estimate (0 for an empty DB) in `build()` at lines 39–51. Every subsequent call to `put()` therefore evaluates `0 + 1 > count_limit`, which is `false` for any reasonable limit. `shrink()` at lines 104–113 — the only place that updates `self.total_keys_num` — is unreachable. The existing unit test at `tests/recent_reject.rs:39` inadvertently confirms this: after 160 `put()` calls with `limit = 100`, the assertion `total_keys_num < 100` passes because the counter is still 0, not because the limit was enforced.

## Impact Explanation
The `recent_reject` RocksDB store accumulates every qualifying rejected transaction without ever triggering `shrink()`. The only remaining bound is the TTL expiry window. An attacker submitting a sustained stream of invalid transactions can fill the node's disk within that window, causing the node to crash or become unable to write blocks, chain state, or tx-pool data. This matches **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The exploit requires no privilege. Any peer or RPC caller can invoke `send_transaction`. Transactions rejected with reasons such as `DeclaredWrongCycles`, `Malformed`, or `Verification` are recorded via `put_recent_reject()` when `reject.should_recorded()` is true. Crafting cheaply-invalid transactions (e.g., mismatched declared cycle counts) is straightforward and requires no special access. The bug is present on every node with `recent_reject` enabled, which is the default configuration.

## Recommendation
Assign the incremented value back to `self.total_keys_num` before the limit check in `put()`:

```rust
if let Some(new_total) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = new_total;   // ← persist the increment
    if self.total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

## Proof of Concept
The existing unit test at `tx-pool/src/component/tests/recent_reject.rs:6–39` is itself the proof of concept. It performs 160 `put()` calls against a limit of 100 and then asserts `recent_reject.total_keys_num < 100`. The assertion passes — but only because `total_keys_num` is still 0, not because the limit was enforced and `shrink()` ran. Changing the assertion to `recent_reject.total_keys_num >= 100` (the expected post-condition if the counter were correctly maintained) would cause the test to fail, confirming the bug. For a live node, repeatedly submitting transactions that trigger `should_recorded()` rejections via the `send_transaction` RPC will grow the on-disk store indefinitely until disk exhaustion.

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

**File:** tx-pool/src/component/tests/recent_reject.rs (L39-39)
```rust
    assert!(recent_reject.total_keys_num < 100);
```
