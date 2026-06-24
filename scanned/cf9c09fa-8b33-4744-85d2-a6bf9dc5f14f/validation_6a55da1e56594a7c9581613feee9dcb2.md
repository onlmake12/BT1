Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access at line 208 but performs an unconditional index into `confirm_blocks_to_failed_txs[tx_age - 1]` at line 215 with no corresponding bounds check. When a tracked transaction ages more than 1000 blocks before being rejected or evicted, `tx_age - 1 >= 1000` exceeds the allocated length of `confirm_blocks_to_failed_txs`, causing a Rust index-out-of-bounds panic that terminates the node process.

## Finding Description

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 only protects the `block_unconfirmed_txs` access. The `count_failure` branch at line 214–216 has no bounds check: [2](#0-1) 

When `tx_age >= 1001`, `tx_age - 1 >= 1000` is out of bounds for a `Vec` of length 1000. Rust panics on out-of-bounds indexing, terminating the process.

The exploit path is fully reachable:
1. `accept_tx` calls `track_tx` with `self.current_tip` as `entry_height`, recording the tx at height H. [3](#0-2) 
2. `drop_tx_inner` calls `remove_unconfirmed_tx` with `self.best_height` as `tip_height`. [4](#0-3) 
3. After 1001+ blocks, `best_height = H + 1001`, so `tx_age = 1001`.
4. `reject_tx` → `drop_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` → panic at `confirm_blocks_to_failed_txs[1000]`. [5](#0-4) 
5. `reject_tx` is triggered by the fee estimator callback registered in `shared_builder.rs`, which is called from `remove_expired` and `limit_size` via `callbacks.call_reject`. [6](#0-5) 

## Impact Explanation

The panic kills the node process entirely. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.** Any peer can crash a node by submitting a single valid low-fee transaction and waiting for natural expiry or pool eviction after 1001+ blocks.

## Likelihood Explanation

At ~8 seconds per block, 1001 blocks ≈ 2.2 hours. A low-fee-rate transaction can easily remain in the pool that long before time-based expiry (`remove_expired`) or size-based eviction (`limit_size`) triggers. No special privilege is required — any user who can call the `send_transaction` RPC can submit such a transaction. The attacker does not need to control mining or any network resource beyond submitting one valid transaction. The condition is repeatable and deterministic.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard pattern used for `block_unconfirmed_txs`:

```rust
if count_failure {
    let max = self.confirm_blocks_to_failed_txs.len();
    if tx_age >= 1 && tx_age <= max {
        self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
    }
}
```

## Proof of Concept

```rust
#[test]
fn test_no_oob_panic_when_tx_age_exceeds_max_confirm_blocks() {
    let stat = TxConfirmStat::default();
    // confirm_blocks_to_failed_txs has exactly 1000 entries
    assert_eq!(stat.confirm_blocks_to_failed_txs.len(), 1000);

    // Directly call remove_unconfirmed_tx with tx_age = 1001 (entry=0, tip=1001)
    // bucket_index=0 is valid; count_failure=true triggers the panic path
    let mut stat = TxConfirmStat::default();
    // Manually set old_unconfirmed_txs to avoid underflow on the guard branch
    stat.bucket_stats[0].old_unconfirmed_txs = 1;
    // This panics without the fix: index 1000 out of bounds for len 1000
    stat.remove_unconfirmed_tx(0, 1001, 0, true);
}
```

### Citations

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-425)
```rust
    fn drop_tx_inner(&mut self, tx_hash: &Byte32, count_failure: bool) -> Option<TxRecord> {
        self.tracked_txs.remove(tx_hash).inspect(|tx_record| {
            self.tx_confirm_stat.remove_unconfirmed_tx(
                tx_record.height,
                self.best_height,
                tx_record.bucket_index,
                count_failure,
            );
        })
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-473)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
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
