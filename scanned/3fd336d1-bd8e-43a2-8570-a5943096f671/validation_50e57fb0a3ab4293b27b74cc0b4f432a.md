### Title
`RecentReject::put` Never Increments `total_keys_num`, Allowing Unbounded DB Growth via Repeated Rejection Spam — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes a local variable `total_keys_num` as `self.total_keys_num + 1` but never writes it back to `self.total_keys_num`. As a result, `shrink()` is never triggered, and the RocksDB-backed recent-reject store grows without bound. Any unprivileged actor who can cause repeated transaction rejections (e.g., via RBF, low-fee-rate submissions, or any other non-`Duplicated` rejection) can drive unbounded disk usage and I/O load on a CKB node.

---

### Finding Description

The root cause is a one-line omission in `RecentReject::put`:

```rust
// tx-pool/src/component/recent_reject.rs, lines 55–71
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ...
    self.db.put(&shard, hash_slice, json_string)?;   // ← entry written to DB

    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        // `total_keys_num` is a LOCAL variable; self.total_keys_num is NEVER updated
        if total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
``` [1](#0-0) 

`self.total_keys_num` is initialized once at startup from a RocksDB key-count estimate: [2](#0-1) 

After that, it is only ever updated inside `shrink()`: [3](#0-2) 

Because `self.total_keys_num` is never incremented in `put()`, the guard condition `total_keys_num > self.count_limit` evaluates to `(self.total_keys_num + 1) > count_limit`. For a fresh node where `self.total_keys_num` starts at 0, this is always `1 > count_limit`, which is false for any reasonable `count_limit`. `shrink()` is therefore never called, and the DB grows without bound.

**The `should_recorded()` gate does not protect against this.** It only excludes `Reject::Duplicated`: [4](#0-3) 

Every other reject variant — including `Reject::RBFRejected`, `Reject::LowFeeRate`, `Reject::Verification`, `Reject::Invalidated`, etc. — passes the gate and is written to the DB.

**Clarification on the RBF path:** The question states "Reject::Invalidated via RBF." This is factually imprecise. RBF-replaced transactions receive `Reject::RBFRejected`, not `Reject::Invalidated`. `Reject::Invalidated` is issued for txs evicted by pool-size limits in `submit_entry`. Both variants pass `should_recorded()` and both are recorded via the same `put()` call, so both contribute to unbounded growth. The RBF path specifically: [5](#0-4) 

The registered reject callback unconditionally calls `recent_reject.put()` for any `should_recorded()` reject: [6](#0-5) 

---

### Impact Explanation

- The recent-reject RocksDB store grows without bound, consuming unbounded disk space.
- Each `put()` also triggers a RocksDB write (WAL + SST compaction pressure), causing sustained I/O load proportional to the rejection rate.
- The `count_limit` invariant (configured via `keep_rejected_tx_hashes_count`) is completely unenforced at runtime.
- Node operators cannot rely on the configured limit to bound storage consumption.

---

### Likelihood Explanation

The attack is low-cost and requires no special privilege:

1. **Via RBF:** Submit tx A (valid, sufficient fee). Submit tx B replacing A with a higher fee. A is recorded as `Reject::RBFRejected`. Submit tx C replacing B. Repeat. Each iteration requires a strictly higher fee, so cost grows, but the DB grows by one entry per iteration with no shrink ever occurring.

2. **Via low-fee-rate spam (cheaper):** Submit many distinct transactions with fee rates just below `min_fee_rate`. Each gets `Reject::LowFeeRate`, passes `should_recorded()`, and is written to the DB. No increasing-fee constraint applies here. This is accessible via both P2P relay and the `send_transaction` RPC.

The TTL (`keep_rejected_tx_hashes_days`) provides partial mitigation — entries expire after N days — but does not prevent the attack if the submission rate exceeds the expiry rate.

---

### Recommendation

In `put()`, assign the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ...
    self.db.put(&shard, hash_slice, json_string)?;

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // ← missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

---

### Proof of Concept

```rust
// Demonstrates that total_keys_num is never incremented and shrink() is never called.
// Run with: cargo test -p ckb-tx-pool test_total_keys_num_never_incremented
#[test]
fn test_total_keys_num_never_incremented() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 10u64;
    let ttl = -1;

    let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();
    assert_eq!(rr.total_keys_num, 0);

    // Insert limit*10 entries — shrink() should have fired multiple times if the
    // counter were correct, but it never fires because total_keys_num stays at 0.
    for i in 0..(limit * 10) {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        rr.put(&key, Reject::LowFeeRate(FeeRate::zero(), 0, 0)).unwrap();
        // total_keys_num is always 0 after each put(); shrink() is never called.
        assert_eq!(rr.total_keys_num, 0,
            "total_keys_num should have been incremented but was not");
    }

    // The actual DB key count far exceeds count_limit.
    let actual = rr.estimate_total_keys_num_pub(); // expose via test helper
    assert!(actual > limit,
        "DB has {} keys, exceeding count_limit={}", actual, limit);
}
```

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

**File:** tx-pool/src/component/recent_reject.rs (L104-112)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
```

**File:** util/types/src/core/tx_pool.rs (L99-102)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L225-231)
```rust
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
```

**File:** shared/src/shared_builder.rs (L579-585)
```rust
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }
```
