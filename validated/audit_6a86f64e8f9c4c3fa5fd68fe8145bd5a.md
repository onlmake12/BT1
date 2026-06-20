### Title
`RecentReject::put` Never Increments `total_keys_num` — Shrink Threshold Permanently Frozen, Enabling Unbounded Disk Growth via Rejected Tx Spam — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes a local `total_keys_num = self.total_keys_num + 1` to check the shrink threshold, but **never writes that value back to `self.total_keys_num`**. On a fresh node (empty DB), `self.total_keys_num` is initialized to `0` from RocksDB's `estimate_num_keys_cf` and stays `0` forever. Every subsequent call to `put` evaluates `0 + 1 > count_limit`, which is always `false` for any `count_limit ≥ 1`, so `shrink()` is never triggered. An unprivileged attacker who continuously submits transactions that are rejected with any non-`Duplicated` reason causes the `recent_reject` RocksDB to grow without bound.

---

### Finding Description

The root cause is a missing assignment in `RecentReject::put`:

```rust
// tx-pool/src/component/recent_reject.rs, lines 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num = total_keys_num; is MISSING
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`total_keys_num` is a **local binding** holding `self.total_keys_num + 1`. The field `self.total_keys_num` is never updated to this value. The only place `self.total_keys_num` is ever written after construction is inside `shrink()`:

```rust
// lines 110-111
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
``` [2](#0-1) 

But `shrink()` is only reachable when the threshold check fires — which it never does because the counter never advances.

At construction, `total_keys_num` is seeded from `estimate_num_keys_cf` across all column families: [3](#0-2) 

On a fresh node this returns `0`. The counter then stays `0` for the entire lifetime of the process, so every `put` evaluates `0 + 1 > count_limit` → `false`, and `shrink()` is never called.

The question's framing about "estimate drift" is a secondary concern; the primary bug is that the counter is simply never incremented at all.

---

### Impact Explanation

Every rejected transaction whose `Reject` variant is not `Duplicated` passes `should_recorded()`:

```rust
pub fn should_recorded(&self) -> bool {
    !matches!(self, Reject::Duplicated(..))
}
``` [4](#0-3) 

This covers `Full`, `ExceededMaximumAncestorsCount`, `Expiry`, `RBFRejected`, `Resolve`, `Verification`, `Malformed`, `LowFeeRate`, `Invalidated`, and `ExceededTransactionSizeLimit`. Each such rejection writes one key to the `recent_reject` RocksDB via `put_recent_reject`: [5](#0-4) 

Because `shrink()` is never triggered, the DB grows without bound. The configured `keep_rejected_tx_hashes_count` limit is completely ineffective. Sustained spam of cheap-to-produce rejected transactions (e.g., transactions referencing dead cells → `Reject::Resolve`) fills the node's disk, degrading I/O and eventually crashing the node when disk space is exhausted.

---

### Likelihood Explanation

The attacker entry point is standard transaction submission — either via the `send_transaction` RPC or P2P relay. No privileged access, no PoW, no key material is required. Producing transactions that resolve to `Reject::Resolve` (dead/missing cell reference) or `Reject::Verification` (invalid script) is trivially cheap. The bug is deterministic and reproducible on any fresh node with `recent_reject` enabled (the default configuration).

---

### Recommendation

Add the missing write-back inside `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;   // ← add this line
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

After `shrink()` returns, `self.total_keys_num` is already refreshed from the RocksDB estimate, so no further adjustment is needed in that branch.

---

### Proof of Concept

```rust
// Reproduces with shard_num=2, count_limit=10, ttl=-1 (no expiry)
let mut rr = RecentReject::build(tmp_dir.path(), 2, 10, -1).unwrap();
assert_eq!(rr.total_keys_num, 0); // fresh DB

for i in 0u64..50 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Full("pool full".into())).unwrap();
}

// total_keys_num is still 0 — shrink was never called
assert_eq!(rr.total_keys_num, 0);

// But the DB actually contains 50 keys — count_limit=10 was violated
let actual: u64 = rr.estimate_total_keys_num_pub(); // expose via test helper
assert!(actual > 10, "DB has {} keys, limit is 10", actual);
```

The existing test `test_basic` in `tx-pool/src/component/tests/recent_reject.rs` asserts `recent_reject.total_keys_num < 100` after 160 puts with `limit=100`, but this assertion passes vacuously because `total_keys_num` is always `0` — it does not verify that the DB itself is bounded. [6](#0-5)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L39-52)
```rust
        let estimate_keys_num = cf_names
            .iter()
            .map(|cf| db.estimate_num_keys_cf(cf))
            .collect::<Result<Vec<_>, _>>()?;

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

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L428-438)
```rust
    pub(crate) async fn put_recent_reject(&self, tx_hash: &Byte32, reject: &Reject) {
        let mut tx_pool = self.tx_pool.write().await;
        if let Some(ref mut recent_reject) = tx_pool.recent_reject
            && let Err(e) = recent_reject.put(tx_hash, reject.clone())
        {
            error!(
                "Failed to record recent_reject {} {} {}",
                tx_hash, reject, e
            );
        }
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
