### Title
Missing `total_keys_num` Increment in `RecentReject::put` Allows Unbounded RocksDB Growth — (`tx-pool/src/component/recent_reject.rs`)

---

### Summary

`RecentReject::put` writes every rejected transaction to RocksDB but **never increments `self.total_keys_num`**. The local result of `checked_add(1)` is computed but discarded without being stored back to the struct field. Because the counter stays frozen at its startup value (0 for a fresh DB), the `> count_limit` guard never fires, `shrink()` is never called, and the RocksDB instance grows without bound. Any unprivileged remote peer can exploit this by relaying transactions that fail with any non-`Duplicated` reject reason.

---

### Finding Description

In `RecentReject::put`:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ...
    self.db.put(&shard, hash_slice, json_string)?;   // ← writes to DB unconditionally

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

`total_keys_num` is a **local shadow variable**. `self.total_keys_num` is never assigned the incremented value. It stays at whatever `estimate_num_keys_cf` returned at startup — 0 for a fresh DB. Every subsequent call evaluates `0 + 1 = 1 > count_limit`, which is always false, so `shrink()` is never triggered.

`shrink()` is the only place that updates `self.total_keys_num`:

```rust
fn shrink(&mut self) -> Result<u64, AnyError> {
    // ...
    let total_keys_num = self.estimate_total_keys_num()?;
    self.total_keys_num = total_keys_num;   // ← only update site
    Ok(total_keys_num)
}
```

Since `shrink()` is never reached, the RocksDB column families accumulate entries indefinitely. [1](#0-0) 

---

### Impact Explanation

- Every rejected transaction (except `Duplicated`) is written to the `RecentReject` RocksDB instance with no eviction.
- `should_recorded()` returns `true` for all `Reject` variants except `Reject::Duplicated(..)`, covering `LowFeeRate`, `Resolve`, `Verification`, `Malformed`, `Full`, `RBFRejected`, etc.
- The RocksDB instance grows without bound, consuming disk space proportional to the number of rejected transactions received.
- Disk exhaustion causes node crash or severe I/O degradation affecting block and transaction processing throughput. [2](#0-1) 

---

### Likelihood Explanation

The attack path is fully reachable from an unprivileged remote peer:

1. Remote peer sends P2P `RelayTransactionHashes` / `SendTransaction` messages.
2. Transactions fail with `LowFeeRate` (trivially achieved by sending zero-fee txs) or `Verification`.
3. `after_process` → `reject.should_recorded() == true` → `put_recent_reject` → `RecentReject::put`.
4. Each call writes to RocksDB; `self.total_keys_num` stays at 0; `shrink()` never fires. [3](#0-2) [4](#0-3) 

No special privileges, no PoW, no key material required. The attacker only needs to send enough transactions to fill the disk. The default `keep_rejected_tx_hashes_count` config value determines how many entries *should* be kept, but the bug means this limit is never enforced. [5](#0-4) 

---

### Recommendation

Add the missing assignment in `put()` so `self.total_keys_num` is actually incremented:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ... db.put ...
    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // ← add this line
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
``` [6](#0-5) 

---

### Proof of Concept

The existing unit test in `tx-pool/src/component/tests/recent_reject.rs` already demonstrates the bug: it inserts 160 entries (80 + 80) against a `limit = 100` and asserts `total_keys_num < 100`. With the bug present, `total_keys_num` stays at 0 (the frozen initial value), so the assertion passes vacuously — but the actual RocksDB key count is 80 (not bounded to 100). A stronger test would assert the *actual DB key count* does not exceed `count_limit`:

```rust
// After 10 * count_limit puts, actual DB keys must not exceed count_limit
for i in 0..10 * limit {
    let key = Byte32::new(blake2b_256(i.to_le_bytes()));
    recent_reject.put(&key, Reject::LowFeeRate(...)).unwrap();
}
let actual = recent_reject.estimate_total_keys_num().unwrap();
assert!(actual <= limit, "DB has {} keys, limit is {}", actual, limit);
```

This test would fail with the current code, confirming unbounded growth. [7](#0-6)

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

**File:** tx-pool/src/process.rs (L428-438)
```rust
    pub(crate) async fn put_recent_reject(&self, tx_hash: &Byte32, reject: &Reject) {
        let mut tx_pool = self.tx_pool.write().await;
        if let Some(ref mut recent_reject) = tx_pool.recent_reject
            && let Err(e) = recent_reject.put(tx_hash, reject.clone())
        {
            error!(
                "Failed to record recent_reject {} {} {}",
                tx_hash, reject, e
            );
        }
    }
```

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
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

**File:** tx-pool/src/component/tests/recent_reject.rs (L1-42)
```rust
use ckb_hash::blake2b_256;
use ckb_types::{core::tx_pool::Reject, packed::Byte32};

use crate::component::recent_reject::RecentReject;

#[test]
fn test_basic() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 100;
    let ttl = -1;

    let mut recent_reject = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        let reject: ckb_jsonrpc_types::PoolTransactionReject =
            Reject::Malformed(i.to_string(), Default::default()).into();
        assert_eq!(
            recent_reject.get(&key).unwrap().unwrap(),
            serde_json::to_string(&reject).unwrap()
        )
    }

    for i in 0..80u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    assert!(recent_reject.total_keys_num < 100);
}


```
