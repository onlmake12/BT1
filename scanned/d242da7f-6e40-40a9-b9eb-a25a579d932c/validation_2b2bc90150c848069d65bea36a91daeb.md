### Title
`RecentReject::put` Never Persists Incremented `total_keys_num`, Disabling `shrink()` and Allowing Unbounded Disk Growth — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes `self.total_keys_num + 1` into a local variable but never writes it back to `self.total_keys_num`. The field stays frozen at its initial RocksDB estimate (typically 0 on a fresh DB), so the `shrink()` guard is never entered. An unprivileged attacker who continuously submits rejected transactions causes the `DBWithTTL` to grow without bound, exhausting disk space.

---

### Finding Description

In `RecentReject::put`:

```rust
// tx-pool/src/component/recent_reject.rs, lines 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

`total_keys_num` is a **local binding** — the result of `checked_add(1)`. `self.total_keys_num` is never updated. After every `put()` call on a fresh DB, `self.total_keys_num` remains `0`, so `0 + 1 = 1 > count_limit` is always `false` (default `count_limit` = 10,000,000), and `shrink()` is never called. [1](#0-0) 

The correct fix requires assigning back: `self.total_keys_num = total_keys_num;` inside the `Some` branch before the limit check.

`shrink()` does correctly update `self.total_keys_num` from a fresh RocksDB estimate when it is called: [2](#0-1) 

But it is never reached.

---

### Impact Explanation

The `DBWithTTL` reject store grows without bound. Each stored entry is a JSON-serialized `PoolTransactionReject` keyed by a 32-byte tx hash. With the default `keep_rejected_tx_hashes_count` of 10,000,000: [3](#0-2) 

...and a 7-day TTL, an attacker submitting unique rejected transactions faster than compaction expires them will exhaust disk space, crashing the node.

---

### Likelihood Explanation

The attack path is fully unprivileged:

1. `Reject::should_recorded()` returns `true` for every variant except `Duplicated`: [4](#0-3) 

2. `Reject::LowFeeRate` satisfies `should_recorded()`. An attacker submits zero-fee transactions via P2P relay or `send_transaction` RPC, which are rejected with `LowFeeRate`: [5](#0-4) 

3. Each rejection flows through `after_process` → `put_recent_reject` → `RecentReject::put`: [6](#0-5) 

4. The existing unit test `test_basic` does **not** catch this bug — it asserts `total_keys_num < 100` after 160 puts, which passes trivially because `total_keys_num` stays at `0` (fresh DB estimate): [7](#0-6) 

---

### Recommendation

In `RecentReject::put`, assign the incremented value back to `self.total_keys_num` before the limit check:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;   // ← missing assignment
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Also update the unit test to assert that `total_keys_num` actually tracks the number of inserted keys and that `shrink()` is triggered when `count_limit` is exceeded.

---

### Proof of Concept

```rust
// Unit test demonstrating the frozen counter
let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
let mut rr = RecentReject::build(tmp_dir.path(), 2, 10, -1).unwrap();
assert_eq!(rr.total_keys_num, 0); // fresh DB

for i in 0u64..10_000 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::LowFeeRate(Default::default(), 1, 0)).unwrap();
}

// Bug: total_keys_num is still 0, not ~10000
assert_eq!(rr.total_keys_num, 0);
// Bug: RocksDB CF contains far more than count_limit=10 keys
// shrink() was never called
```

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

**File:** util/app-config/src/legacy/tx_pool.rs (L57-59)
```rust
fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
}
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/util.rs (L46-52)
```rust
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L39-39)
```rust
    assert!(recent_reject.total_keys_num < 100);
```
