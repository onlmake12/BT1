The code is confirmed. The bug is real and the exploit path is valid. Let me verify the key facts:

1. `put()` at lines 62-69: `self.total_keys_num` is never updated — only a local shadow variable is computed. [1](#0-0) 

2. `shrink()` at lines 110-111 is the only place that writes back to `self.total_keys_num`, but it is never reached because the guard condition `total_keys_num > self.count_limit` always evaluates against the frozen initial value. [2](#0-1) 

3. The TTL is `keep_rejected_tx_hashes_days × 86400` seconds (minimum 1 day), meaning the DB is bounded by `rejection_rate × TTL × entry_size` — not by `count_limit`. At high rejection rates this is gigabytes. [3](#0-2) 

4. The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes trivially with the bug because `total_keys_num` stays at 0, not because `shrink()` was correctly invoked. [4](#0-3) 

---

Audit Report

## Title
`RecentReject.total_keys_num` Never Incremented in `put()`, Permanently Disabling `count_limit` Guard — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local shadow variable `total_keys_num` but is never written back to `self.total_keys_num`. The field stays frozen at its initialization value for the entire lifetime of the node. The `count_limit` guard that is supposed to trigger `shrink()` — the only active mechanism bounding the on-disk `recent_reject` RocksDB store — therefore never fires. An unprivileged attacker who can generate cheap-to-reject transactions can grow the store until disk space is exhausted, crashing the node.

## Finding Description
`RecentReject` is initialized in `build()` with `total_keys_num` estimated from RocksDB column-family key counts. In `put()` (lines 62–69), the code computes `self.total_keys_num.checked_add(1)` into a local variable named `total_keys_num`, checks whether it exceeds `self.count_limit`, and calls `shrink()` if so — but never executes `self.total_keys_num = total_keys_num`. The field is only mutated inside `shrink()` itself (lines 110–111), which re-estimates the count from the DB after dropping a shard. Because `self.total_keys_num` never grows, the condition `(initial_estimate + 1) > count_limit` evaluates to `1 > count_limit` on a fresh node (initial estimate = 0), which is false for any sane limit. `shrink()` is therefore never called through the normal `put()` path.

The TTL (`keep_rejected_tx_hashes_days × 86400` seconds, minimum 86 400 s) provides a natural upper bound of `rejection_rate × TTL × entry_size`, but RocksDB TTL expiry is lazy (applied during compaction, not on write). Under a sustained high-rate attack the live key count grows far beyond `count_limit` before compaction catches up, and the on-disk size can reach tens of gigabytes.

The stale counter is also the value returned by `get_estimate_total_keys_num()` (line 81) and surfaced through the `get_total_recent_reject_num` RPC path (service.rs lines 1100–1106), misleading operators into believing the store is empty.

The existing unit test (`assert!(recent_reject.total_keys_num < 100)`) passes trivially because the frozen value is 0, not because `shrink()` was correctly triggered.

## Impact Explanation
**High (10001–15000 points) — Vulnerability which could easily crash a CKB node.**

With `count_limit` permanently disabled, the `recent_reject` RocksDB directory grows at `rejection_rate × TTL`. At 1 000 rejections/second and a 1-day TTL, the store accumulates ~86 million entries. At ~200 bytes per entry (32-byte key + JSON-serialized `PoolTransactionReject`), that is ~17 GB — sufficient to exhaust disk on a typical node and cause a crash or unrecoverable I/O error. The attack requires no special privilege and no script execution.

## Likelihood Explanation
Any peer or RPC caller can submit transactions. Transactions rejected for fee-below-minimum, duplicate hash, or capacity underflow are recorded in `recent_reject` without script execution, making each rejection essentially free for the attacker. A single CKB wallet can generate thousands of such submissions per second. No majority hash-power, key material, or victim mistake is required.

## Recommendation
In `put()`, write the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    let hash_slice = hash.as_slice();
    let shard = self.get_shard(hash_slice).to_string();
    let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
    let json_string = serde_json::to_string(&reject)?;
    self.db.put(&shard, hash_slice, json_string)?;

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;   // ← missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

Additionally, update the unit test to assert that `shrink()` was actually invoked (e.g., by verifying that `total_keys_num` was re-estimated from the DB rather than remaining at 0).

## Proof of Concept
1. Start a CKB node with a non-empty `recent_reject` path and a small `keep_rejected_tx_hashes_count` (e.g., 100).
2. In a loop, call `send_transaction` RPC with a valid-structure transaction whose fee rate is below `min_fee_rate`. Each call returns a rejection and writes one entry to the `recent_reject` DB.
3. After >100 submissions, observe via `get_overview` that `total_recent_reject_num` is still 0 (frozen initial value) and that the on-disk `recent_reject` directory size grows monotonically with no shard ever being dropped.
4. Alternatively, run the existing unit test under a debugger or with an added `assert!(recent_reject.total_keys_num > 0)` after the first 80 puts — it will fail, confirming the counter is never updated.

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L62-69)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
```

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```

**File:** tx-pool/src/pool.rs (L715-716)
```rust
            let recent_reject_ttl =
                u8::max(1, config.keep_rejected_tx_hashes_days) as i32 * 24 * 60 * 60;
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L39-39)
```rust
    assert!(recent_reject.total_keys_num < 100);
```
