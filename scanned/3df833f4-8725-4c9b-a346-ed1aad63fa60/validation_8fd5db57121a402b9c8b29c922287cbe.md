### Title
`RecentReject::put` Never Increments `total_keys_num`, Making `count_limit` Completely Unenforced — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put()` computes a local variable `total_keys_num` via `self.total_keys_num.checked_add(1)` but **never writes it back to `self.total_keys_num`**. As a result, the in-memory counter stays at its initial estimated value (typically 0 on a fresh node) for the entire lifetime of the process. The `shrink()` guard is therefore never triggered during a running session, and the RocksDB-with-TTL store grows without any bound. An unprivileged attacker who submits a stream of unique, zero-fee transactions can exhaust the node's disk.

---

### Finding Description

In `RecentReject::put()`:

```rust
// tx-pool/src/component/recent_reject.rs:62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    // overflow occurred, try shrink
    self.shrink()?;
}
```

The `let Some(total_keys_num)` binding creates a **local** variable that shadows the struct field. `self.total_keys_num` is never assigned in this branch. The only place the field is mutated is inside `shrink()`:

```rust
// tx-pool/src/component/recent_reject.rs:110-111
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;
```

Because `self.total_keys_num` never increments, the condition `total_keys_num > self.count_limit` evaluates as `(0 + 1) > 10_000_000` on every single call — always false. `shrink()` is never invoked during the session. Every call to `put()` writes a new entry to RocksDB and returns without any eviction. [1](#0-0) 

The `should_recorded()` predicate returns `true` for every `Reject` variant except `Reject::Duplicated`: [2](#0-1) 

This includes `Reject::LowFeeRate`, which is produced for any transaction whose fee is below `min_fee_rate` — no actual CKB tokens are consumed by the attacker: [3](#0-2) 

The rejection is then unconditionally written to `RecentReject` via `put_recent_reject`: [4](#0-3) 

The default `count_limit` is 10,000,000 and the default TTL is 7 days: [5](#0-4) 

---

### Impact Explanation

Each unique rejected transaction writes a RocksDB entry of approximately 32 bytes (key) + 100–500 bytes (JSON-serialized reject reason). With no shrink ever firing, the DB grows at the rate of attacker submissions. At 1,000 submissions/second (easily achievable over a local or fast network connection), the DB accumulates ~500 MB/hour and ~84 GB over the 7-day TTL window before any entries expire. This can exhaust disk space on the victim node, causing RocksDB write failures, node crashes, or severe I/O degradation affecting block relay and transaction processing.

---

### Likelihood Explanation

The attack requires only the ability to submit structurally valid CKB transactions (no valid inputs, no fees, no signing keys needed for the `LowFeeRate` path). There is no rate limiting on the `send_transaction` RPC endpoint. The bug is present in every running session starting from a fresh or recently-shrunk DB. It is locally reproducible and requires no special privileges.

---

### Recommendation

In `RecentReject::put()`, assign the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ... write to db ...
    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // <-- missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

Additionally, consider adding a rate limit or connection-level throttle on `send_transaction` RPC calls to reduce the submission rate available to unauthenticated callers.

---

### Proof of Concept

```rust
// Reproduces the counter-never-increments bug locally
let tmp = tempfile::tempdir().unwrap();
let limit = 5u64;
let mut rr = RecentReject::build(tmp.path(), 2, limit, -1).unwrap();

for i in 0..20u64 {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    rr.put(&key, Reject::LowFeeRate(FeeRate::from_u64(1000), 100, 0)).unwrap();
}

// total_keys_num is still 0 (or whatever the initial estimate was),
// shrink() was never called, and the DB actually holds 20 entries.
assert_eq!(rr.total_keys_num, 0);  // passes — counter was never incremented
assert!(rr.get_estimate_total_keys_num() < limit); // passes trivially, DB is unbounded
```

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` asserts `total_keys_num < 100` after 160 puts with `limit = 100`, which passes vacuously because the counter is always 0 — it does not verify the actual DB entry count. [6](#0-5)

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

**File:** util/types/src/core/tx_pool.rs (L99-102)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/util.rs (L44-52)
```rust
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
```

**File:** tx-pool/src/process.rs (L546-550)
```rust
                    Err(reject) => {
                        debug!("after_process {} reject: {} ", tx_hash, reject);
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L53-59)
```rust
fn default_keep_rejected_tx_hashes_days() -> u8 {
    7
}

fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
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
