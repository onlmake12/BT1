The code confirms the claim exactly. In `put()` at line 62, `self.total_keys_num.checked_add(1)` binds the result to a local variable `total_keys_num` inside the `if let Some(...)` arm — `self.total_keys_num` (the struct field) is never assigned the incremented value. The field stays at its initial estimated value (0 for a fresh DB) forever. The test at line 39 asserts `total_keys_num < 100` after 160 puts with limit=100, which passes only because the counter is still 0.

Audit Report

## Title
`RecentReject::put()` Never Persists Incremented `total_keys_num`, Making `count_limit` Permanently Ineffective — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put()`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable and never written back to `self.total_keys_num`. The struct field remains at its initial estimated value (0 on a fresh node) indefinitely, so the `count_limit` guard is never satisfied and `shrink()` is never called. The on-disk RocksDB store for rejected transactions grows without bound, bounded only by the TTL expiry window, allowing an unprivileged attacker to exhaust node disk space.

## Finding Description
In `put()` (lines 62–65), the incremented count is computed but discarded:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
}
```

`self.total_keys_num` is never assigned `total_keys_num`. On a fresh node, `self.total_keys_num` is initialized to the RocksDB estimate (0 for an empty DB) in `build()` at lines 39–51. Every subsequent call to `put()` therefore evaluates `0 + 1 > count_limit`, which is always `false` for any reasonable limit. `shrink()` at lines 104–113 — the only place that updates `self.total_keys_num` — is unreachable. The existing unit test at `tests/recent_reject.rs:39` inadvertently confirms this: after 160 `put()` calls with `limit = 100`, the assertion `total_keys_num < 100` passes because the counter is still 0, not because the limit was enforced. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
The `recent_reject` RocksDB store accumulates every qualifying rejected transaction without ever triggering `shrink()`. The only remaining bound is the TTL expiry window. An attacker submitting a sustained stream of invalid transactions can fill the node's disk within that window, causing the node to crash or become unable to write blocks, chain state, or tx-pool data. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The exploit requires no privilege. Any peer or RPC caller can invoke `send_transaction`. Transactions rejected with reasons such as `DeclaredWrongCycles`, `Malformed`, or `Verification` are recorded via `put_recent_reject()` when `reject.should_recorded()` is true. Crafting cheaply-invalid transactions (e.g., mismatched declared cycle counts) is straightforward and requires no special access. The bug is present on every node with `recent_reject` enabled, which is the default configuration. [5](#0-4) 

## Recommendation
Assign the incremented value back to `self.total_keys_num` before the limit check in `put()`:

```rust
if let Some(new_total) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = new_total;   // ← persist the increment
    if self.total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
``` [1](#0-0) 

## Proof of Concept
The existing unit test at `tx-pool/src/component/tests/recent_reject.rs:6–39` is itself the proof of concept. It performs 160 `put()` calls against a limit of 100 and then asserts `recent_reject.total_keys_num < 100`. The assertion passes — but only because `total_keys_num` is still 0, not because the limit was enforced and `shrink()` ran. Changing the assertion to `recent_reject.total_keys_num >= 100` (the expected post-condition if the counter were correctly maintained) would cause the test to fail, confirming the bug. For a live node, repeatedly submitting transactions that trigger `should_recorded()` rejections via the `send_transaction` RPC will grow the on-disk store indefinitely until disk exhaustion. [6](#0-5)

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
