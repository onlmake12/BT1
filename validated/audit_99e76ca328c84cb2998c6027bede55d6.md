Audit Report

## Title
`RecentReject::put()` Never Persists Incremented `total_keys_num`, Making Count Limit Permanently Ineffective — (`tx-pool/src/component/recent_reject.rs`)

## Summary

In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound only to a local variable; `self.total_keys_num` is never assigned the incremented value. Because the field stays at its initial estimated value (typically `0` on a fresh node), the `count_limit` guard is never satisfied and `shrink()` is never called. The `recent_reject` RocksDB store grows without bound, enabling disk exhaustion and node crash.

## Finding Description

In `tx-pool/src/component/recent_reject.rs`, `put()` writes an entry to the DB and then attempts a count-limit check:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [1](#0-0) 

The local binding `total_keys_num` is computed but `self.total_keys_num` is never assigned. On a fresh node, `total_keys_num` is initialized from `estimate_num_keys_cf`, which returns `0` for an empty DB. [2](#0-1) 

Every subsequent call to `put()` therefore evaluates `0 + 1 > count_limit`, which is always `false` for any reasonable limit. `shrink()` is unreachable through the count path. The only place `self.total_keys_num` is ever updated is inside `shrink()` itself: [3](#0-2) 

The existing unit test inadvertently confirms the bug: after 160 `put()` calls against a limit of 100, it asserts `recent_reject.total_keys_num < 100`. This assertion passes only because the counter is still `0`, not because the limit was enforced. [4](#0-3) 

The TTL-based expiry (RocksDB `DBWithTTL`) provides partial mitigation — entries expire after `keep_rejected_tx_hashes_days` — but this does not prevent disk exhaustion under sustained attack, because RocksDB TTL expiry is lazy (triggered by compaction, not on write) and the attacker can write far faster than compaction reclaims space.

## Impact Explanation

The `recent_reject` store accumulates every qualifying rejected transaction indefinitely. An attacker who drives continuous rejections will exhaust the node's disk, causing RocksDB writes to fail, which propagates to chain state and tx-pool writes, crashing the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires no privilege. RPC callers submitting transactions via `send_transaction` that are rejected with recordable reasons (e.g., `LowFeeRate`, `DeclaredWrongCycles`, `Verification`) trigger `put_recent_reject` without any rate limit or ban: [5](#0-4) 

P2P peers sending malformed transactions are banned, but RPC callers are not. A local or remote RPC caller can submit a high volume of cheaply-crafted invalid transactions (e.g., transactions with a declared cycle count mismatching actual execution) indefinitely. The bug is present on every node with `recent_reject` enabled (the default).

## Recommendation

Assign the incremented value back to `self.total_keys_num` before the limit check in `put()`:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ... existing write ...
    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;   // ← persist the increment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
``` [6](#0-5) 

The unit test assertion `assert!(recent_reject.total_keys_num < 100)` should also be updated to verify that `shrink()` was actually triggered and the counter reflects a post-shrink value, not that the counter is still `0`.

## Proof of Concept

1. Start a CKB node with default config (`recent_reject` enabled, `keep_rejected_tx_hashes_count = 100`).
2. Repeatedly call `send_transaction` RPC with transactions whose `declared_cycles` mismatches actual execution (`DeclaredWrongCycles` rejection). These are recorded via `put_recent_reject` and the caller is not banned.
3. Observe via `get_estimate_total_keys_num` (or direct DB inspection) that `total_keys_num` never advances past its initial value of `0`.
4. The on-disk RocksDB store grows without bound; disk exhaustion eventually causes write failures and node crash.

The existing test at `tx-pool/src/component/tests/recent_reject.rs:39` reproduces the bug in isolation: after 160 `put()` calls with `limit = 100`, `total_keys_num` is `0`, confirming the counter is never updated and the limit is never enforced. [7](#0-6)

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

**File:** tx-pool/src/process.rs (L546-550)
```rust
                    Err(reject) => {
                        debug!("after_process {} reject: {} ", tx_hash, reject);
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```
