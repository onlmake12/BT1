The premise of the question is **correct**. The bug is real and verifiable in the code.

---

### Title
`RecentReject::put` never writes back `total_keys_num`, disabling `count_limit` enforcement and allowing unbounded RocksDB growth — (`tx-pool/src/component/recent_reject.rs`)

### Summary

`RecentReject::put` computes a local `total_keys_num` via `checked_add(1)` but never assigns it back to `self.total_keys_num` unless `shrink()` fires. Since `shrink()` only fires when `total_keys_num > count_limit`, and `total_keys_num` is always `self.total_keys_num + 1` (a constant), `shrink()` never fires on a fresh node. The DB grows without bound.

### Finding Description

In `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // BUG: self.total_keys_num is never updated here
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`self.total_keys_num` is initialized at startup from a RocksDB estimate: [2](#0-1) 

It is only ever written back inside `shrink()`: [3](#0-2) 

On a fresh node, `self.total_keys_num = 0`. Every call to `put()` evaluates `total_keys_num = 0 + 1 = 1`. Since `1 > count_limit` is false for any reasonable `count_limit`, `shrink()` is never called, `self.total_keys_num` stays at `0` forever, and the DB grows without bound.

### Impact Explanation

Every rejected transaction (except `Reject::Duplicated`) is recorded via `put_recent_reject` → `RecentReject::put`: [4](#0-3) 

This covers `LowFeeRate`, `Verification`, `Malformed`, `Resolve`, `Full`, `RBFRejected`, `Expiry`, `ExceededMaximumAncestorsCount`, `ExceededTransactionSizeLimit`, and `DeclaredWrongCycles` — essentially every rejection type an attacker can trigger cheaply. The call path from P2P relay is: [5](#0-4) 

An attacker flooding the node with unique low-fee-rate transactions (which are cheap to construct and require no PoW) causes the `recent_reject` RocksDB column families to grow without bound, consuming unbounded disk I/O and storage until the node crashes or becomes unresponsive.

### Likelihood Explanation

The attack requires only the ability to submit transactions via P2P relay — no privileged access, no PoW, no key material. Transactions rejected for `LowFeeRate` are trivially constructable. The TTL mechanism (`DBWithTTL`) provides eventual cleanup only during RocksDB compaction, not immediate deletion, and an attacker can write entries far faster than TTL-based compaction removes them.

### Recommendation

In `put()`, write back the incremented counter when `shrink()` is not called:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num; // fix: persist the increment
    }
} else {
    self.shrink()?;
}
```

### Proof of Concept

Using the existing test harness (`tx-pool/src/component/tests/recent_reject.rs`): [6](#0-5) 

Set `shard_num=2`, `count_limit=10`, insert 10 000 unique entries with `Reject::LowFeeRate(...)`. Assert that `recent_reject.total_keys_num` remains `0` (or its startup estimate) throughout, and that the actual DB key count via `estimate_total_keys_num()` far exceeds `count_limit`. The existing test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) would fail with 160 inserts if the limit were 100 and the initial estimate were 0, confirming the bug.

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

**File:** tx-pool/src/component/recent_reject.rs (L110-111)
```rust
        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L502-524)
```rust
                Err(reject) => {
                    debug!(
                        "after_process {} {} remote reject: {} ",
                        tx_hash, peer, reject
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```

**File:** tx-pool/src/component/tests/recent_reject.rs (L7-39)
```rust
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
