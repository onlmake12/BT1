Audit Report

## Title
Out-of-Bounds Index Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`TxConfirmStat::remove_unconfirmed_tx` guards `block_unconfirmed_txs` access for `tx_age >= 1000` but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a corresponding bounds check. When a tracked tx is evicted after more than 1000 blocks, `tx_age - 1 >= 1000` exceeds the Vec's length of 1000, causing a Rust index-out-of-bounds panic that crashes the tx-pool service thread.

## Finding Description

`confirm_blocks_to_failed_txs` is initialized with length `MAX_CONFIRM_BLOCKS = 1000`, giving valid indices `0..=999`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly routes `block_unconfirmed_txs` access for aged-out txs (`tx_age >= 1000`), but the `count_failure` branch immediately below has **no bounds check** on `tx_age`: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for a Vec of length 1000 → **panic**.

`drop_tx` always passes `count_failure = true`: [3](#0-2) 

`drop_tx_inner` passes `self.best_height` as `tip_height`, which is updated on every committed block: [4](#0-3) 

The pool eviction path (`remove_expired`, `limit_size`) calls `callbacks.call_reject`, which propagates to `fee_estimator.reject_tx` → `drop_tx`: [5](#0-4) 

## Impact Explanation

A Rust Vec index-out-of-bounds panics unconditionally at runtime. The tx-pool service thread panics, crashing the node's transaction processing. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

Any unprivileged user can submit a low-fee-rate tx via RPC (`SubmitLocalTx`) or P2P relay. The default pool expiry is 12 hours; at ~8 seconds/block, that is ~5400 blocks — far exceeding the 1001-block threshold. Any such tx that remains unconfirmed for >1000 blocks and is then evicted by pool size pressure or expiry will trigger the panic. No special privileges are required; the attacker only needs to submit a tx that will not be confirmed quickly (e.g., below-market fee rate).

## Recommendation

Add a bounds check before accessing `confirm_blocks_to_failed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Or use `.get_mut(tx_age - 1)` to silently skip out-of-range ages, consistent with how `block_unconfirmed_txs` already handles the overflow case via `old_unconfirmed_txs`.

## Proof of Concept

1. Start a CKB node with the fee estimator enabled (non-IBD mode).
2. Submit a tx with a fee rate below the market rate so it will not be confirmed.
3. Advance the chain by 1001+ blocks (e.g., via a private testnet or by waiting on mainnet).
4. Allow the pool expiry timer to fire (or trigger `limit_size` eviction).
5. The eviction path calls `remove_unconfirmed_tx` with `tx_age >= 1001` and `count_failure = true`.
6. `confirm_blocks_to_failed_txs[1000]` panics → node tx-pool thread crashes.

Minimal unit test:
```rust
#[test]
fn test_no_oob_on_old_tx_drop() {
    let buckets = vec![FeeRate::from_u64(1000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993);
    // tx entered at height 0, now at height 1001 → tx_age = 1001
    stat.remove_unconfirmed_tx(0, 1001, 0, true); // panics without fix
}
```

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-216)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L428-430)
```rust
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** tx-pool/src/pool.rs (L271-288)
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
    }
```
