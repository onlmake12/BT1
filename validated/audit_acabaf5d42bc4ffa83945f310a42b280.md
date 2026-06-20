### Title
Unbounded O(N log N) Recomputation in `sorted_flowed` Per `estimate_fee_rate` RPC Call — (`util/fee-estimator/src/estimator/weight_units_flow.rs`)

---

### Summary

The `WeightUnitsFlow` fee estimator stores every accepted transaction in an unbounded `HashMap<BlockNumber, Vec<TxStatus>>` over a 256-block historical window. On every `estimate_fee_rate` RPC call, `sorted_flowed` performs a full `flat_map` + `sort_unstable_by` over all stored entries with no caching. An unprivileged attacker who submits many valid transactions can inflate this store, making each RPC call proportionally more expensive.

---

### Finding Description

**Data structure with no per-block or total count cap:**

`Algorithm.txs` is declared as `HashMap<BlockNumber, Vec<TxStatus>>`. [1](#0-0) 

**`accept_tx` stores every accepted tx unconditionally:**

The only guard is `current_tip == 0`. There is no cap on entries per block or total entries. [2](#0-1) 

**`expire` only prunes by block age, never by count:**

`expire` retains all entries where `num >= expired_tip`, where the window is `historical_blocks(MAX_TARGET) = MAX_TARGET * 2 = 256` blocks. Txs committed to blocks are **not** removed from `self.txs`; they remain until their block number ages out. [3](#0-2) 

**`sorted_flowed` does O(N log N) work on every call, with no caching:**

Every invocation of `estimate_fee_rate` calls `do_estimate`, which calls `sorted_flowed`. `sorted_flowed` performs a `flat_map` over all `self.txs` entries in the historical window, collects them into a `Vec`, and sorts it. The result is never memoized or cached. [4](#0-3) 

**`do_estimate` calls `sorted_flowed` on every RPC invocation:** [5](#0-4) 

**`MAX_TARGET = 128` blocks, so `historical_blocks(MAX_TARGET) = 256`:** [6](#0-5) 

---

### Impact Explanation

Let N = total `TxStatus` entries accumulated in `self.txs` over the 256-block window. This includes:
- All txs currently in the pool (bounded by `max_tx_pool_size`)
- All txs committed to blocks over 256 blocks (not removed from `self.txs` on commit)

Each `estimate_fee_rate` RPC call costs O(N log N) CPU. With no caching and no rate limiting on the RPC, a caller who repeatedly invokes `estimate_fee_rate` while N is large causes sustained CPU amplification proportional to N log N per call. The `do_estimate` bucket-fill loop adds an additional O(max_bucket_index) pass per call. [7](#0-6) 

---

### Likelihood Explanation

The attacker is unprivileged and enters through the standard transaction submission path (P2P relay or `send_transaction` RPC). Valid txs require UTXOs, which limits the practical scale. However, the fee estimator accumulates historical flow including committed txs, so the effective N can exceed the current pool size. The RPC endpoint has no rate limiting or computation budget guard. The `WeightUnitsFlow` algorithm must be explicitly configured, reducing exposure to nodes that opt into it. [8](#0-7) 

---

### Recommendation

1. **Cache the `sorted_flowed` result** and invalidate it only when `self.txs` is mutated (i.e., on `accept_tx` or `commit_block`). Since `estimate_fee_rate` takes `&self`, the cache can be stored as an `Option<(BlockNumber, Vec<TxStatus>)>` keyed by `current_tip`.
2. **Cap `self.txs` per block**: enforce a maximum number of `TxStatus` entries per block number in `accept_tx` to bound N.
3. **Add a total-entry count guard** in `accept_tx` to reject new entries once a configurable ceiling is reached.

---

### Proof of Concept

```
1. Configure node with WeightUnitsFlow fee estimator.
2. Submit N valid txs per block for 256 blocks (N bounded by pool limits).
   -> Each accepted tx calls accept_tx, appending to self.txs[current_tip].
   -> Committed txs are NOT removed from self.txs.
3. Repeatedly call estimate_fee_rate RPC.
   -> Each call: sorted_flowed flat_maps 256*N TxStatus entries, sorts them O(256*N * log(256*N)).
   -> No cached result; full recomputation every call.
4. Benchmark: measure wall-clock time of estimate_fee_rate at N=0, 1000, 10000, 100000.
   -> Expect super-linear growth confirming the O(N log N) amplification.
``` [2](#0-1) [4](#0-3)

### Citations

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L68-74)
```rust
pub struct Algorithm {
    boot_tip: BlockNumber,
    current_tip: BlockNumber,
    txs: HashMap<BlockNumber, Vec<TxStatus>>,

    is_ready: bool,
}
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L147-151)
```rust
    fn expire(&mut self) {
        let historical_blocks = Self::historical_blocks(constants::MAX_TARGET);
        let expired_tip = self.current_tip.saturating_sub(historical_blocks);
        self.txs.retain(|&num, _| num >= expired_tip);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L153-162)
```rust
    pub fn accept_tx(&mut self, info: TxEntryInfo) {
        if self.current_tip == 0 {
            return;
        }
        let item = TxStatus::new_from_entry_info(info);
        self.txs
            .entry(self.current_tip)
            .and_modify(|items| items.push(item))
            .or_insert_with(|| vec![item]);
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L244-248)
```rust
        // Calculate flow speeds for buckets.
        let flow_speed_buckets = {
            let historical_tip = self.current_tip - historical_blocks;
            let sorted_flowed = self.sorted_flowed(historical_tip);
            let mut buckets = vec![0u64; max_bucket_index + 1];
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-298)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
            ckb_logger::trace!(
                ">>> bucket[{}]: {}; {} + {} - {}",
                bucket_index,
                passed,
                current_weight,
                added_weight,
                removed_weight
            );
            if passed {
                let fee_rate = Self::lowest_fee_rate_by_bucket_index(bucket_index);
                return Ok(fee_rate);
            }
        }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L303-313)
```rust
    fn sorted_flowed(&self, historical_tip: BlockNumber) -> Vec<TxStatus> {
        let mut statuses: Vec<_> = self
            .txs
            .iter()
            .filter(|&(&num, _)| num >= historical_tip)
            .flat_map(|(_, statuses)| statuses.to_owned())
            .collect();
        statuses.sort_unstable_by(|a, b| b.cmp(a));
        ckb_logger::trace!(">>> sorted flowed length: {}", statuses.len());
        statuses
    }
```

**File:** util/fee-estimator/src/constants.rs (L9-10)
```rust
/// Max target blocks, about 1 hour (128).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
```

**File:** util/app-config/src/configs/fee_estimator.rs (L12-18)
```rust
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Eq)]
pub enum Algorithm {
    /// Confirmation Fraction Fee Estimator
    ConfirmationFraction,
    /// Weight-Units Flow Fee Estimator
    WeightUnitsFlow,
}
```
