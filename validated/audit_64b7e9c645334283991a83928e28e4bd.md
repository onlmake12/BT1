### Title
`RecentReject::put()` Never Increments `total_keys_num`, Silently Disabling the Count Limit — (`File: tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject` is the tx-pool subsystem that persists recently-rejected transaction hashes to a RocksDB-with-TTL store. It is supposed to enforce a configurable `count_limit` (default `keep_rejected_tx_hashes_count`, typically 100 000) by calling `shrink()` when the stored count exceeds the limit. However, `put()` computes `self.total_keys_num + 1` into a **local** variable and never writes it back to `self.total_keys_num`. As a result the in-memory counter is permanently frozen at its initial value (an RocksDB estimate that starts at 0 on a fresh node), the `> count_limit` guard is never satisfied, and the on-disk store grows without bound — limited only by the TTL expiry mechanism.

---

### Finding Description

In `RecentReject::build()` the field `total_keys_num` is seeded from RocksDB's `rocksdb.estimate-num-keys` property — an approximation that returns 0 on a freshly-created column family. [1](#0-0) 

In `put()`, the guard that is supposed to trigger `shrink()` reads:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [2](#0-1) 

The result of `checked_add(1)` is bound to the **local** `total_keys_num`; `self.total_keys_num` is never assigned. On a fresh node `self.total_keys_num` stays at `0` forever, so the condition `0 + 1 > count_limit` is always `false`, and `shrink()` is never called. `self.total_keys_num` is only updated inside `shrink()` itself: [3](#0-2) 

Because `shrink()` is never reached, the counter is never refreshed, creating a permanent stale-count state identical in structure to the HatsSignerGate paginated-fetch mismatch: a stored count diverges from reality and the enforcement logic built on top of it silently stops working.

The `RecentReject` store is enabled by default whenever `config.recent_reject` is non-empty (the default config sets it to a subdirectory of the data directory): [4](#0-3) 

---

### Impact Explanation

Every rejected transaction — including zero-cost malformed submissions — is written to the `RecentReject` store via `put()`. Because the count limit is never enforced, the store grows proportionally to `(rejection rate) × (TTL)`. With the default TTL of 1 day and no rate-limiting on the RPC submission path, an attacker can exhaust node disk space, causing the RocksDB write to fail, the tx-pool service to error, and ultimately the node to become unable to process new transactions. This is a remote, unprivileged denial-of-service.

---

### Likelihood Explanation

The attack entry point is the standard `send_transaction` RPC, which is publicly reachable. Malformed transactions (e.g., empty inputs, invalid molecule encoding) are rejected before fee validation, so the attacker pays nothing per submission. No privileged access, key material, or majority hash-power is required. The only practical throttle is network bandwidth and the node's RPC concurrency limit, both of which are high enough to make the attack feasible.

---

### Recommendation

Assign the incremented value back to `self.total_keys_num` inside `put()`:

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

After `shrink()` the counter is already refreshed from the DB estimate, so no additional change is needed there.

---

### Proof of Concept

1. Start a CKB node with default config (recent-reject DB enabled).
2. In a loop, submit transactions with structurally invalid molecule encoding via `send_transaction` RPC.
3. Each call returns a `Malformed` rejection and writes one entry to the `RecentReject` store.
4. Observe that `total_keys_num` in memory never rises above `1` (initial estimate `0` + `1`), so `shrink()` is never invoked.
5. After `count_limit` (e.g., 100 000) submissions the store should have been shrunk; instead it holds all entries and continues growing, consuming disk proportional to the submission rate × TTL.

The existing unit test inadvertently confirms the bug: it asserts `recent_reject.total_keys_num < 100` after 160 puts against a limit of 100, which passes only because `total_keys_num` is never incremented and stays near the initial RocksDB estimate rather than reflecting the true entry count. [5](#0-4)

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
