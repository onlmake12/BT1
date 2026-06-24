Audit Report

## Title
Unconditional Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` access against `tx_age >= len` (1000), but applies no equivalent guard to the `confirm_blocks_to_failed_txs[tx_age - 1]` write. When a tracked transaction is rejected after 1001+ blocks, `tx_age - 1 >= 1000` indexes past the end of the 1000-element `Vec`, causing an unconditional Rust index-out-of-bounds panic that crashes the node process. An unprivileged remote peer can trigger this by submitting a valid transaction and waiting approximately 2.2–2.8 hours for it to be rejected.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized with length `max_confirm_blocks` (= `MAX_CONFIRM_BLOCKS` = 1000): [1](#0-0) 

In `remove_unconfirmed_tx`, the `block_unconfirmed_txs` access is correctly guarded: [2](#0-1) 

But the `count_failure` branch at line 215 is **outside** that guard and has no bounds check: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is past the last valid index (999) of the 1000-element `confirm_blocks_to_failed_txs`. Rust panics unconditionally with an index-out-of-bounds.

The call chain is fully confirmed: `remove_expired` → `callbacks.call_reject` → `reject_tx` → `drop_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx`. [4](#0-3) [5](#0-4) [6](#0-5) 

## Impact Explanation
The panic propagates up through the tx-pool service thread and crashes the node process. This matches the **High** impact class: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points). The crash is unconditional once triggered — there is no recovery path from a Rust index-out-of-bounds panic.

## Likelihood Explanation
CKB produces blocks roughly every 8–10 seconds; 1001 blocks ≈ 2.2–2.8 hours. The default tx-pool expiry is time-based (typically 12 hours), so a transaction easily survives 1001 blocks within the expiry window. When expiry fires, `remove_expired` calls `callbacks.call_reject`, which chains to `drop_tx(count_failure=true)` and triggers the panic. Alternatively, an attacker can submit a conflicting (double-spend) transaction after 1001 blocks to trigger the same rejection path immediately. No special privileges, PoW, or majority hashpower are required — only the ability to submit a valid transaction.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard on `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently drops the failure sample for transactions older than `MAX_CONFIRM_BLOCKS`, consistent with how `block_unconfirmed_txs` already handles the same case.

## Proof of Concept
```rust
// Reproduces with max_confirm_blocks = 10
let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
let mut stat = TxConfirmStat::new(buckets, 10, 0.993);

// Track a tx at height 0
let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

// 11 blocks later, reject it with count_failure=true
// tx_age = 11, confirm_blocks_to_failed_txs[10] → index 10 on a 10-element Vec → PANIC
stat.remove_unconfirmed_tx(0, 11, bucket_index, true);
```

In production: submit a valid tx at height H, wait for `best_height` to reach H+1001 (≈2.2 hours), then either wait for expiry or submit a conflicting tx. The original tx is rejected via `reject_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` → `confirm_blocks_to_failed_txs[1000]` → **index out of bounds → node process crash**.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-213)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L214-216)
```rust
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
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
