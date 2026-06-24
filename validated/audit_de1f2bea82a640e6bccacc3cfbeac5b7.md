Audit Report

## Title
Missing `self.total_keys_num` Increment in Non-Shrink Path Disables Shrink Mechanism — (`tx-pool/src/component/recent_reject.rs`)

## Summary

In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` but is never written back to `self.total_keys_num` when the limit is not exceeded. For a fresh DB, `self.total_keys_num` initializes to 0 and never increments, so the threshold check always evaluates `1 > count_limit`, which is false for any reasonable limit. `shrink()` is therefore never triggered, and the `recent_reject` RocksDB instance grows without effective bound, enabling disk exhaustion and node crash by any unprivileged remote peer.

## Finding Description

In `RecentReject::put` at lines 62–69:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
``` [1](#0-0) 

`total_keys_num` is a local binding. `self.total_keys_num` is only mutated inside `shrink()` at lines 110–111, which re-reads the estimate from RocksDB. [2](#0-1) 

`self.total_keys_num` is initialized in `build()` via `estimate_num_keys_cf` across all column families. For a fresh DB, `estimate_num_keys_cf` returns `None`, which is treated as 0 via `unwrap_or(0)`. [3](#0-2) 

Because `self.total_keys_num` starts at 0 and is never incremented in the normal path, every call to `put` evaluates `0 + 1 > count_limit`, which is false. `shrink()` is never called. The DB grows without bound.

The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes *because of* this bug — the in-memory counter stays at 0 regardless of how many entries are written. [4](#0-3) 

**Exploit path:**
1. A remote peer sends `RelayTransactions` P2P messages with transactions that fail with `LowFeeRate`.
2. `Reject::LowFeeRate` is not malformed: `is_malformed_tx()` returns `false` for it. [5](#0-4) 
3. `should_recorded()` returns `true` for all non-`Duplicated` rejects, including `LowFeeRate`. [6](#0-5) 
4. `put_recent_reject` is called, writing one entry to RocksDB permanently.
5. The peer is never banned and can repeat indefinitely.

The TTL mechanism (RocksDB TTL compaction) provides a partial mitigation — entries expire after the configured TTL — but compaction is not immediate and does not enforce a hard size cap. At sustained write rates, the DB grows faster than compaction can reclaim space.

## Impact Explanation

The `recent_reject` RocksDB instance grows without an effective upper bound. Each rejected transaction writes a JSON-serialized entry. At sustained relay rates from multiple peers (up to `MAX_RELAY_PEERS`), an attacker can drive continuous writes to the DB, causing disk exhaustion. Disk exhaustion crashes the node or causes severe I/O degradation affecting block and transaction processing. This matches **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

No special privileges are required. Any unprivileged remote peer can connect via the standard P2P relay protocol and submit transactions that fail with non-banning reject reasons (`LowFeeRate`, `Resolve`, `ExceededMaximumAncestorsCount`, etc.). The peer is never banned for these rejections. The attack is repeatable, persistent, and requires no victim interaction. Multiple peers can amplify the effect up to the `MAX_RELAY_PEERS` limit.

## Recommendation

In `RecentReject::put`, update `self.total_keys_num` in the non-shrink branch:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    } else {
        self.total_keys_num = total_keys_num;  // add this line
    }
} else {
    self.shrink()?;
}
```

Additionally, fix the unit test to assert the actual RocksDB key count via `estimate_total_keys_num()` rather than the in-memory `total_keys_num` field.

## Proof of Concept

Using the existing test harness in `tx-pool/src/component/tests/recent_reject.rs`:

```rust
#[test]
fn test_unbounded_growth() {
    let tmp_dir = tempfile::Builder::new().tempdir().unwrap();
    let mut recent_reject = RecentReject::build(tmp_dir.path(), 2, 100, -1).unwrap();

    for i in 0..1000u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::LowFeeRate(Default::default(), 0, 0))
            .unwrap();
    }

    // In-memory counter stays at 0 due to the bug
    assert_eq!(recent_reject.total_keys_num, 0);

    // Actual DB has ~1000 entries, far exceeding count_limit=100
    let actual = recent_reject.estimate_total_keys_num().unwrap();
    assert!(actual > 100, "actual={}", actual);
}
```

The existing `test_basic` test inserts 160 entries against a limit of 100 and then asserts `total_keys_num < 100` — this assertion passes only because the counter is stuck at 0, confirming the bug. [7](#0-6)

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

**File:** util/types/src/core/tx_pool.rs (L89-96)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
```

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```
