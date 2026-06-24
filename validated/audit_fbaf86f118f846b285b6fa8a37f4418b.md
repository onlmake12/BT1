The code confirms the claim. Let me verify the test behavior to complete the assessment.

Audit Report

## Title
`RecentReject::put` never increments `self.total_keys_num`, causing unbounded disk growth — (File: tx-pool/src/component/recent_reject.rs)

## Summary
In `RecentReject::put()`, the local binding `total_keys_num` is computed via `self.total_keys_num.checked_add(1)` but is never written back to `self.total_keys_num` in the non-shrink branch. As a result, `self.total_keys_num` is permanently stuck at its startup estimate, `shrink()` is never triggered, and the `DBWithTTL`-backed store grows without bound as rejected transactions accumulate within the TTL window.

## Finding Description
In `build()` (lines 39–51), `self.total_keys_num` is initialized once from `estimate_num_keys_cf` — typically 0 on a fresh node. In `put()` (lines 62–69):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // BUG: self.total_keys_num is never updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local shadow variable. `self.total_keys_num` is only mutated inside `shrink()` (lines 110–111). Because the write-back is absent, every call to `put()` re-evaluates `(initial_estimate + 1) > count_limit` against the same stale value. On a fresh node where `initial_estimate ≈ 0`, this condition is `1 > count_limit`, which is false for any `count_limit ≥ 1`. `shrink()` is never reached, and `self.db.put()` (line 60) executes unconditionally on every call.

The existing test `test_basic` does not catch this: its second batch reuses the same 80 keys (overwrites), so the DB never exceeds 80 entries and `total_keys_num` stays at 0, which trivially satisfies the `< 100` assertion at line 39.

The TTL mechanism (`DBWithTTL`) provides eventual expiry, but RocksDB TTL deletion is compaction-driven and not immediate. Within the TTL window (configured via `keep_rejected_tx_hashes_days`), entries accumulate without any count-based bound.

## Impact Explanation
An attacker can submit an unlimited number of distinct rejected transactions, each unconditionally written to disk. Because `shrink()` never fires, the `count_limit` invariant is permanently broken and disk usage grows without bound within the TTL window. Sustained submission exhausts disk space, causing the node to crash or become unavailable. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Transaction submission is an unauthenticated operation available over both P2P and RPC. Transactions need only be distinct (different hashes) to avoid the `Reject::Duplicated` early-exit path; they do not need to be valid. No special privileges, keys, or hashpower are required. The attack is trivially repeatable and can be sustained indefinitely.

## Recommendation
Add the missing write-back in the non-shrink branch of `put()`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // ← fix
    }
} else {
    self.shrink()?;
}
```

## Proof of Concept
With `shard_num=2`, `count_limit=10`, `ttl=-1`, starting from an empty DB (`total_keys_num=0`):

1. Call `put()` with 10,000 distinct `Byte32` hashes.
2. Each call evaluates `0 + 1 = 1 > 10` → false → no shrink, no write-back.
3. After 10,000 inserts, assert `self.total_keys_num == 0` (stale) and `db.estimate_num_keys_cf(...)` ≈ 10,000.
4. Confirm the DB contains 10,000 entries while `count_limit` is 10 — invariant permanently broken. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
