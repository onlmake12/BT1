### Title
`RecentReject::put` Never Updates `total_keys_num`, Making the Count Limit Permanently Unenforceable — (`File: tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes `self.total_keys_num + 1` into a **local shadow variable** but never writes it back to `self.total_keys_num`. As a result, the field stays frozen at its initial DB-estimated value (typically 0 on a fresh node) for the entire lifetime of the process. The count-limit guard `if total_keys_num > self.count_limit` therefore compares the constant value `1` against `count_limit` on every call and never triggers `shrink()`. Any unprivileged tx-pool submitter can spam rejected transactions to grow the `recent_reject` RocksDB column families without bound, exhausting disk space.

---

### Finding Description

`RecentReject` is a sharded RocksDB-with-TTL store that records recently rejected transactions so that peers can query why a transaction was rejected. It is configured with a `count_limit` (default `keep_rejected_tx_hashes_count`, e.g. 20 000) and a TTL. The intent is that once the total number of stored entries exceeds `count_limit`, `shrink()` drops a random shard to reclaim space.

The flaw is in `put()`:

```rust
// tx-pool/src/component/recent_reject.rs  lines 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

`self.total_keys_num.checked_add(1)` produces a **new value bound to the local name `total_keys_num`**; `self.total_keys_num` is never assigned. After `put()` returns, `self.total_keys_num` is identical to what it was before the call.

On a fresh node the field is initialised to the DB estimate (0). Every subsequent call to `put()` therefore evaluates `0 + 1 = 1 > count_limit`, which is always `false`, so `shrink()` is never invoked and the DB grows without any bound.

`shrink()` does update `self.total_keys_num` by re-estimating from the DB, but it is unreachable through the broken guard.

The analogous pattern from the Olympus report: `totalEndorsementsForProposal` was incremented when a user endorsed a proposal but was never decremented when the user's votes were burned, making the accumulated value permanently stale. Here, `total_keys_num` is supposed to be incremented on every `put` but the increment is discarded, making the accumulated value permanently stale in the opposite direction — it never grows, so the limit is never crossed.

---

### Impact Explanation

An attacker who can cause transactions to be rejected (trivially achievable by submitting transactions with invalid scripts, double-spends, or fee-rate below minimum) can call `send_transaction` via RPC or relay transactions via P2P indefinitely. Each rejection records an entry in the `recent_reject` DB. Because `shrink()` is never called, the DB grows without bound, consuming disk space until the node's storage is exhausted, causing a denial-of-service against the node operator.

The `total_recent_reject_num` field exposed by the terminal RPC also reads `get_estimate_total_keys_num()` which returns the frozen `total_keys_num`, so operators see a permanently stale (near-zero) count and receive no warning that the DB is growing.

---

### Likelihood Explanation

The entry path requires no privilege: any RPC caller or P2P peer can submit transactions. Rejected transactions are a normal operational occurrence (double-spends, low-fee-rate, invalid scripts). A targeted attacker needs only to submit a stream of unique transactions that fail validation. The TTL provides partial mitigation (entries expire after `keep_rejected_tx_hashes_days` days), but with a high enough submission rate the disk can be exhausted before TTL-based compaction reclaims space.

---

### Recommendation

In `put()`, write the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    let hash_slice = hash.as_slice();
    let shard = self.get_shard(hash_slice).to_string();
    let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
    let json_string = serde_json::to_string(&reject)?;
    self.db.put(&shard, hash_slice, json_string)?;

    match self.total_keys_num.checked_add(1) {
        Some(n) => {
            self.total_keys_num = n;          // ← missing assignment
            if n > self.count_limit {
                self.shrink()?;
            }
        }
        None => {
            self.shrink()?;
        }
    }
    Ok(())
}
```

---

### Proof of Concept

The existing test in `tx-pool/src/component/tests/recent_reject.rs` inadvertently demonstrates the bug: it inserts 80 entries twice (same keys, so they overwrite) against a limit of 100, then asserts `total_keys_num < 100`. The assertion passes trivially because `total_keys_num` is never updated from 0, not because the limit was correctly enforced.

A correct demonstration:

```rust
#[test]
fn test_count_limit_never_enforced() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 10;   // small limit
    let ttl = -1;

    let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();
    assert_eq!(rr.total_keys_num, 0);

    // Insert 50 unique entries — 5× over the limit
    for i in 0u64..50 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        rr.put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    // total_keys_num is still 0 — the limit was never enforced
    assert_eq!(rr.total_keys_num, 0,
        "total_keys_num was never incremented; shrink() was never called");

    // The DB actually contains all 50 entries
    let real_count = rr.estimate_total_keys_num_pub(); // expose for test
    assert!(real_count > limit,
        "DB has {} entries, far exceeding limit {}", real_count, limit);
}
```

**Root cause lines:** [1](#0-0) 

`self.total_keys_num` is set only at construction time: [2](#0-1) 

`shrink()` does update `self.total_keys_num` but is unreachable because the guard never fires: [3](#0-2) 

The stale value is what the RPC exposes as `total_recent_reject_num`: [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/component/recent_reject.rs (L80-82)
```rust
    pub fn get_estimate_total_keys_num(&self) -> u64 {
        self.total_keys_num
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
