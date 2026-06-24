Audit Report

## Title
`RecentReject::put` Never Increments `self.total_keys_num`, Disabling Shrink Guard and Allowing Unbounded DB Growth — (`tx-pool/src/component/recent_reject.rs`)

## Summary

In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` that is never written back to `self.total_keys_num`. Consequently, `self.total_keys_num` never advances past its startup value, the guard condition `total_keys_num > self.count_limit` is never satisfied for any reasonable limit, `shrink()` is never invoked, and the RocksDB-backed recent-reject store grows without bound for the lifetime of the process. Any unprivileged peer can exploit this by repeatedly submitting transactions that produce non-`Duplicated` rejections, driving unbounded disk consumption and I/O load on a CKB node.

## Finding Description

**Root cause — missing write-back in `put()`:**

```rust
// tx-pool/src/component/recent_reject.rs, lines 62–69
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {   // total_keys_num is LOCAL
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

`total_keys_num` is a pattern-bound local. `self.total_keys_num` is never updated here. The missing line is `self.total_keys_num = total_keys_num;`.

**Initialization:** `self.total_keys_num` is set once at startup from a RocksDB key-count estimate (lines 39–52). For a fresh node this is 0.

**Guard evaluation:** Every call to `put()` evaluates `(self.total_keys_num + 1) > count_limit`. Because `self.total_keys_num` is frozen at its startup value, this expression is constant across all calls. For a fresh node it is `1 > count_limit`, which is false for any `count_limit ≥ 1`.

**`shrink()` update path:** The only place `self.total_keys_num` is ever updated is inside `shrink()` (lines 110–111), which re-reads the RocksDB estimate and stores it. Because `shrink()` is never reached, the in-memory counter never reflects actual DB growth.

**Exploit path:**
1. Attacker submits transactions with fee rates below `min_fee_rate` (or any other non-`Duplicated` rejection path).
2. Each rejection passes `should_recorded()` (line 100–102 of `util/types/src/core/tx_pool.rs`), which only excludes `Reject::Duplicated`.
3. The reject callback in `shared/src/shared_builder.rs` (lines 580–585) calls `recent_reject.put()` unconditionally for every such rejection.
4. Each `put()` writes one entry to RocksDB and exits without calling `shrink()`.
5. `self.total_keys_num` remains at its startup value; the configured `count_limit` is never enforced.

**Existing guards reviewed and found insufficient:**
- `should_recorded()` only filters `Reject::Duplicated`; all other variants (including `LowFeeRate`, `RBFRejected`, `Verification`, `Invalidated`) are recorded.
- The TTL (`keep_rejected_tx_hashes_days`) causes entries to expire after N days but does not prevent growth if the submission rate exceeds the expiry rate.
- The overflow branch (`else { self.shrink()?; }`) requires `u64` overflow of `self.total_keys_num`, which is unreachable in practice since the counter never increments.

## Impact Explanation

The `count_limit` invariant (configured via `keep_rejected_tx_hashes_count`) is completely unenforced at runtime. The recent-reject RocksDB store grows without bound, consuming unbounded disk space. Sustained disk exhaustion will crash the CKB node process or render the host system inoperable. Each `put()` also triggers a RocksDB WAL write and SST compaction pressure, causing I/O load proportional to the rejection rate. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The attack requires no special privilege and is accessible via both P2P relay and the `send_transaction` RPC. The cheapest vector is low-fee-rate spam: submit many distinct transactions with fee rates just below `min_fee_rate`. Each receives `Reject::LowFeeRate`, passes `should_recorded()`, and is written to the DB. There is no increasing-fee constraint, so the cost per entry is minimal and constant. The attacker only needs to sustain a submission rate that exceeds the TTL expiry rate to achieve net DB growth. This is a realistic, repeatable, low-cost attack.

## Recommendation

In `put()`, assign the incremented value back to `self.total_keys_num` before the limit check:

```rust
pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
    // ... existing db.put() call ...

    if let Some(new_total) = self.total_keys_num.checked_add(1) {
        self.total_keys_num = new_total;          // ← missing assignment
        if self.total_keys_num > self.count_limit {
            self.shrink()?;
        }
    } else {
        self.shrink()?;
    }
    Ok(())
}
```

## Proof of Concept

The following unit test demonstrates that `self.total_keys_num` is never incremented and `shrink()` is never called, regardless of how many entries are inserted:

```rust
// cargo test -p ckb-tx-pool test_total_keys_num_never_incremented
#[test]
fn test_total_keys_num_never_incremented() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let shard_num = 2;
    let limit = 10u64;
    let ttl = -1; // no expiry

    let mut rr = RecentReject::build(tmp_dir.path(), shard_num, limit, ttl).unwrap();
    assert_eq!(rr.total_keys_num, 0);

    // Insert 10× the limit; shrink() should fire repeatedly if the counter worked.
    for i in 0..(limit * 10) {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        rr.put(&key, Reject::LowFeeRate(FeeRate::zero(), 0, 0)).unwrap();
        // total_keys_num never changes — shrink() is never called.
        assert_eq!(rr.total_keys_num, 0,
            "iteration {}: total_keys_num was not incremented", i);
    }

    // Actual DB key count far exceeds count_limit.
    let actual = rr.estimate_total_keys_num().unwrap();
    assert!(actual > limit,
        "DB has {} keys, exceeding count_limit={}", actual, limit);
}
```

The assertions on `rr.total_keys_num == 0` will pass (demonstrating the bug), and the final assertion on `actual > limit` will also pass (demonstrating unbounded growth).

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/component/recent_reject.rs (L104-112)
```rust
    fn shrink(&mut self) -> Result<u64, AnyError> {
        let mut rng = thread_rng();
        let shard = rng.sample(Uniform::new(0, self.shard_num)).to_string();
        self.db.drop_cf(&shard)?;
        self.db.create_cf_with_ttl(&shard, self.ttl)?;

        let total_keys_num = self.estimate_total_keys_num()?;
        self.total_keys_num = total_keys_num;
        Ok(total_keys_num)
```

**File:** util/types/src/core/tx_pool.rs (L99-102)
```rust
    /// Returns true if the reject should be recorded.
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
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
