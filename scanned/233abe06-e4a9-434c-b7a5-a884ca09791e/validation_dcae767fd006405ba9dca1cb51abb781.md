The bug is real and concrete. In `RecentReject::put()`, `self.total_keys_num` is **never written back** after the `checked_add(1)` — the result is only bound to a local variable used for the comparison, so the in-memory counter stays frozen at its startup value forever.

---

### Title
`RecentReject::total_keys_num` Never Incremented — `count_limit` Enforcement Completely Bypassed, Enabling Unbounded Disk Growth via Rejected Transactions — (`tx-pool/src/component/recent_reject.rs`)

### Summary
`RecentReject::put()` computes `self.total_keys_num + 1` into a local variable to check against `count_limit`, but never assigns the result back to `self.total_keys_num`. The field stays frozen at its startup estimate for the entire lifetime of the process. Because the counter never advances, `shrink()` is never triggered, and the RocksDB `recent_reject` store grows without bound for as long as the node runs.

### Finding Description

In `tx-pool/src/component/recent_reject.rs`, `put()` reads:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`total_keys_num` here is a **local binding** — the result of `checked_add(1)`. `self.total_keys_num` is never updated. On a fresh node it starts at `0` (from the DB estimate at startup), and every subsequent call to `put()` evaluates `0 + 1 > count_limit`, which is always `false` for the default `count_limit` of `10_000_000`. [2](#0-1) 

`shrink()` is therefore never called during normal operation. The only place `self.total_keys_num` is ever written is inside `shrink()` itself (line 111) and at construction (line 51) — neither of which is reached. [3](#0-2) 

The reject callback is wired directly into `TxPool::limit_size`, which calls `callbacks.call_reject()` for every entry evicted with `Reject::Full`: [4](#0-3) 

The default `keep_rejected_tx_hashes_count` is `10_000_000` and `keep_rejected_tx_hashes_days` is `7`: [5](#0-4) 

### Impact Explanation

An unprivileged peer can submit a continuous stream of valid-looking transactions (via P2P relay or RPC `send_transaction`). When the pool is full (`max_tx_pool_size = 180 MB` by default), each new higher-fee submission evicts lower-fee entries, each of which calls `recent_reject.put()`. Because `total_keys_num` is never incremented, the count-based cap is never enforced. The RocksDB TTL (7 days by default) is the only backstop, but RocksDB TTL only removes entries lazily during compaction — disk space is not reclaimed promptly. A sustained flood of evictions can exhaust disk space on the target node, causing a crash or denial of service.

### Likelihood Explanation

The attack requires no special privilege: submitting transactions is a standard P2P and RPC operation. Filling the pool and triggering evictions is straightforward — submit a large volume of minimum-fee transactions to fill the pool, then submit higher-fee transactions to trigger `limit_size` evictions. Each eviction writes one record to the recent_reject DB. The bug is deterministic and reproducible on any node with `recent_reject` configured (the default).

### Recommendation

In `RecentReject::put()`, assign the incremented value back to `self.total_keys_num` before the comparison:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // <-- missing assignment
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [1](#0-0) 

### Proof of Concept

1. Configure a CKB node with a small `max_tx_pool_size` (e.g., 1 MB) and `keep_rejected_tx_hashes_count = 10`.
2. Fill the pool with minimum-fee transactions.
3. Submit a stream of higher-fee transactions to trigger `limit_size` evictions.
4. Observe that `RecentReject::get_estimate_total_keys_num()` always returns the startup value (0 or the initial DB estimate), while the actual RocksDB key count grows without bound.
5. Assert that after inserting >10 entries the DB contains far more than `count_limit` keys — confirming `shrink()` was never called.

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

**File:** util/app-config/src/legacy/tx_pool.rs (L53-59)
```rust
fn default_keep_rejected_tx_hashes_days() -> u8 {
    7
}

fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
}
```
