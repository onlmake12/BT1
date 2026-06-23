### Title
`RecentReject::put` Never Increments `self.total_keys_num`, Breaking the Count-Limit Shrink Guard — (File: `tx-pool/src/component/recent_reject.rs`)

---

### Summary

In `RecentReject::put`, the in-memory counter `self.total_keys_num` is read and a locally-incremented copy is computed, but the result is **never written back** to `self.total_keys_num`. Because the field never grows after node startup, the `count_limit` guard that is supposed to trigger `shrink` never fires. An unprivileged attacker who continuously submits unique rejected transactions can grow the on-disk `RecentReject` RocksDB-with-TTL store well beyond its configured `count_limit`, leading to unbounded disk consumption and a denial-of-service against the node.

---

### Finding Description

`RecentReject` is a sharded RocksDB-with-TTL store that records recently rejected transactions so that duplicate relay attempts can be short-circuited. It is bounded by two mechanisms: a TTL that expires old entries automatically, and a `count_limit` that is supposed to call `shrink` when the estimated key count exceeds the limit.

The `put` method writes the new entry to the database and then checks whether the count limit has been exceeded: [1](#0-0) 

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    ...
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

The local binding `total_keys_num` holds `self.total_keys_num + 1`, but **`self.total_keys_num` is never assigned this value**. After node startup, `self.total_keys_num` is initialized once from the database estimate in `build`: [2](#0-1) 

It then stays frozen at that initial value forever. Every subsequent call to `put` compares the same stale value against `count_limit`. If the initial estimate was below `count_limit` (which it will be on a fresh or recently-started node), the condition `total_keys_num > self.count_limit` is never true, and `shrink` is never called.

Because `self.total_keys_num` never increases, the overflow branch (`else`) is also never reached. The `shrink` function is therefore **never invoked proactively** after startup.

**Attacker entry path:**

1. Connect to the target node as an unprivileged P2P peer (or use the open RPC endpoint).
2. Relay or submit a stream of unique transactions that will be rejected — e.g., transactions with always-failure lock scripts, double-spend attempts, or transactions that fail fee-rate checks.
3. Each rejection that passes `should_recorded()` (all rejections except `Reject::Duplicated`) is stored via `put_recent_reject` → `RecentReject::put`: [3](#0-2) 

4. Because `self.total_keys_num` never increases, `shrink` is never triggered, and the database grows without bound until the TTL expires entries.

The `should_recorded` check confirms that nearly all rejection types are stored: [4](#0-3) 

---

### Impact Explanation

The `count_limit` guard is the only **proactive** bound on the `RecentReject` store size. With it broken, the store grows at the rate of `rejected_tx_rate × TTL`. A sustained stream of unique rejected transactions (feasible via P2P relay at no cost) can exhaust disk space on the node. Disk exhaustion prevents the node from writing new blocks to the chain database, effectively halting the node — a denial-of-service reachable by any unprivileged peer.

---

### Likelihood Explanation

Any unprivileged P2P peer can relay transactions. Generating unique transactions that will be rejected (e.g., spending non-existent cells, using always-failure scripts) requires no special privilege, no keys, and no on-chain funds. The attack is cheap to sustain and requires only a persistent connection to the target node. The broken guard is present in every node running this code, making it universally exploitable.

---

### Recommendation

Update `self.total_keys_num` after the `checked_add` succeeds:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // <-- missing assignment
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Additionally, `shrink` should reset `self.total_keys_num` to the post-shrink database estimate so the counter remains accurate over the node's lifetime.

---

### Proof of Concept

1. Start a CKB node with default configuration (fresh data directory, so initial `total_keys_num` estimate = 0).
2. Connect a malicious peer that continuously relays unique transactions with always-failure lock scripts (each transaction has a unique input cell reference to avoid `Reject::Duplicated`).
3. Each transaction is rejected with `Reject::Verification(...)` and stored in `RecentReject::put`.
4. Observe via `get_estimate_total_keys_num()` that it always returns `0` (the initial estimate), while the actual RocksDB key count grows monotonically — confirming `self.total_keys_num` is never incremented.
5. After `count_limit` entries have been inserted, `shrink` has never been called, confirming the guard is permanently broken.
6. Continue until disk space is exhausted, causing the node to crash or fail to write new chain data.

**Analogy to the reference vulnerability:** In LeXscrow, `buyer` is reset to `address(0)` but `deposited` is not reset to `false`, breaking the guard check for new deposits. In CKB, `total_keys_num` is read and a local increment is computed but never written back to `self.total_keys_num`, breaking the `count_limit` guard for the `RecentReject` store — in both cases, a dependent state variable is left stale after a state transition, causing a guard to silently fail.

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

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** util/types/src/core/tx_pool.rs (L99-102)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```
