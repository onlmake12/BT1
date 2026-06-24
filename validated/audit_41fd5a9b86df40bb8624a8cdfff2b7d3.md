Audit Report

## Title
Broken shrink counter in `RecentReject::put` allows unbounded disk growth via rejected transaction flood — (`tx-pool/src/component/recent_reject.rs`)

## Summary
In `RecentReject::put`, the incremented key count is computed into a local binding `total_keys_num` but is never written back to `self.total_keys_num`. As a result, `self.total_keys_num` remains at its initial estimate value across all subsequent calls, the shrink threshold is never crossed, and the underlying `DBWithTTL` RocksDB instance grows without bound. An attacker can exploit this by continuously submitting non-duplicate rejected transactions (e.g., low-fee-rate) to exhaust node disk space and crash the node.

## Finding Description
In `RecentReject::put` at lines 62–65 of `tx-pool/src/component/recent_reject.rs`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // self.total_keys_num is NEVER assigned here
} else {
    self.shrink()?;
}
```

`total_keys_num` is a local binding. `self.total_keys_num` is initialized once in `build()` from `estimate_total_keys_num()` (0 for a fresh DB). On every subsequent call to `put()`, the condition evaluates `0 + 1 > count_limit`, which is false for any `count_limit >= 1`. `shrink()` is therefore never invoked through the normal path.

`shrink()` does correctly update `self.total_keys_num` at line 111, but it is unreachable because the triggering condition is never satisfied.

The existing unit test at line 39 of `tx-pool/src/component/tests/recent_reject.rs` asserts `recent_reject.total_keys_num < 100`. This passes vacuously since `total_keys_num` stays at 0 — it does not verify that shrink was triggered or that the DB is bounded.

The exploit path is: remote peer → P2P relay → `after_process` (lines 522–524 of `tx-pool/src/process.rs`) → `put_recent_reject` → `RecentReject::put`. `should_recorded()` returns `true` for all `Reject` variants except `Duplicated`, so any non-duplicate rejection triggers a DB write. Non-malformed rejections (`LowFeeRate`, `Full`, `ExceededMaximumAncestorsCount`) do not trigger peer banning (only `is_malformed_tx()` triggers `ban_malformed` at line 514–515), so an attacker can continuously submit policy-rejected transactions from a single peer without being disconnected.

## Impact Explanation
The `count_limit` bound on the recent-reject DB is completely non-functional. The DB grows proportionally to the number of rejected transactions received since node startup, bounded only by the TTL (minimum 1 day). At even modest submission rates, disk exhaustion is achievable within hours, causing the node to crash or become inoperable. This maps to: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
An unprivileged remote peer can submit structurally valid but policy-rejected transactions (e.g., `LowFeeRate`) indefinitely without being banned. No special privileges, leaked keys, or victim mistakes are required. The attack is repeatable and low-cost: submitting transactions with fee rates just below the minimum threshold is trivially automatable. The `recent_reject` DB is enabled by default when `recent_reject` path is configured in `TxPoolConfig`.

## Recommendation
Add `self.total_keys_num = total_keys_num;` in the non-shrink branch of `put()`, immediately after the `checked_add`:

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    self.total_keys_num = total_keys_num;  // ← add this line
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
} else {
    self.shrink()?;
}
```

Also update the unit test to assert that the actual DB key count (via `estimate_total_keys_num`) is bounded by `count_limit * shard_num` after many inserts, not just that `total_keys_num < limit`.

## Proof of Concept
1. Build a `RecentReject` with `count_limit = 10`, `shard_num = 2`, `ttl = -1`.
2. Insert 10,000 distinct rejected tx hashes via `put()`.
3. Observe: `self.total_keys_num == 0` (never incremented).
4. Observe: `db.estimate_num_keys_cf` sum ≈ 10,000, far exceeding `count_limit * shard_num = 20`.
5. Observe: disk usage grows linearly with insert count.

The existing test already inadvertently demonstrates this: after 160 inserts with `limit=100`, `total_keys_num` is 0, not a post-shrink estimate. The assertion `total_keys_num < 100` passes vacuously. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** tx-pool/src/process.rs (L513-524)
```rust
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
