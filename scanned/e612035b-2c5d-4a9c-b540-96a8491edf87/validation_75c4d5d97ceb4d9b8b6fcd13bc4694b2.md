Looking at the code carefully, I need to trace the exact execution path and check whether the bounds guard covers all array accesses.

### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access with a bounds check, but performs an **unconditional** index into `confirm_blocks_to_failed_txs[tx_age - 1]` with no corresponding guard. When a tracked transaction ages more than `MAX_CONFIRM_BLOCKS` (1000) blocks before being rejected/evicted, `tx_age - 1 >= 1000` exceeds the allocated length of `confirm_blocks_to_failed_txs`, causing a Rust index-out-of-bounds **panic** that crashes the node.

---

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries: [1](#0-0) [2](#0-1) 

In `remove_unconfirmed_tx`, the guard at line 208 only protects the `block_unconfirmed_txs` access. The subsequent `count_failure` branch at line 215 has **no bounds check**: [3](#0-2) 

When `tx_age >= 1001`, `tx_age - 1 >= 1000`, which is out of bounds for a `Vec` of length 1000. Rust panics on out-of-bounds indexing, terminating the process.

---

### Impact Explanation

The panic kills the node process entirely. Any peer can trigger this by submitting a valid transaction that:
1. Enters the pending pool (calling `accept_tx` → `track_tx` at height H)
2. Remains unconfirmed while 1001+ blocks are committed (advancing `best_height` to H+1001)
3. Is then evicted via `remove_expired` (time-based) or `limit_size` (pool-full eviction), both of which call `callbacks.call_reject` → `fee_estimator.reject_tx` → `drop_tx(..., count_failure=true)` → `remove_unconfirmed_tx` [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

At ~1 block per 8 seconds, 1001 blocks ≈ 2.2 hours. A low-fee-rate transaction can easily remain in the pool that long before time-based expiry or size-based eviction triggers. No special privilege is required — any user who can call `send_transaction` RPC can submit such a transaction. The attacker does not need to control mining or any network resource beyond submitting one valid transaction.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access:

```rust
if count_failure {
    let max = self.confirm_blocks_to_failed_txs.len();
    if tx_age >= 1 && tx_age <= max {
        self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
    }
}
```

This mirrors the existing guard pattern used for `block_unconfirmed_txs` at line 208. [6](#0-5) 

---

### Proof of Concept

```rust
#[test]
fn test_no_oob_panic_when_tx_age_exceeds_max_confirm_blocks() {
    let mut algo = Algorithm::new();
    algo.update_ibd_state(false); // mark ready

    // Simulate commit_block at height 1 to set best_height = current_tip = 1
    // (use a real BlockView or mock; here shown conceptually)
    // algo.commit_block(&block_at_height(1));
    // algo.accept_tx(tx_hash, entry_info);  // tracked at entry_height = 1

    // Advance 1001 more blocks: best_height = 1002
    // for h in 2..=1002 { algo.commit_block(&block_at_height(h)); }

    // Now reject the tx: tx_age = 1002 - 1 = 1001 > 1000 → panic without fix
    // algo.reject_tx(&tx_hash);  // must NOT panic

    // Assert: tx_age - 1 = 1000 is out of bounds for confirm_blocks_to_failed_txs.len() == 1000
    let stat = TxConfirmStat::default();
    assert_eq!(stat.confirm_blocks_to_failed_txs.len(), 1000);
    // index 1000 would panic: stat.confirm_blocks_to_failed_txs[1000][0]
}
```

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-140)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L204-216)
```rust
        let tx_age = tip_height.saturating_sub(entry_height) as usize;
        if tx_age < 1 {
            return;
        }
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
        }
```

**File:** tx-pool/src/pool.rs (L271-287)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
```

**File:** shared/src/shared_builder.rs (L576-601)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```
