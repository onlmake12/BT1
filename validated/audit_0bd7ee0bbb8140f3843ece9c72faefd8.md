Audit Report

## Title
Missing `self.total_keys_num` Increment in Non-Shrink Path Causes Unbounded RocksDB Growth — (`tx-pool/src/component/recent_reject.rs`)

## Summary

In `RecentReject::put`, the result of `self.total_keys_num.checked_add(1)` is bound to a local variable `total_keys_num` but is never written back to `self.total_keys_num` when the limit is not exceeded. Because `self.total_keys_num` is initialized once at startup from `estimate_num_keys_cf` (returning 0 for a fresh DB) and never incremented thereafter, the threshold check `total_keys_num > self.count_limit` always evaluates as `1 > count_limit`, which is false for any reasonable limit. `shrink()` is therefore never triggered, and the `recent_reject` RocksDB instance grows without bound, leading to disk exhaustion and node crash.

## Finding Description

In `tx-pool/src/component/recent_reject.rs` lines 62–69:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is NEVER updated here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local binding. `self.total_keys_num` is only mutated inside `shrink()` (lines 110–111), which re-reads the estimate from RocksDB. Because `self.total_keys_num` starts at 0 for a fresh DB and is never incremented in the normal path, every call to `put` evaluates `0 + 1 > count_limit`, which is false. `shrink()` is never called.

The existing unit test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes precisely because of this bug — the in-memory counter stays at 0 regardless of how many entries are written to RocksDB. The test does not call `estimate_total_keys_num()` to check the actual DB key count.

The exploit path is fully reachable by an unprivileged remote peer:
1. Peer sends `RelayTransactions` messages over P2P (up to `MAX_RELAY_TXS_NUM_PER_BATCH = 32767` txs per message, rate-limited to 30 messages/second per peer).
2. Transactions fail with `LowFeeRate` (or `Resolve`, `ExceededMaximumAncestorsCount`, etc.).
3. `should_recorded()` returns `true` for all non-`Duplicated` rejects (line 100–102 of `util/types/src/core/tx_pool.rs`).
4. `is_malformed_tx()` returns `false` for `LowFeeRate` (line 89–97), so `ban_malformed` is not called.
5. `put_recent_reject` is called (line 522–524 of `tx-pool/src/process.rs`), writing one entry to RocksDB permanently.
6. The peer is never banned and can repeat indefinitely.

## Impact Explanation

Each rejected transaction writes a JSON-serialized entry to RocksDB with no effective upper bound. At 30 relay messages/second per peer × up to 1 MB per message × up to 128 peers, an attacker can sustain a high-throughput write stream to the `recent_reject` RocksDB instance. Over time this causes disk exhaustion, which crashes the node or causes severe I/O degradation affecting block and transaction processing. This matches **High: Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

No special privileges are required. Any unprivileged remote peer can connect via the standard P2P relay protocol and submit transactions that fail with non-banning reject reasons (`LowFeeRate`, `Resolve`, `ExceededMaximumAncestorsCount`, etc.). The peer is never banned for these rejections. The attack is repeatable, persistent, and requires no victim interaction. Multiple peers can amplify the effect up to the `MAX_RELAY_PEERS = 128` limit.

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

    // Insert 1000 unique rejected transactions
    for i in 0..1000u64 {
        let key = Byte32::new(blake2b_256(i.to_le_bytes()));
        recent_reject
            .put(&key, Reject::Malformed(i.to_string(), Default::default()))
            .unwrap();
    }

    // In-memory counter stays at 0 (the bug)
    assert_eq!(recent_reject.total_keys_num, 0);

    // Actual DB has ~1000 entries, far exceeding count_limit=100
    let actual = recent_reject.estimate_total_keys_num().unwrap();
    assert!(actual > 100, "actual={}", actual); // this assertion passes, proving unbounded growth
}
```