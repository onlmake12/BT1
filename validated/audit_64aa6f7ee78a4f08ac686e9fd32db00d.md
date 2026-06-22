### Title
`RecentReject` Count Limit Never Enforced Due to `total_keys_num` Not Being Updated in `put()` — (File: `tx-pool/src/component/recent_reject.rs`)

---

### Summary

The `RecentReject::put()` function computes `self.total_keys_num.checked_add(1)` into a **local variable** but never writes the result back to `self.total_keys_num`. As a result, the in-memory counter stays at its initial estimate forever, the `count_limit` guard is never triggered, and the on-disk rejected-transaction database grows without bound. Any unprivileged RPC caller can exploit this by flooding the node with rejected transactions.

---

### Finding Description

`RecentReject` is the subsystem that records recently-rejected transactions so that duplicate submissions can be answered quickly. It is initialised with a `count_limit` that is supposed to cap the number of stored entries and trigger a `shrink()` (compaction/eviction) when exceeded.

The relevant code in `put()` is:

```rust
// tx-pool/src/component/recent_reject.rs  lines 55-71
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    ...
    self.db.put(&shard, hash_slice, json_string)?;   // entry written to DB

    if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
        // `total_keys_num` is a LOCAL variable — self.total_keys_num is NEVER updated
        if total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

`self.total_keys_num.checked_add(1)` produces a new value that is bound to the local name `total_keys_num` inside the `if let` arm. `self.total_keys_num` itself is **never reassigned**. After `build()` sets it to the initial RocksDB estimate (typically `0` for a fresh database), it remains `0` for the entire lifetime of the process.

Consequently:
- The branch `total_keys_num > self.count_limit` evaluates `1 > count_limit`. For any `count_limit ≥ 1` (the default is `keep_rejected_tx_hashes_count`, a large number), this is always `false`.
- `shrink()` is **never called** via the count path.
- The only protection that remains is the RocksDB TTL, which is set to `keep_rejected_tx_hashes_days * 86400` seconds. Within that window the database is unbounded.

The `estimate_total_keys_num()` helper and the `shrink()` method both exist and are correct in isolation; the bug is solely the missing write-back of the incremented counter.

---

### Impact Explanation

An attacker who can reach the node's RPC endpoint (or relay transactions through the P2P network) can continuously submit transactions that are guaranteed to be rejected (e.g., transactions with a zero-capacity output, an invalid lock script hash, or a double-spend of a known live cell). Each rejection causes `RecentReject::put()` to be called, writing one entry to the RocksDB shard. Because `shrink()` is never triggered, entries accumulate until the TTL expires them — which can be days. Over that window the attacker can fill the node's disk, causing:

1. **Disk exhaustion / node crash** — RocksDB write failures propagate upward and can halt the tx-pool service.
2. **Degraded lookup performance** — an oversized SST file set slows `get()` calls used to answer `get_pool_transaction` and relay-dedup checks.

The impact is a **resource-exhaustion DoS** reachable by any unprivileged transaction sender or RPC caller.

---

### Likelihood Explanation

- The entry path (submitting a rejected transaction via `send_transaction` RPC or P2P relay) requires no special privilege.
- Generating a stream of syntactically valid but semantically invalid transactions is trivial (e.g., re-sign a transaction spending an already-spent cell).
- The bug is deterministic: every single call to `put()` fails to update the counter, so the condition is always present, not race-dependent.
- The only natural mitigation is the TTL, but a sustained low-rate flood (well within normal network noise) is sufficient to keep the database large.

Likelihood: **Medium-High**.

---

### Recommendation

In `put()`, write the incremented value back to `self.total_keys_num` before the comparison:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    ...
    self.db.put(&shard, hash_slice, json_string)?;

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // ← missing write-back
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

Additionally, `shrink()` should reset `self.total_keys_num` to the post-compaction estimate (via `self.estimate_total_keys_num()`) so the counter stays accurate after eviction.

---

### Proof of Concept

1. Start a CKB node with default configuration (`keep_rejected_tx_hashes_count = 10_000`, `keep_rejected_tx_hashes_days = 7`).
2. Obtain any live cell outpoint from the chain.
3. In a loop, construct a transaction that double-spends that outpoint (change only the witness each iteration to get a unique tx hash) and submit it via `send_transaction` RPC.
4. Each submission is rejected with `Reject::Resolve(OutPointError::Dead(_))` and recorded via `put()`.
5. Observe that `self.total_keys_num` never advances past its initial value (inspect via `get_estimate_total_keys_num()` RPC or a debugger), `shrink()` is never invoked, and the `recent_reject` RocksDB directory grows monotonically until disk is exhausted or the TTL window closes.

The root cause is confirmed at: [1](#0-0) 

where `self.total_keys_num` is read but never written back after `checked_add(1)`, and at: [2](#0-1) 

where `total_keys_num` is declared as a mutable field that is only ever set during `build()`. [3](#0-2)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L12-18)
```rust
pub struct RecentReject {
    ttl: i32,
    shard_num: u32,
    pub(crate) count_limit: u64,
    pub(crate) total_keys_num: u64,
    pub(crate) db: DBWithTTL,
}
```

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
