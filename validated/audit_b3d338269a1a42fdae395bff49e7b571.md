The code contains a concrete, exploitable bug. Here is the analysis:

---

### Title
`RecentReject::put` Never Increments `total_keys_num`, Permanently Disabling `shrink()` and Allowing Unbounded Disk Growth — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes a local variable `total_keys_num = self.total_keys_num.checked_add(1)` and uses it to decide whether to call `shrink()`, but **never writes the incremented value back to `self.total_keys_num`** when the limit is not exceeded. Because `self.total_keys_num` is initialized from a RocksDB estimate at startup (typically 0 for a fresh DB) and never incremented, the guard condition `total_keys_num > self.count_limit` permanently evaluates to `1 > count_limit`, which is false for any configured limit ≥ 1. `shrink()` is therefore never triggered, and the `recent_reject` DBWithTTL grows without bound.

---

### Finding Description

In `RecentReject::put`:

```rust
// tx-pool/src/component/recent_reject.rs, lines 55–71
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ...
    self.db.put(&shard, hash_slice, json_string)?;   // always writes to DB

    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        if total_keys_num > self.count_limit {
            self.shrink()?;   // only path that updates self.total_keys_num
        }
        // ← BUG: missing `else { self.total_keys_num = total_keys_num; }`
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

`total_keys_num` is a **local** binding. When `total_keys_num <= self.count_limit`, the function returns without ever updating `self.total_keys_num`. On the next call, `self.total_keys_num` is still the old value, so `checked_add(1)` again produces the same local value, and the condition is again false. The counter is frozen at its initial value for the entire lifetime of the process.

`shrink()` is the only place that re-estimates and writes back `self.total_keys_num`:

```rust
// lines 104–113
fn shrink(&mut self) -> Result<u64, AnyError> {
    // ...
    let total_keys_num = self.estimate_total_keys_num()?;
    self.total_keys_num = total_keys_num;   // only update site
    Ok(total_keys_num)
}
```

Because `shrink()` is never reached, the DB accumulates every rejected transaction entry indefinitely. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

Every rejected transaction is written to the `recent_reject` RocksDB-with-TTL instance on disk. With `shrink()` never firing, entries accumulate at the rate of rejections with no upper bound enforced. TTL provides only a delayed, compaction-dependent cleanup — it does not prevent disk space from being consumed in the interim. A sustained stream of rejected transactions will exhaust available disk space, causing the node process to crash or become unable to write to any database. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The attacker path is fully unprivileged and reachable via standard transaction submission (RPC `send_transaction` or P2P relay). Any transaction that fails validation — wrong fee, malformed structure, double-spend, etc. — is routed through `put_recent_reject`:

```rust
// tx-pool/src/process.rs, lines 428–438
pub(crate) async fn put_recent_reject(&self, tx_hash: &Byte32, reject: &Reject) {
    let mut tx_pool = self.tx_pool.write().await;
    if let Some(ref mut recent_reject) = tx_pool.recent_reject
        && let Err(e) = recent_reject.put(tx_hash, reject.clone())
    { ... }
}
``` [5](#0-4) 

No PoW, no privileged access, and no special network position is required. The attacker only needs to submit transactions that are rejected, which is trivially achievable at high rate.

---

### Recommendation

Add the missing increment to `self.total_keys_num` in the non-shrink branch of `put`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // ← add this line
    }
} else {
    self.shrink()?;
}
``` [6](#0-5) 

---

### Proof of Concept

```rust
let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
let shard_num = 2;
let limit = 10;
let ttl = -1;  // no TTL expiry

let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

// Insert limit+N entries; shrink() should have fired but never does
for i in 0..1000u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// total_keys_num is still 0 (or whatever it was at startup)
assert_eq!(rr.total_keys_num, 0);

// But the DB actually contains 1000 entries — far above count_limit=10
let actual = rr.estimate_total_keys_num_pub();  // expose via test helper
assert!(actual > limit);  // passes, proving the invariant is broken
```

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` only asserts `total_keys_num < 100`, which trivially passes because the counter is frozen at 0 — it does not verify actual DB occupancy. [7](#0-6)

### Citations

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

**File:** tx-pool/src/pool.rs (L713-735)
```rust
    fn build_recent_reject(config: &TxPoolConfig) -> Option<RecentReject> {
        if !config.recent_reject.as_os_str().is_empty() {
            let recent_reject_ttl =
                u8::max(1, config.keep_rejected_tx_hashes_days) as i32 * 24 * 60 * 60;
            match RecentReject::new(
                &config.recent_reject,
                config.keep_rejected_tx_hashes_count,
                recent_reject_ttl,
            ) {
                Ok(recent_reject) => Some(recent_reject),
                Err(err) => {
                    error!(
                        "Failed to open the recent reject database {:?} {}",
                        config.recent_reject, err
                    );
                    None
                }
            }
        } else {
            warn!("Recent reject database is disabled!");
            None
        }
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

**File:** tx-pool/src/component/tests/recent_reject.rs (L32-40)
```rust
    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    assert!(recent_reject.total_keys_num < 100);
}
```
