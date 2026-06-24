The code is confirmed. The vulnerability is real and exactly as described.

- `confirm_blocks_to_failed_txs` has length 1000 (line 140), same as `block_unconfirmed_txs` (line 141).
- In `remove_unconfirmed_tx` (lines 208–212), there is a bounds guard for `block_unconfirmed_txs`, but line 215 indexes `confirm_blocks_to_failed_txs[tx_age - 1]` with no guard.
- `drop_tx` passes `count_failure = true` (line 429), and `reject_tx` calls `drop_tx` (line 476).
- When `tx_age > 1000`, `tx_age - 1 >= 1000` is out of bounds → Rust panics unconditionally.

---

Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age` Exceeds Circular Buffer Size — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
The `ConfirmationFraction` fee estimator tracks unconfirmed transactions in two parallel fixed-size arrays of length 1000. When a tracked transaction is rejected or evicted after more than 1000 blocks, `remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` ring-buffer access but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a bounds check. Any `tx_age > 1000` causes a Rust index-out-of-bounds panic, crashing the node.

## Finding Description
`TxConfirmStat` initializes both arrays to length `MAX_CONFIRM_BLOCKS = 1000`:

```rust
// confirmation_fraction.rs L139-141
let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let confirm_blocks_to_failed_txs    = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let block_unconfirmed_txs           = vec![vec![0;    buckets.len()]; max_confirm_blocks];
```

In `remove_unconfirmed_tx` (L197–217):

```rust
let tx_age = tip_height.saturating_sub(entry_height) as usize;
if tx_age < 1 { return; }
if tx_age >= self.block_unconfirmed_txs.len() {   // guard for ring buffer ✓
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
    self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
}
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;  // NO guard ✗
}
```

The guard on L208 routes `block_unconfirmed_txs` safely, but L215 has no corresponding check. When `tx_age > 1000`, `tx_age - 1 >= 1000` is out of bounds for a `Vec` of length 1000. Rust panics unconditionally in both debug and release builds.

The call chain that sets `count_failure = true`:
- `reject_tx` (L475–477) → `drop_tx` (L428–430) → `drop_tx_inner(tx_hash, true)` (L416–425) → `remove_unconfirmed_tx(..., count_failure: true)`
- `remove_expired` (pool.rs L271–288) calls `callbacks.call_reject`, which invokes `fee_estimator.reject_tx`.

## Impact Explanation
When `ConfirmationFraction` is enabled and a tracked transaction remains unconfirmed for more than 1000 blocks before being rejected or evicted, the node panics and crashes. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. The crash is deterministic (Rust index-out-of-bounds panic, not UB), reproducible, and requires no special privileges beyond submitting a transaction.

## Likelihood Explanation
`ConfirmationFraction` is opt-in via `fee_estimator.algorithm = "ConfirmationFraction"` in `ckb.toml`. Once enabled, the trigger is trivial: submit a low-fee transaction that miners ignore, wait ~2.8 hours (1001 blocks at 10 s/block), then wait for the default 12-hour expiry timer (`remove_expired`) to fire. No authentication or elevated privilege is required — only the ability to call `send_transaction` via RPC. The condition is met automatically by any long-lived unconfirmed transaction.

## Recommendation
Add a bounds guard before indexing `confirm_blocks_to_failed_txs`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This ensures that failure counts are only recorded for transactions whose age falls within the tracked window, consistent with the design intent of `MAX_CONFIRM_BLOCKS`.

## Proof of Concept
1. Configure the node: `fee_estimator.algorithm = "ConfirmationFraction"` in `ckb.toml`.
2. Submit a transaction with a fee rate just above `min_fee_rate` but low enough that miners do not include it. The tx is accepted and tracked at height `H` with `best_height = H`.
3. Allow 1001+ blocks to be produced. The tx remains unconfirmed; `tx_age` grows beyond 1000.
4. Either wait for the 12-hour expiry timer (`remove_expired` → `callbacks.call_reject` → `fee_estimator.reject_tx`) or fill the pool to trigger `limit_size` eviction.
5. `remove_unconfirmed_tx` is called with `tx_age > 1000` and `count_failure = true`.
6. `self.confirm_blocks_to_failed_txs[tx_age - 1]` panics: **thread panicked at 'index out of bounds: the len is 1000 but the index is N'**, crashing the node.

Unit test to confirm: construct a `TxConfirmStat` with `max_confirm_blocks = 5`, call `add_unconfirmed_tx` at height 0, then call `remove_unconfirmed_tx(entry_height=0, tip_height=6, bucket_index, count_failure=true)` — this produces `tx_age = 6`, which panics at `confirm_blocks_to_failed_txs[5]` on a Vec of length 5. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L21-22)
```rust
/// The number of blocks that the estimator will trace the statistics.
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-430)
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

    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
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
