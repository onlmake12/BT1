### Title
`RecentReject::put()` Never Updates `self.total_keys_num`, Allowing Unbounded Database Growth — (File: `tx-pool/src/component/recent_reject.rs`)

---

### Summary

In `tx-pool/src/component/recent_reject.rs`, the `put()` method computes a new `total_keys_num` as a **local variable** but never writes it back to `self.total_keys_num`. This is a direct Rust analog of the Solidity `memory`-vs-storage bug: the count is updated in a temporary binding, the struct field is never mutated, and the count-based shrink guard is therefore never triggered. An unprivileged transaction sender can submit an unbounded stream of rejected transactions, causing the `RecentReject` RocksDB-with-TTL shard to grow beyond its configured `count_limit`.

---

### Finding Description

`RecentReject` is the tx-pool subsystem that records recently-rejected transactions so that duplicate submissions can be answered quickly. It is backed by a `DBWithTTL` (a RocksDB column-family database with per-entry TTL) and is meant to be bounded by `count_limit`.

The relevant code in `put()`:

```rust
// tx-pool/src/component/recent_reject.rs  lines 55-71
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    let hash_slice = hash.as_slice();
    let shard = self.get_shard(hash_slice).to_string();
    let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
    let json_string = serde_json::to_string(&reject)?;
    self.db.put(&shard, hash_slice, json_string)?;   // ← entry written to DB

    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        if total_keys_num > self.count_limit {
            self.shrink()?;
        }
        // ← self.total_keys_num is NEVER updated here
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

`total_keys_num` is a **local `Option<u64>` binding** produced by `self.total_keys_num.checked_add(1)`. The struct field `self.total_keys_num` is never assigned the new value. Consequently:

1. After every call to `put()`, `self.total_keys_num` remains at its initial value (estimated from RocksDB at startup via `estimate_total_keys_num()`).
2. The condition `total_keys_num > self.count_limit` is evaluated against a permanently stale counter.
3. `shrink()` is never invoked through the count path unless the initial estimate already exceeded `count_limit` at startup.
4. Every rejected transaction is durably written to the shard DB with no effective upper-bound enforcement.

The `estimate_total_keys_num` helper exists and is correct, but it is never called inside `put()` to refresh `self.total_keys_num`. [1](#0-0) 

The struct field is initialized once in `build()`: [2](#0-1) 

The read-only estimator that should have been used to keep the field current: [3](#0-2) 

---

### Impact Explanation

The `RecentReject` database is populated whenever the tx-pool rejects a transaction. Because `self.total_keys_num` is never incremented, the configured `count_limit` is never enforced through the count path. The only backstop is the RocksDB TTL, which expires entries after a fixed wall-clock duration. Between submission and TTL expiry, an attacker can fill the shard with arbitrarily many entries, consuming unbounded disk space and potentially exhausting the node's storage, causing a crash or service disruption.

The reject callback is wired in `shared_builder.rs`: [4](#0-3) 

---

### Likelihood Explanation

Any unprivileged actor can reach this path by submitting transactions via the public JSON-RPC `send_transaction` endpoint or via the P2P transaction relay protocol. Transactions rejected for low fee rate, invalid scripts, capacity errors, or any other reason all invoke `recent_reject.put()`. No special privilege, key, or majority hash-power is required. The attack is cheap: submitting a high volume of low-fee-rate transactions costs almost nothing and each one increments the unguarded DB.

---

### Recommendation

Assign the incremented value back to `self.total_keys_num` inside `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;   // ← persist the updated count
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Additionally, `shrink()` should reset `self.total_keys_num` to the post-shrink estimate so the counter stays accurate across shrink cycles.

---

### Proof of Concept

1. Start a CKB node with a small `count_limit` for `RecentReject` (e.g., 100).
2. Submit 10 000 transactions via `send_transaction` RPC, each with a fee rate below the minimum threshold.
3. Each transaction is rejected; `recent_reject.put()` is called 10 000 times.
4. Because `self.total_keys_num` is never updated, `shrink()` is never called.
5. Inspect the on-disk size of the `recent_reject` RocksDB directory: it contains all 10 000 entries, far exceeding `count_limit`.
6. Repeat until disk exhaustion. [1](#0-0)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L28-53)
```rust
    pub(crate) fn build<P>(
        path: P,
        shard_num: u32,
        count_limit: u64,
        ttl: i32,
    ) -> Result<RecentReject, AnyError>
    where
        P: AsRef<Path>,
    {
        let cf_names: Vec<_> = (0..shard_num).map(|c| c.to_string()).collect();
        let db = DBWithTTL::open_cf(path, cf_names.clone(), ttl)?;
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
    }
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

**File:** tx-pool/src/component/recent_reject.rs (L84-90)
```rust
    fn estimate_total_keys_num(&self) -> Result<u64, AnyError> {
        let estimate_keys_num = (0..self.shard_num)
            .map(|num| self.db.estimate_num_keys_cf(&num.to_string()))
            .collect::<Result<Vec<_>, _>>()?;

        Self::checked_estimate_sum(&estimate_keys_num).map_err(Into::into)
    }
```

**File:** shared/src/shared_builder.rs (L576-601)
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

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```
