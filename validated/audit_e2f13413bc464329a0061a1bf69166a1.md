### Title
`RecentReject::put` Never Increments `self.total_keys_num`, Allowing Unbounded DB Growth via `Reject::Full` Evictions — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` computes a local `total_keys_num` via `checked_add(1)` but **never writes it back to `self.total_keys_num`**. The shrink guard therefore always compares `initial_estimate + 1` against `count_limit`, never triggering `shrink()` on a fresh DB. An unprivileged attacker who keeps the pool at capacity and floods it with low-fee transactions causes every eviction to write a `Reject::Full` record into the RocksDB-backed reject store with no bound, leading to disk exhaustion.

---

### Finding Description

In `RecentReject::put`:

```rust
// tx-pool/src/component/recent_reject.rs  lines 62-69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a **local binding**; `self.total_keys_num` is never reassigned. On a fresh node (initial estimate = 0), every call evaluates `1 > count_limit` — always false for any reasonable limit — and returns without shrinking. The field stays at 0 permanently until a restart re-estimates it from RocksDB. [1](#0-0) 

The attacker-reachable path is fully wired:

1. `limit_size()` evicts entries as `Reject::Full` and calls `callbacks.call_reject()`. [2](#0-1) 

2. The registered reject callback checks `reject.should_recorded()` — which returns `true` for every variant except `Reject::Duplicated` — and calls `recent_reject.put()`. [3](#0-2) [4](#0-3) 

3. `put()` writes the record to RocksDB but leaves `self.total_keys_num` unchanged, so `shrink()` is never triggered. [5](#0-4) 

---

### Impact Explanation

Every `Reject::Full` eviction writes a JSON-serialised reject record to the TTL-backed RocksDB column family. With `self.total_keys_num` frozen at its startup value, the `count_limit` guard is permanently bypassed. An attacker who sustains a submission rate equal to the eviction rate can grow the reject DB at the rate of one record per submitted transaction, bounded only by disk capacity. The TTL provides a soft ceiling only if the submission rate stays below `count_limit / TTL`; a sustained flood exceeds this.

Additionally, the reject callback is invoked while the tx-pool write lock is held (`tx_pool: &mut TxPool`), so each RocksDB write extends the lock hold time under load. [6](#0-5) 

---

### Likelihood Explanation

The preconditions are reachable by any P2P peer:
- Fill the pool with high-fee transactions (standard mempool behaviour).
- Submit a continuous stream of low-fee transactions; each is evicted as `Reject::Full` and recorded.

No privileged access, key material, or majority hashpower is required. The bug is deterministic and locally testable.

---

### Recommendation

Assign the incremented value back to `self.total_keys_num` inside `put()`:

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

After `shrink()` the field is already refreshed via `estimate_total_keys_num()`, so no change is needed in that branch. [7](#0-6) 

---

### Proof of Concept

```
1. Start a CKB node with default config (count_limit = N, TTL = T).
2. Fill the tx-pool to max_tx_pool_size with high-fee-rate transactions.
3. In a loop, submit M >> N low-fee-rate transactions via P2P/RPC.
4. Each submission triggers limit_size() → Reject::Full → recent_reject.put().
5. Observe: recent_reject DB on disk grows to M records (>> count_limit).
6. Observe: self.total_keys_num remains 0 (or initial estimate) throughout.
7. Assert: DB size exceeds count_limit * avg_record_size bytes.
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

**File:** tx-pool/src/pool.rs (L314-323)
```rust
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** shared/src/shared_builder.rs (L576-585)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }
```

**File:** tx-pool/src/callback.rs (L65-69)
```rust
    pub fn call_reject(&self, tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject) {
        if let Some(call) = &self.reject {
            call(tx_pool, entry, reject)
        }
    }
```
