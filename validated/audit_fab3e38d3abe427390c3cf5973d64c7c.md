The core claim is that `self.total_keys_num` is never incremented inside `put()`, so `shrink()` is never triggered. Let me verify this precisely against the actual code.

The bug claim is precise and verifiable. Here is the exact analysis:

**Root cause — `self.total_keys_num` is never written back in `put()`**

```rust
// line 62: checked_add(1) produces a LOCAL binding named total_keys_num
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER assigned here
}
```

`self.total_keys_num` is initialized once at startup from `estimate_total_keys_num()`. For a fresh DB it is `0`. Every subsequent call to `put()` computes `0 + 1 = 1` into a local variable, checks `1 > 10_000_000` (false), and returns — leaving `self.total_keys_num` permanently at `0`. `shrink()` is never called. [1](#0-0) 

`shrink()` does correctly write back the re-estimated count, but it is unreachable via the normal `put()` path. [2](#0-1) 

`should_recorded()` returns `true` for every `Reject` variant except `Duplicated`, so every distinct rejected transaction hash is written to the DB. [3](#0-2) 

The remote-peer path calls `put_recent_reject` → `RecentReject::put` for any rejection that passes `should_recorded()`, including `LowFeeRate`. [4](#0-3) 

The default `keep_rejected_tx_hashes_count` is 10,000,000 and `keep_rejected_tx_hashes_days` is 7. [5](#0-4) 

**TTL partial mitigation:** RocksDB's TTL compaction eventually deletes entries older than 7 days, but compaction is background and non-immediate. An attacker submitting distinct low-fee-rate transactions continuously will outpace TTL deletion, growing the DB without bound.

---

### Title
`RecentReject::put` never increments `self.total_keys_num`, disabling the shrink bound — (`tx-pool/src/component/recent_reject.rs`)

### Summary
`RecentReject::put` computes `self.total_keys_num.checked_add(1)` into a local variable but never assigns the result back to `self.total_keys_num`. The field stays at its startup-estimated value (0 for a fresh DB) forever. The `shrink()` guard condition `total_keys_num > count_limit` is therefore never satisfied, and the RocksDB `recent_reject` store grows without bound.

### Finding Description
In `RecentReject::put` (line 62), the Rust `if let Some(total_keys_num) = self.total_keys_num.checked_add(1)` pattern binds the incremented value to a **new local variable** `total_keys_num`, shadowing the field name. `self.total_keys_num` is never updated. On a fresh node, `self.total_keys_num` is 0 at startup; every call to `put()` evaluates `0 + 1 = 1`, which is never `> count_limit` (10,000,000), so `shrink()` is never invoked. The DB accumulates one entry per unique rejected transaction hash with no eviction.

### Impact Explanation
Each entry is a JSON-serialized `PoolTransactionReject` (~100–300 bytes). At 10M+ entries the DB consumes multiple gigabytes of disk. Continued growth exhausts disk space, causing RocksDB write failures, node crash, and inability to participate in consensus — a remote-triggered availability/consensus-deviation impact.

### Likelihood Explanation
Any unprivileged P2P peer can relay transactions with fee rate below `min_fee_rate` (1000 shannons/KB). Each distinct transaction hash produces a `LowFeeRate` rejection that is recorded (`should_recorded()` = true, peer is not banned for `LowFeeRate`). No PoW, no key, no privileged access is required. The attacker only needs to generate distinct transaction hashes at high volume.

### Recommendation
Assign the incremented value back to `self.total_keys_num` inside `put()`:

```rust
if let Some(new_total) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = new_total;   // ← missing assignment
    if self.total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

### Proof of Concept
1. Start a CKB node with `recent_reject` enabled (default config).
2. In a loop, generate distinct transactions each paying 0 fee (below `min_fee_rate`).
3. Submit them via P2P relay or RPC `send_transaction`.
4. Observe that `get_estimate_total_keys_num()` always returns 0 (the stuck field), while the on-disk RocksDB directory grows monotonically past `count_limit` entries.
5. Assert: after 10,000,001 submissions, the DB directory size exceeds the expected bounded size.

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

**File:** util/app-config/src/legacy/tx_pool.rs (L53-59)
```rust
fn default_keep_rejected_tx_hashes_days() -> u8 {
    7
}

fn default_keep_rejected_tx_hashes_count() -> u64 {
    10_000_000
}
```
