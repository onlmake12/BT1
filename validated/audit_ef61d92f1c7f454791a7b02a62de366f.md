Audit Report

## Title
`RecentReject::put` Never Updates `self.total_keys_num`, Disabling the Count Limit and Enabling Unbounded Disk Growth — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, the incremented key count is computed into a local variable but never written back to `self.total_keys_num`. As a result, the limit guard always evaluates against the stale startup estimate, `shrink()` is never triggered during the node's lifetime, and the `recent_reject` RocksDB database grows without bound. Any unprivileged user who can submit transactions that are rejected with a recordable reason can exhaust disk space and crash the node.

## Finding Description
In `tx-pool/src/component/recent_reject.rs` lines 62–69, `put()` computes `total_keys_num` as a local binding via `self.total_keys_num.checked_add(1)` but never assigns it back to `self.total_keys_num`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {   // always (initial_estimate + 1) > limit
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`self.total_keys_num` is only ever written inside `shrink()`:

```rust
let total_keys_num = self.estimate_total_keys_num()?;
self.total_keys_num = total_keys_num;   // only update site
``` [2](#0-1) 

Because `shrink()` is never reached (the guard condition is permanently stale), `self.total_keys_num` stays at the RocksDB key-count estimate from construction time — `0` for a fresh database. Every subsequent call to `put()` evaluates `0 + 1 > count_limit` (i.e., `1 > 10_000_000`), which is always `false`.

The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes trivially because `total_keys_num` is never updated and stays `0`, providing no real coverage of the shrink path: [3](#0-2) 

`should_recorded()` returns `true` for every `Reject` variant except `Reject::Duplicated`, meaning nearly all rejection reasons write to the database: [4](#0-3) 

The call path from remote transaction submission is: P2P relay / `send_transaction` RPC → `process_tx` → `after_process` → `put_recent_reject` → `RecentReject::put`: [5](#0-4) [6](#0-5) 

A second call path exists via the reject callback registered in `shared_builder.rs`, which also calls `recent_reject.put` directly for pool-eviction rejections: [7](#0-6) 

## Impact Explanation
This matches **High: Vulnerabilities which could easily crash a CKB node**. Sustained submission of rejected transactions causes the `recent_reject` RocksDB database to grow at one entry per rejection, bounded only by the 7-day TTL window. At the default limit of 10,000,000 entries and hundreds of bytes per JSON-serialised entry, sustained spam can exhaust gigabytes of disk space. When the disk is full, RocksDB write operations fail; these errors propagate through the tx-pool and can halt block processing or crash the node entirely.

## Likelihood Explanation
The exploit requires no privilege. Any peer can broadcast transactions over the P2P relay protocol or call the `send_transaction` RPC. Transactions rejected for `LowFeeRate`, `ExceededMaximumAncestorsCount`, `ExceededTransactionSizeLimit`, `Resolve`, `Verification`, `RBFRejected`, `Expiry`, `Invalidated`, or `DeclaredWrongCycles` all satisfy `should_recorded()`. On mainnet, the per-transaction cost is non-trivial but the attacker only needs to sustain a rate sufficient to fill the disk before TTL expiry clears old entries. In dev/test environments with zero minimum fee rate, the cost is effectively zero.

## Recommendation
In `RecentReject::put`, assign the incremented value back to `self.total_keys_num` immediately after `checked_add(1)` succeeds, before the limit check:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;   // ← add this line
    if total_keys_num >= self.count_limit { // use >= to enforce at exactly count_limit
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Additionally, update the unit test to assert that `shrink()` was actually triggered (i.e., that entries were dropped) rather than asserting on the stale `total_keys_num` field.

## Proof of Concept
1. Start a CKB node with default tx-pool config (`keep_rejected_tx_hashes_count = 10_000_000`, `keep_rejected_tx_hashes_days = 7`).
2. Observe that `RecentReject` is constructed with `total_keys_num = 0` (empty database). [8](#0-7) 
3. Submit transactions in a tight loop via `send_transaction` RPC that fail fee-rate validation (`Reject::LowFeeRate`).
4. Each call to `put_recent_reject` → `RecentReject::put` writes one entry to RocksDB and evaluates `0 + 1 > 10_000_000` → `false`; `shrink()` is never called and `self.total_keys_num` stays `0`.
5. After N iterations the database contains N entries with no eviction. Disk usage grows linearly with N.
6. Confirm by calling `get_estimate_total_keys_num()` — it returns `0` regardless of how many entries have been written. [9](#0-8) 
7. Unit-level reproduction: modify the existing test to insert 160 entries against a limit of 100 and assert that `estimate_total_keys_num()` (the live RocksDB estimate, not the stale field) returns a value below 100 — this assertion will fail, confirming the bug. [10](#0-9)

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

**File:** tx-pool/src/component/recent_reject.rs (L80-82)
```rust
    pub fn get_estimate_total_keys_num(&self) -> u64 {
        self.total_keys_num
    }
```

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L6-39)
```rust
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
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
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

**File:** shared/src/shared_builder.rs (L579-585)
```rust
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }
```
