Let me check the tx-pool expiry settings and how `drop_tx` is called from the tx-pool service.

The vulnerability is real. Here is the complete analysis:

---

### Title
Out-of-Bounds Index Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access when `tx_age >= 1000`, but performs an **unguarded** index into `confirm_blocks_to_failed_txs[tx_age - 1]` immediately after. When `tx_age > 1000`, `tx_age - 1 >= 1000` is out of bounds for a `Vec` of length 1000, causing a Rust index-out-of-bounds panic that crashes the tx-pool service thread.

### Finding Description

`confirm_blocks_to_failed_txs` is initialized with length `MAX_CONFIRM_BLOCKS = 1000` (valid indices `0..=999`): [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly handles the `block_unconfirmed_txs` access for `tx_age >= 1000`: [2](#0-1) 

But the subsequent `count_failure` branch has **no corresponding bounds check**: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is an out-of-bounds access on a `Vec` of length 1000 → **panic**.

### Impact Explanation

`drop_tx` always passes `count_failure = true`: [4](#0-3) 

`reject_tx` calls `drop_tx`: [5](#0-4) 

The tx-pool eviction callbacks wire directly to `fee_estimator.reject_tx`: [6](#0-5) 

Both `remove_expired` and `limit_size` (called on every block update) invoke `callbacks.call_reject`: [7](#0-6) 

A panic in the fee estimator write-lock propagates to the tx-pool service, crashing the node.

### Likelihood Explanation

The default `expiry_hours = 12`: [8](#0-7) 

At CKB's ~8-second block time, 1001 blocks ≈ 2.2 hours — well within the 12-hour expiry window. Any valid tx that sits unconfirmed for 1001+ blocks (e.g., a low-fee-rate tx during congestion) will trigger the panic upon expiry eviction. No special privileges are required: any unprivileged user can submit a valid tx via P2P or RPC and simply wait.

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard for `block_unconfirmed_txs` and clamps failure recording to the tracked window.

### Proof of Concept

```rust
// In confirmation_fraction.rs tests:
#[test]
fn test_no_panic_on_old_drop() {
    let mut algo = Algorithm::new();
    algo.is_ready = true;
    algo.best_height = 0;
    algo.current_tip = 0;

    // Submit tx at height 0
    let tx_hash = Byte32::zero();
    algo.accept_tx(tx_hash.clone(), /* valid TxEntryInfo */);

    // Advance 1001 blocks without committing the tx
    for h in 1..=1001u64 {
        algo.commit_block(&make_empty_block(h)); // no tx_hash in block
    }
    // best_height = 1001, tx entry_height = 0, tx_age = 1001
    // confirm_blocks_to_failed_txs[1000] → index 1000 on Vec of len 1000 → PANIC
    algo.reject_tx(&tx_hash); // must not panic
}
```

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
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

**File:** shared/src/shared_builder.rs (L598-601)
```rust

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```

**File:** util/app-config/src/legacy/tx_pool.rs (L17-18)
```rust
// Default expiration time for pool transactions in hours
const DEFAULT_EXPIRY_HOURS: u8 = 12;
```
