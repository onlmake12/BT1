Audit Report

## Title
`RecentReject.total_keys_num` Never Incremented in `put()`, Permanently Bypassing `count_limit` Guard — (File: `tx-pool/src/component/recent_reject.rs`)

## Summary
In `put()`, `checked_add(1)` produces a new value bound to a local variable `total_keys_num`, but `self.total_keys_num` is never assigned the result. The field stays frozen at its initialization value (0 for a fresh node), so the condition `total_keys_num > self.count_limit` is never true, `shrink()` is never called via the normal increment path, and the on-disk `recent_reject` RocksDB store grows without bound for the lifetime of the node.

## Finding Description
In `build()`, `self.total_keys_num` is initialized from a RocksDB key-count estimate — zero for a fresh node. [1](#0-0) 

In `put()` (lines 62–69), `checked_add(1)` shadows `self.total_keys_num` with a local binding. The local `total_keys_num` is used for the limit comparison, but `self.total_keys_num` is never written back: [2](#0-1) 

For a fresh node, `self.total_keys_num` is 0. Every call to `put()` computes `0 + 1 = 1` into the local, checks `1 > count_limit` (false for any sane limit), and exits without updating the field or calling `shrink()`. The field remains 0 permanently.

`self.total_keys_num` is only mutated inside `shrink()`: [3](#0-2) 

But `shrink()` is unreachable via the broken counter path. The existing unit test asserts `recent_reject.total_keys_num < 100` after 160 puts with `limit = 100`: [4](#0-3) 

Because the counter is frozen at 0, this assertion trivially passes (`0 < 100`) and does not detect the missing write-back. RocksDB's TTL provides eventual expiry only during background compaction; it does not actively bound the live key count the way `shrink()` does (dropping a random shard entirely).

## Impact Explanation
The `count_limit` guard is the sole in-process mechanism that caps the size of the `recent_reject` store. With it permanently disabled, every rejected transaction is written to the store and never evicted by the `shrink()` path. An attacker who can generate cheap-to-reject transactions at high rate can exhaust the node's disk, causing the node process to crash or become unable to write any further data. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
No special privilege is required. Any RPC caller or P2P peer can submit transactions. Transactions rejected for cheap reasons (fee below `min_fee_rate`, already-known hash, capacity underflow) are recorded in `recent_reject` without script execution. A single attacker with a standard CKB wallet can sustain a high rate of such submissions. The TTL window (default 7 days) means entries accumulate for a long period before RocksDB compaction reclaims space, amplifying disk growth per unit of attacker effort.

## Recommendation
In `put()`, assign the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    let hash_slice = hash.as_slice();
    let shard = self.get_shard(hash_slice).to_string();
    let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
    let json_string = serde_json::to_string(&reject)?;
    self.db.put(&shard, hash_slice, json_string)?;

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;   // missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

The unit test in `tx-pool/src/component/tests/recent_reject.rs` should also be strengthened: after inserting more than `limit` entries, assert that `shrink()` was actually triggered by verifying `total_keys_num` was updated to a non-zero value reflecting the post-shrink estimate, not the frozen initial 0.

## Proof of Concept
1. Start a CKB node with a non-empty `recent_reject` path and a non-zero `keep_rejected_tx_hashes_count` (the default configuration).
2. In a loop, call `send_transaction` RPC with transactions whose fee rate is below `min_fee_rate`. Each is rejected and written to the `recent_reject` DB via `put()`.
3. Observe via `get_pool_info` that `total_recent_reject_num` always returns 0 (the frozen initial value), confirming `self.total_keys_num` is never updated.
4. Observe the on-disk `recent_reject` directory growing monotonically with no shard being dropped, until disk space is exhausted.

Deterministic unit test:
```rust
let mut rr = RecentReject::build(tmp, 2, 10, -1).unwrap();
for i in 0..20u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}
// With the bug: rr.total_keys_num == 0 (frozen), shrink never called
// After fix:    rr.total_keys_num reflects post-shrink estimate, shrink was called
assert!(rr.total_keys_num > 0, "counter must have been updated by shrink");
```

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L44-52)
```rust
        let total_keys_num = Self::checked_estimate_sum(&estimate_keys_num)?;

        Ok(RecentReject {
            shard_num,
            count_limit,
            ttl,
            db,
            total_keys_num,
        })
```

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

**File:** tx-pool/src/component/tests/recent_reject.rs (L39-39)
```rust
    assert!(recent_reject.total_keys_num < 100);
```
