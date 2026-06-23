The code is fully readable. Let me analyze the exact bug precisely.

**The core issue in `put()`** (lines 62–65):

```rust
if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
    if total_keys_num > self.count_limit {
        self.shrink()?;
    }
    // ← self.total_keys_num is NEVER assigned here
}
```

`total_keys_num` is a **local binding** — it is computed but never written back to `self.total_keys_num`. So `self.total_keys_num` stays at its initial estimate (0 for a fresh DB) forever. The condition `total_keys_num > self.count_limit` evaluates `0 + 1 > count_limit`, which is only true when `count_limit == 0`. For any `count_limit >= 1`, `shrink()` is never called.

`shrink()` does correctly update `self.total_keys_num` at line 111, but it is unreachable via the normal path.

The existing test at line 39 (`assert!(recent_reject.total_keys_num < 100)`) passes vacuously — `total_keys_num` stays at 0, so `0 < 100` is always true. The test does not verify that shrink was actually triggered.

`should_recorded()` returns `true` for every `Reject` variant except `Duplicated` (line 101), so any non-duplicate rejection (LowFeeRate, Verification, Resolve, Full, etc.) is stored. Non-malformed rejections (LowFeeRate, Full, ExceededMaximumAncestorsCount) do not trigger peer banning, so an attacker can continuously submit low-fee transactions without being banned.

The TTL (minimum 1 day) provides partial mitigation — entries expire — but the steady-state DB size is bounded only by `rate × TTL × entry_size`, not by `count_limit`. At even modest rates (e.g., 1000 tx/s), this is ~8.6M entries/day × ~200 bytes ≈ 1.7 GB/day, growing until disk exhaustion.

---

### Title
Broken shrink counter in `RecentReject::put` allows unbounded disk growth via rejected transaction flood — (`tx-pool/src/component/recent_reject.rs`)

### Summary
`RecentReject::put` computes an incremented key count into a local variable but never writes it back to `self.total_keys_num`. As a result, the shrink threshold is never crossed, the `count_limit` bound is never enforced, and the underlying `DBWithTTL` RocksDB instance grows without bound as long as an attacker can submit rejected transactions.

### Finding Description
In `RecentReject::put` [1](#0-0) , the local binding `total_keys_num` is computed via `self.total_keys_num.checked_add(1)` but is never assigned back to `self.total_keys_num`. Consequently, `self.total_keys_num` remains at its initial estimate value (0 for a fresh DB) across all subsequent calls. The condition `total_keys_num > self.count_limit` evaluates `1 > count_limit` on every call, which is false for any `count_limit >= 1`. `shrink()` is therefore never invoked through the normal path.

`shrink()` does correctly update `self.total_keys_num` at line 111 [2](#0-1) , but it is unreachable because the triggering condition is never satisfied.

The existing unit test [3](#0-2)  asserts `recent_reject.total_keys_num < 100`, which passes vacuously since `total_keys_num` stays at 0 — it does not verify that shrink was triggered or that the DB is bounded.

### Impact Explanation
The `count_limit` bound on the recent-reject DB is completely non-functional. The DB grows proportionally to the number of rejected transactions received since node startup (bounded only by the TTL). An attacker submitting rejected transactions at high rate can exhaust disk space, causing the node to crash or become inoperable. The `recent_reject` DB is enabled by default when `recent_reject` path is configured. [4](#0-3) 

### Likelihood Explanation
`should_recorded()` returns `true` for all `Reject` variants except `Duplicated` [5](#0-4) , so any non-duplicate rejection triggers a DB write. Non-malformed rejections (e.g., `LowFeeRate`, `Full`, `ExceededMaximumAncestorsCount`) do not trigger peer banning [6](#0-5) , so an attacker can continuously submit low-fee or structurally valid but policy-rejected transactions from a single peer without being disconnected. The attack path is: remote peer → P2P relay → `after_process` → `put_recent_reject` → `RecentReject::put`. [7](#0-6) 

### Recommendation
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

Also update the unit test to assert that the actual DB key count (via `estimate_total_keys_num`) is bounded by `count_limit * shard_num`, not just that `total_keys_num < limit`.

### Proof of Concept
1. Build a `RecentReject` with `count_limit = 10`, `shard_num = 2`, `ttl = -1`.
2. Insert 10,000 distinct rejected tx hashes.
3. Observe: `self.total_keys_num == 0` (never incremented).
4. Observe: `db.estimate_num_keys_cf` sum ≈ 10,000 >> `count_limit * shard_num = 20`.
5. Observe: disk usage grows linearly with insert count.

The existing test already inadvertently demonstrates this: after 160 inserts with `limit=100`, `total_keys_num` is 0, not a post-shrink estimate. [8](#0-7)

### Citations

**File:** tx-pool/src/component/recent_reject.rs (L62-65)
```rust
        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
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

**File:** util/types/src/core/tx_pool.rs (L100-102)
```rust
    pub fn should_recorded(&self) -> bool {
        !matches!(self, Reject::Duplicated(..))
    }
```

**File:** tx-pool/src/process.rs (L514-515)
```rust
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
```

**File:** tx-pool/src/process.rs (L522-524)
```rust
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
```
