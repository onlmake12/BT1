### Title
`RecentReject::put()` Never Increments `total_keys_num`, Disabling `shrink()` and Allowing Unbounded DB Growth — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put()` contains a missing assignment: `self.total_keys_num` is never incremented in the normal (non-shrink) path. The counter permanently stays at its initialization value (0 for a fresh DB), so `shrink()` is never triggered, and the reject DB grows without bound. A remote attacker can exploit this by continuously submitting transactions that get rejected, exhausting disk space.

---

### Finding Description

In `put()`, the code computes `self.total_keys_num.checked_add(1)` and binds the result to a **local variable** `total_keys_num`, but never assigns it back to `self.total_keys_num`:

```rust
// tx-pool/src/component/recent_reject.rs, lines 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`self.total_keys_num` is only ever written in two places:

1. At construction (`build()`), set from `estimate_num_keys_cf` — returns 0 for a fresh DB. [2](#0-1) 

2. Inside `shrink()`, reset to the post-drop estimate. [3](#0-2) 

Because `self.total_keys_num` starts at 0 and is never incremented, every call to `put()` evaluates `0 + 1 = 1`. With the default `count_limit` of **10,000,000**, the condition `1 > 10_000_000` is always false, so `shrink()` is never called. [4](#0-3) 

The question's framing about `estimate_num_keys_cf` approximation causing drift is a secondary concern. The primary bug is the missing `self.total_keys_num = total_keys_num;` assignment in the non-shrink branch of `put()`.

---

### Impact Explanation

- The reject DB (`DBWithTTL`) grows without bound. Each rejected tx writes a JSON-serialized `PoolTransactionReject` record.
- TTL (`keep_rejected_tx_hashes_days`, default 7 days) provides partial mitigation: RocksDB deletes expired entries **only during compaction**, not immediately. An attacker submitting rejected txs faster than compaction runs can exhaust disk space within the TTL window.
- Disk exhaustion causes the node to crash or become unable to write to any DB, halting block processing and sync — a full node DoS.

---

### Likelihood Explanation

- The attacker path is fully unprivileged: submit transactions via P2P relay or the `send_transaction` RPC. Any tx that fails validation (e.g., double-spend, malformed, insufficient fee) triggers `put_recent_reject` → `RecentReject::put()`. [5](#0-4) 
- No PoW, no key, no special role required.
- The bug is deterministic and reproducible on any fresh node.

---

### Recommendation

Add the missing assignment in the non-shrink branch of `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // ← add this line
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

After `shrink()`, `self.total_keys_num` is already reset via `estimate_total_keys_num()`, so no change is needed in that branch.

---

### Proof of Concept

```rust
// Reproduces the counter-never-increments bug
let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
let count_limit = 10u64;
let mut rr = RecentReject::build(tmp_dir.path(), 2, count_limit, -1).unwrap();

// Insert 100 entries — far beyond count_limit
for i in 0..100u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::Malformed(i.to_string(), Default::default())).unwrap();
}

// total_keys_num is still 0 (or initial estimate), shrink() was never called
assert_eq!(rr.total_keys_num, 0);  // passes — counter never moved
// True DB key count is ~100, far above count_limit=10
// shrink() was never triggered
```

The counter stays at 0 throughout, `shrink()` is never invoked, and the DB holds all 100 entries despite `count_limit = 10`. [6](#0-5)

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

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L57-59)
```rust
fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
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
