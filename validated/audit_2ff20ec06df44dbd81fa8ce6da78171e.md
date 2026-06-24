Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries. In `remove_unconfirmed_tx`, the existing guard at line 208 only protects the `block_unconfirmed_txs` access; the unconditional index `confirm_blocks_to_failed_txs[tx_age - 1]` at line 215 has no corresponding bounds check. When a tracked transaction ages more than 1000 blocks before being rejected or evicted with `count_failure = true`, `tx_age - 1 >= 1000` exceeds the allocated length, causing a Rust index-out-of-bounds panic that terminates the node process.

## Finding Description

`MAX_CONFIRM_BLOCKS` is set to 1000 and both `confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are allocated with exactly that many entries: [1](#0-0) [2](#0-1) 

In `remove_unconfirmed_tx`, the guard at line 208 checks `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `tx_age >= 1000`) and redirects to `old_unconfirmed_txs` — but this guard only covers the `block_unconfirmed_txs` branch. The `count_failure` branch immediately below performs an unconditional index with no bounds check: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for a `Vec` of length 1000. Rust panics with an index-out-of-bounds error, terminating the process.

The full exploit path:
1. `accept_tx` calls `track_tx` with `self.current_tip` as `entry_height`, recording the tx at height H. [4](#0-3) 
2. After 1001+ blocks, `best_height = H + 1001`, so `tx_age = 1001`.
3. `reject_tx` → `drop_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` → panic at `confirm_blocks_to_failed_txs[1000]`. [5](#0-4) [6](#0-5) 

The `reject_tx` callback is triggered by `remove_expired` and `limit_size` via `callbacks.call_reject` in the tx pool, making this reachable by any unprivileged user who submits a low-fee transaction.

## Impact Explanation

The panic terminates the node process entirely. This matches the **High (10001–15000 points)** CKB bounty impact: *Vulnerabilities which could easily crash a CKB node*. Any peer with access to the `send_transaction` RPC can trigger this deterministically.

## Likelihood Explanation

At ~8 seconds per block, 1001 blocks is approximately 2.2 hours. A low-fee-rate transaction can easily remain in the mempool that long before time-based expiry (`remove_expired`) or size-based eviction (`limit_size`) triggers `reject_tx`. No special privilege, mining control, or network resource is required beyond submitting one valid transaction. The condition is repeatable and deterministic.

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
    let mut stat = TxConfirmStat::default();
    // confirm_blocks_to_failed_txs has exactly 1000 entries
    assert_eq!(stat.confirm_blocks_to_failed_txs.len(), 1000);

    // Simulate a tx tracked at height 0, removed at height 1001 (tx_age = 1001)
    // bucket_index=0 is valid; count_failure=true triggers the panic path
    stat.bucket_stats[0].old_unconfirmed_txs = 1;
    // Without the fix: panics with index 1000 out of bounds for len 1000
    stat.remove_unconfirmed_tx(0, 1001, 0, true);
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
