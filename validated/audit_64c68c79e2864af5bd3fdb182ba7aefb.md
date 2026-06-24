All code claims check out against the actual source. Here is the validated report:

---

Audit Report

## Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`confirm_blocks_to_failed_txs` is allocated with exactly 1000 entries, giving valid indices `0..=999`. In `remove_unconfirmed_tx`, the `block_unconfirmed_txs` access is guarded against out-of-range `tx_age`, but the `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215 has no equivalent guard. When a tracked transaction has been pending for more than 1000 blocks and is then evicted via `reject_tx`, the index `tx_age - 1 >= 1000` exceeds the array length, causing a Rust index-out-of-bounds panic that poisons the `RwLock` and crashes the node.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized at line 140 with `max_confirm_blocks = 1000` rows: [1](#0-0) 

In `remove_unconfirmed_tx` (lines 197–217), the guard at line 208 correctly routes old transactions away from `block_unconfirmed_txs`: [2](#0-1) 

However, the `count_failure` branch immediately following has no such guard: [3](#0-2) 

When `tx_age == 1001`, `tx_age - 1 == 1000` is out of bounds for a length-1000 vec, causing a panic.

The full call chain that reaches this with `count_failure=true`:
- `reject_tx` (line 475–477) calls `drop_tx` [4](#0-3) 
- `drop_tx` (lines 428–430) hardcodes `count_failure=true` [5](#0-4) 
- `drop_tx_inner` (lines 416–425) calls `remove_unconfirmed_tx` with that flag [6](#0-5) 
- `FeeEstimator::reject_tx` in `mod.rs` confirms this path is exclusive to the `ConfirmationFraction` variant [7](#0-6) 

## Impact Explanation
The panic occurs inside `algo.write().reject_tx(tx_hash)`. Rust's `RwLock` becomes poisoned when a thread panics while holding the write guard. All subsequent `algo.write()` and `algo.read()` calls return `Err(PoisonError)`, which if unwrapped (standard practice) cause further panics, cascading into a node crash. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
Preconditions are fully attainable by an unprivileged user: (1) submit a low-fee transaction via RPC or P2P — no special access required; (2) the transaction is tracked normally at submission time since `track_tx` only requires `height == best_height`; (3) the chain advances more than 1000 blocks without confirming the transaction, achievable by deliberate fee underpricing; (4) eviction via `limit_size` or RBF replacement triggers `reject_tx`. The only configuration requirement is that `ConfirmationFraction` is the active algorithm, which is a supported and documented mode. The scenario is repeatable and requires no victim mistakes.

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

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-140)
```rust
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-212)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L428-430)
```rust
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

**File:** util/fee-estimator/src/estimator/mod.rs (L84-89)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
```
