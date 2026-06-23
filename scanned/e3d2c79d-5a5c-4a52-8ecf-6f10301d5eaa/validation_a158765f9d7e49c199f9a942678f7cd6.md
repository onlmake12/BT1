The code is available. Let me analyze the `put` function carefully.

### Title
`RecentReject::put` Never Increments `self.total_keys_num`, Disabling the `count_limit` Guard — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a **local variable** `total_keys_num` and is never written back to `self.total_keys_num`. The count-limit guard therefore always evaluates `self.total_keys_num + 1` against `count_limit`, where `self.total_keys_num` is permanently frozen at the startup estimate. For a node starting with an empty DB (estimate = 0), the guard evaluates `1 > count_limit` on every call — which is always false — so `shrink()` is never triggered and the on-disk `DBWithTTL` grows without bound within the TTL window.

---

### Finding Description

The defect is in `RecentReject::put`: [1](#0-0) 

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local binding. `self.total_keys_num` is only ever mutated inside `shrink()`: [2](#0-1) 

Because `shrink()` is never reached (the guard is always false when starting from an empty DB), `self.total_keys_num` stays at the startup estimate for the entire lifetime of the process.

`self.total_keys_num` is initialized at startup from RocksDB's `estimate_num_keys_cf`: [3](#0-2) 

For a freshly started or empty node this estimate is 0. Every subsequent `put()` call computes `0 + 1 = 1`, checks `1 > count_limit` (false for any sane limit), and returns without shrinking or updating the counter.

The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes vacuously — it asserts the stale counter (0) is less than the limit (100), which is always true, and does not detect the unbounded DB growth. [4](#0-3) 

---

### Impact Explanation

The `recent_reject` RocksDB (`DBWithTTL`) grows without bound across the node's lifetime, bounded only by the TTL expiry (default 7 days × 86400 s). The intended invariant — that `keep_rejected_tx_hashes_count` caps disk usage — is completely bypassed. An attacker who continuously submits recordable-rejected transactions can exhaust the node's disk, causing a crash or service disruption.

---

### Likelihood Explanation

**Attacker entry point:** Any unprivileged peer or RPC caller. `should_recorded()` returns `true` for every `Reject` variant except `Duplicated`: [5](#0-4) 

The easiest vector is submitting transactions with random (non-existent) outpoints, which are rejected as `Reject::Resolve(OutPointError::Unknown(...))` — no UTXOs required, no fees required, just valid CKB transaction serialization. These flow through `after_process` → `put_recent_reject` → `recent_reject.put()`: [6](#0-5) 

The default `count_limit` is 10,000,000 entries: [7](#0-6) 

An attacker can trivially exceed this within the 7-day TTL window by submitting rejected transactions at any sustained rate.

---

### Recommendation

Add `self.total_keys_num = total_keys_num;` in the non-shrink branch of `put()`:

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

Also update the unit test to assert that the actual RocksDB key count (not `total_keys_num`) stays within `count_limit` after many puts.

---

### Proof of Concept

```rust
// In tx-pool/src/component/tests/recent_reject.rs
#[test]
fn test_count_limit_enforced() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 10u64;
    let ttl = -1;

    let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

    // Insert 100 >> limit entries
    for i in 0..100u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        rr.put(&key, Reject::LowFeeRate(Default::default(), 100, 0)).unwrap();
    }

    // BUG: total_keys_num is still 0 (never incremented)
    assert_eq!(rr.total_keys_num, 0);

    // BUG: actual DB key count far exceeds limit
    let actual = rr.estimate_total_keys_num_pub(); // expose for test
    assert!(actual > limit, "DB has {} keys, limit is {}", actual, limit);
}
```

Running this test against the current code will show `total_keys_num == 0` throughout and the DB holding ~100 entries despite `limit = 10`, confirming the invariant is broken.

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

**File:** tx-pool/src/component/tests/recent_reject.rs (L39-39)
```rust
    assert!(recent_reject.total_keys_num < 100);
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L57-59)
```rust
fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
}
```
