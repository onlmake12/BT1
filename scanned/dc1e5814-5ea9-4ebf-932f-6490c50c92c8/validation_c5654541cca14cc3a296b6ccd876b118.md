Audit Report

## Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
In `TxConfirmStat::remove_unconfirmed_tx`, a bounds check guards the `block_unconfirmed_txs` array access when `tx_age >= 1000`, but no equivalent guard exists before the `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215. When a tracked transaction has been pending for more than `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted with `count_failure=true`, the index `tx_age - 1 >= 1000` exceeds the array length of 1000, causing a Rust index-out-of-bounds panic that crashes the node.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized with exactly `max_confirm_blocks = 1000` entries at line 140, giving valid indices `0..=999`. In `remove_unconfirmed_tx` (lines 197–217), the guard at line 208 (`if tx_age >= self.block_unconfirmed_txs.len()`) correctly routes old transactions away from the `block_unconfirmed_txs` array. However, the `count_failure` branch at lines 214–216 has no such guard:

```rust
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

When `tx_age == 1001`, `tx_age - 1 == 1000` is out of bounds. The call chain that reaches this with `count_failure=true` is: `reject_tx` (line 475) → `drop_tx` (line 428–430, hardcodes `count_failure=true`) → `drop_tx_inner` (lines 416–425) → `remove_unconfirmed_tx`. The `FeeEstimator::reject_tx` dispatcher at `mod.rs` lines 84–89 confirms this path is only active for the `ConfirmationFraction` variant.

## Impact Explanation
A Rust index-out-of-bounds panic in a synchronous write-lock context (`algo.write().reject_tx(tx_hash)`) terminates the thread holding the lock, crashing the CKB node process. This matches the **High** impact category: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

## Likelihood Explanation
The preconditions are fully attainable by an unprivileged external user: (1) submit a low-fee transaction via RPC or P2P relay — no special access required; (2) the transaction is tracked normally by the estimator at submission time; (3) the chain advances more than 1000 blocks without confirming the transaction (achievable by deliberate fee underpricing); (4) eviction via `limit_size` or RBF replacement triggers `callbacks.call_reject` → `fee_estimator.reject_tx`. The only configuration requirement is that `ConfirmationFraction` is the active algorithm, which is a supported and documented mode.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` access, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently drops failure accounting for transactions older than `MAX_CONFIRM_BLOCKS`, consistent with how `block_unconfirmed_txs` already handles such transactions.

## Proof of Concept
```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // tx_age = 1001 > 1000 = confirm_blocks_to_failed_txs.len()
    // confirm_blocks_to_failed_txs[1000] → index out of bounds, PANIC
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running against the unpatched code panics with:
```
thread 'test' panicked at 'index out of bounds: the len is 1000 but the index is 1000'
```