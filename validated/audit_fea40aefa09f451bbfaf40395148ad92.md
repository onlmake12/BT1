### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`File: util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

The `ConfirmationFraction` fee estimator's `remove_unconfirmed_tx` function allocates `confirm_blocks_to_failed_txs` with a fixed length of `MAX_CONFIRM_BLOCKS = 1000`, but unconditionally indexes it with `tx_age - 1` without bounding `tx_age` to that length. When a tracked transaction has been in the tx-pool for more than 1000 blocks and is then rejected, the index `tx_age - 1 >= 1000` causes a Rust index-out-of-bounds **panic**, crashing the node's tx-pool service thread.

---

### Finding Description

In `confirmation_fraction.rs`, `TxConfirmStat::new` initializes two parallel 2-D arrays of identical length `max_confirm_blocks = 1000`:

```rust
let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let confirm_blocks_to_failed_txs    = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
let block_unconfirmed_txs           = vec![vec![0;    buckets.len()]; max_confirm_blocks];
``` [1](#0-0) 

`remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` access:

```rust
if tx_age >= self.block_unconfirmed_txs.len() {   // tx_age >= 1000
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    ...
    self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
}
```

But then **unconditionally** accesses `confirm_blocks_to_failed_txs` with the same unbounded `tx_age`:

```rust
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
``` [2](#0-1) 

`confirm_blocks_to_failed_txs` has length 1000 (indices 0–999). When `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds → **panic**.

The call chain that reaches this with `count_failure = true`:

- `reject_tx` → `drop_tx` → `drop_tx_inner(tx_hash, count_failure=true)` → `remove_unconfirmed_tx(..., count_failure=true)` [3](#0-2) 

The `reject_tx` callback is registered in `shared_builder.rs` and fires whenever the tx-pool rejects a tracked transaction:

```rust
fee_estimator.reject_tx(&tx_hash);
``` [4](#0-3) 

The `FeeEstimator::reject_tx` dispatcher only invokes the inner logic for `ConfirmationFraction`:

```rust
Self::Dummy | Self::WeightUnitsFlow(_) => {}
Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
``` [5](#0-4) 

---

### Impact Explanation

A Rust index-out-of-bounds panic in the tx-pool service thread crashes the CKB node process. The fee estimator is embedded inside the tx-pool service (same async runtime), so a panic propagates to the service and terminates the node. This is a **remote denial-of-service**: any peer or user who can cause a tracked transaction to remain in the pool for more than 1000 blocks (~7.7 hours at the 28-second average block interval) and then be rejected will trigger the crash.

---

### Likelihood Explanation

A transaction enters tracking when it is accepted into the pending pool (`accept_tx` → `track_tx`). It stays tracked until confirmed or rejected. Transactions with very low fee rates, or transactions whose inputs become invalid after a reorg, can remain in the pool for extended periods. An attacker can deliberately submit a valid but low-fee transaction, wait for it to age past 1000 blocks, then trigger its eviction (e.g., via a conflicting spend or by waiting for the pool's own eviction logic). No privileged access is required; only the ability to submit a transaction to the RPC or P2P relay.

---

### Recommendation

Add a bounds check before indexing `confirm_blocks_to_failed_txs` in `remove_unconfirmed_tx`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This is the direct analog of the Reserve Protocol fix: the array sized by a fixed constant (`MAX_CONFIRM_BLOCKS`) must not be indexed by a value (`tx_age`) that can exceed that constant due to real-world state (a long-lived pool transaction).

---

### Proof of Concept

1. Node is configured with the `ConfirmationFraction` fee estimator.
2. Attacker submits a transaction `T` with a very low fee rate at block height `H`. The estimator calls `track_tx`, recording `TxRecord { height: H, bucket_index, fee_rate }`.
3. The chain advances to block `H + 1001` without `T` being confirmed. `best_height = H + 1001`.
4. Attacker (or the pool's own eviction) causes `T` to be rejected. The tx-pool callback fires `fee_estimator.reject_tx(&T.hash())`.
5. Inside `drop_tx_inner(T.hash(), count_failure=true)`, `remove_unconfirmed_tx` is called with `entry_height = H`, `tip_height = H + 1001`, `count_failure = true`.
6. `tx_age = (H + 1001) - H = 1001`. The guard `tx_age >= 1000` is true, so `block_unconfirmed_txs` is handled safely.
7. `self.confirm_blocks_to_failed_txs[1001 - 1]` = `confirm_blocks_to_failed_txs[1000]` — **index out of bounds on a length-1000 array → panic → node crash**. [2](#0-1)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L138-141)
```rust
        let bucket_stats = vec![BucketStat::default(); buckets.len()];
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L197-216)
```rust
    fn remove_unconfirmed_tx(
        &mut self,
        entry_height: u64,
        tip_height: u64,
        bucket_index: usize,
        count_failure: bool,
    ) {
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

**File:** shared/src/shared_builder.rs (L599-601)
```rust
            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
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
