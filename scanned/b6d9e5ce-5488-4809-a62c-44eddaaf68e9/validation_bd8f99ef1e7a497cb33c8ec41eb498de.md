Audit Report

## Title
`RecentReject::put` Discards `total_keys_num` Increment, Making Count Limit Permanently Unenforceable — (File: tx-pool/src/component/recent_reject.rs)

## Summary
In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local shadow variable `total_keys_num` but is never written back to `self.total_keys_num`. The field remains frozen at its DB-estimated initial value (typically 0 on a fresh node) for the entire process lifetime. The guard `if total_keys_num > self.count_limit` therefore always evaluates `1 > count_limit`, which is permanently false, so `shrink()` is never invoked and the `recent_reject` RocksDB store grows without bound.

## Finding Description
`RecentReject` is a sharded RocksDB-with-TTL store for recording rejected transactions. It is configured with `count_limit` (default `keep_rejected_tx_hashes_count`, e.g. 20,000) and a TTL. When the total stored entries exceed `count_limit`, `shrink()` is supposed to drop a random shard to reclaim space.

The flaw is at `tx-pool/src/component/recent_reject.rs` lines 62–69:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

`self.total_keys_num.checked_add(1)` produces a new value bound to the local name `total_keys_num`; `self.total_keys_num` is never assigned. After `put()` returns, `self.total_keys_num` is identical to what it was before the call. [1](#0-0) 

On a fresh node, `total_keys_num` is initialized from the DB estimate (0 for an empty DB): [2](#0-1) 

Every subsequent call to `put()` evaluates `0 + 1 = 1 > count_limit`, which is always `false` for any reasonable `count_limit` (e.g. 20,000). `shrink()` is therefore unreachable through the broken guard. `shrink()` does correctly update `self.total_keys_num` by re-estimating from the DB, but it is never called: [3](#0-2) 

The existing test at line 39 inadvertently confirms the bug: it asserts `recent_reject.total_keys_num < 100` after 160 insertions against a limit of 100, and the assertion passes trivially because `total_keys_num` was never incremented from 0, not because the limit was correctly enforced: [4](#0-3) 

The stale value is also what the RPC exposes as `total_recent_reject_num`, giving operators no warning: [5](#0-4) 

## Impact Explanation
An attacker who can cause transactions to be rejected (trivially achievable by submitting transactions with invalid scripts, double-spends, or fee-rate below minimum) can call `send_transaction` via RPC or relay transactions via P2P indefinitely. Each rejection records an entry in the `recent_reject` DB. Because `shrink()` is never called, the DB grows without bound, consuming disk space until the node's storage is exhausted. This constitutes a **High** impact: **Vulnerabilities which could easily crash a CKB node** (10001–15000 points). The TTL provides partial mitigation (entries expire after `keep_rejected_tx_hashes_days` days), but at a sufficiently high submission rate, disk exhaustion occurs before TTL-based compaction reclaims space.

## Likelihood Explanation
The entry path requires no privilege: any RPC caller or P2P peer can submit transactions. Rejected transactions are a normal operational occurrence. A targeted attacker needs only to submit a stream of unique transactions that fail validation (e.g., transactions with invalid scripts or below-minimum fee rates). The attack is repeatable and requires no special knowledge of the node's internal state.

## Recommendation
In `put()`, write the incremented value back to `self.total_keys_num` before the limit check:

```rust
match self.total_keys_num.checked_add(1) {
    Some(n) => {
        self.total_keys_num = n;  // ← missing assignment
        if n > self.count_limit {
            self.shrink()?;
        }
    }
    None => {
        self.shrink()?;
    }
}
```

## Proof of Concept
The existing test in `tx-pool/src/component/tests/recent_reject.rs` already demonstrates the bug: after 160 insertions against a limit of 100, `total_keys_num` is still 0 and the assertion `total_keys_num < 100` passes trivially.

A targeted reproduction:
1. Build `RecentReject` with `count_limit = 10`.
2. Insert 50 unique rejected transactions via `put()`.
3. Assert `rr.total_keys_num == 0` — it will pass, proving the counter was never incremented.
4. Query the actual DB key count via `estimate_total_keys_num()` — it will return ~50, far exceeding the limit of 10, proving `shrink()` was never called. [6](#0-5)

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

**File:** tx-pool/src/component/recent_reject.rs (L55-71)
```rust
    pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
        let hash_slice = hash.as_slice();
        let shard = self.get_shard(hash_slice).to_string();
        let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
        let json_string = serde_json::to_string(&reject)?;
        self.db.put(&shard, hash_slice, json_string)?;

        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
        Ok(())
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L104-113)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
    }
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L32-39)
```rust
    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    assert!(recent_reject.total_keys_num < 100);
```

**File:** tx-pool/src/service.rs (L1100-1106)
```rust
    async fn get_total_recent_reject_num(&self) -> Option<u64> {
        let tx_pool = self.tx_pool.read().await;
        tx_pool
            .recent_reject
            .as_ref()
            .map(|r| r.get_estimate_total_keys_num())
    }
```
