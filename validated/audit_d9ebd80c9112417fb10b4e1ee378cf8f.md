### Title
`FeeRateCollector::statistics` Panics on Any Block With `txs_sizes = None`, Breaking the `get_fee_rate_statistics` RPC Endpoint — (`File: rpc/src/util/fee_rate.rs`)

---

### Summary

`FeeRateCollector::statistics` iterates over a range of recent blocks and calls `.expect()` on `block_ext.txs_sizes` inside the fold closure. If any single block in that range has `txs_sizes = None` — which is a valid stored state — the entire RPC call panics rather than skipping that block gracefully. This is the direct CKB analog of the Bond `BondAggregator.findMarketFor` bug: a loop over a collection calls a sub-operation that can fail for certain items, and the failure of one item aborts the entire loop.

---

### Finding Description

In `rpc/src/util/fee_rate.rs`, `FeeRateCollector::statistics` calls `self.provider.collect(target, closure)`, which folds over all `BlockExt` records for the last `target` blocks (default 21): [1](#0-0) 

Inside the fold closure, line 93 unconditionally unwraps the optional field:

```rust
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
``` [2](#0-1) 

`txs_sizes` is typed `Option<Vec<u64>>` in `BlockExt`: [3](#0-2) 

The `None` state is a legitimate stored value. In `chain/src/verify.rs`, when `switch.disable_all()` is true, `insert_ok_ext` is called with `None` for both `cache_entries` and `txs_sizes`: [4](#0-3) 

By contrast, the normal verified path passes `Some(txs_sizes)`: [5](#0-4) 

The `filter_map` in `collect` already handles the case where `get_block_ext_by_number` returns `None` (i.e., the block record is entirely absent), but it does **not** handle the case where the record exists but `txs_sizes` is `None` inside it: [6](#0-5) 

So any block stored with `txs_sizes = None` that falls within the queried window causes an unconditional panic.

---

### Impact Explanation

The `get_fee_rate_statistics` RPC method (exposed in `rpc/src/module/chain.rs`) calls `FeeRateCollector::statistics`. Any RPC caller — including an unprivileged local or remote JSON-RPC client — can trigger this path. When the panic fires inside the RPC handler, it crashes the handler task. Depending on the Tokio runtime configuration, repeated panics can degrade or fully disable the RPC service. Fee-rate estimation, which miners and wallets depend on for block assembly and transaction submission, becomes unavailable. [7](#0-6) 

---

### Likelihood Explanation

Two realistic production scenarios produce blocks with `txs_sizes = None` within the recent-block window:

1. **Database migration / version upgrade**: `txs_sizes` is an optional field added in a later release. Blocks stored by an older node version have `txs_sizes = None`. After an upgrade, if the chain tip is within 21 blocks of the migration boundary, `statistics()` will query those legacy blocks and panic.

2. **`Switch::DISABLE_ALL` processing path**: Any code path that calls `insert_ok_ext` with `None` (line 723 of `chain/src/verify.rs`) produces a block record with `txs_sizes = None`. If such blocks appear in the recent window, the RPC panics.

Both scenarios are reachable without any privileged access; the trigger is simply calling `get_fee_rate_statistics` via the standard JSON-RPC interface.

---

### Recommendation

Replace the `.expect()` with a graceful skip, mirroring the fix applied to the Bond protocol (try-catch / continue on failure):

```rust
// Instead of:
let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");

// Use:
let Some(txs_sizes) = txs_sizes else {
    return fee_rates; // skip this block, continue fold
};
```

This ensures that a single block with `txs_sizes = None` does not abort the entire statistics computation, exactly as the Bond fix ensured a single `payoutFor` revert did not abort the entire `findMarketFor` loop.

---

### Proof of Concept

1. Run a CKB node that has processed any block via the `switch.disable_all()` path (or upgrade a node from a version that predates the `txs_sizes` field).
2. Ensure the chain tip is within 21 blocks of such a block.
3. Call the RPC:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"get_fee_rate_statistics","params":[]}
   ```
4. The node panics at `rpc/src/util/fee_rate.rs:93` with `"expect txs_size's length >= 1"`, crashing the RPC handler. [8](#0-7)

### Citations

**File:** rpc/src/util/fee_rate.rs (L35-48)
```rust
    fn collect<F>(&self, target: u64, f: F) -> Vec<u64>
    where
        F: FnMut(Vec<u64>, BlockExt) -> Vec<u64>,
    {
        let tip_number = self.get_tip_number();
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );

        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
    }
```

**File:** rpc/src/util/fee_rate.rs (L79-121)
```rust
    pub fn statistics(&self, target: Option<u64>) -> Option<FeeRateStatistics> {
        let mut target = target.unwrap_or(DEFAULT_TARGET);
        if is_even(target) {
            target = target.saturating_add(1);
        }
        target = std::cmp::min(self.provider.max_target(), target);

        let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
            let BlockExt {
                txs_sizes,
                cycles,
                txs_fees,
                ..
            } = block_ext;
            let txs_sizes = txs_sizes.expect("expect txs_size's length >= 1");
            if txs_sizes.len() > 1 && !txs_fees.is_empty() {
                // block_ext.txs_fees's length == block_ext.cycles's length
                // block_ext.txs_fees's length + 1 == txs_sizes's length
                if let Some(cycles) = cycles {
                    for (fee, cycles, size) in itertools::izip!(
                        txs_fees,
                        cycles,
                        txs_sizes.iter().skip(1) // skip cellbase (first element in the Vec)
                    ) {
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
                    }
                }
            }
            fee_rates
        });

        if fee_rates.is_empty() {
            None
        } else {
            Some(FeeRateStatistics {
                mean: mean(&fee_rates).into(),
                median: median(&mut fee_rates).into(),
            })
        }
    }
```

**File:** util/types/src/core/extras.rs (L476-506)
```rust
    /// The `EpochNumberWithFraction` constructed by this method has a potential risk that when
    /// call `self.to_rational()` may lead to a panic if the user specifies a zero epoch length.
    pub fn from_full_value_unchecked(value: u64) -> Self {
        Self(value)
    }

    /// Prevents leading to a panic if the `EpochNumberWithFraction` is constructed without checks.
    pub fn normalize(self) -> Self {
        if self.length() == 0 {
            Self::new(self.number(), 0, 1)
        } else {
            self
        }
    }

    /// Converts the epoch to an unsigned 256 bits rational.
    ///
    /// # Panics
    ///
    /// Only genesis epoch's length could be zero, otherwise causes a division-by-zero panic.
    pub fn to_rational(self) -> RationalU256 {
        if self.0 == 0 {
            RationalU256::zero()
        } else {
            RationalU256::new(self.index().into(), self.length().into()) + U256::from(self.number())
        }
    }

    /// Check if current value is the genesis block.
    pub fn is_genesis(&self) -> bool {
        self.number() == 0 && self.index() == 0 && self.length() == 0
```

**File:** chain/src/verify.rs (L674-692)
```rust
                                Ok((cycles, cache_entries)) => {
                                    let txs_sizes = resolved
                                        .iter()
                                        .map(|rtx| {
                                            rtx.transaction.data().serialized_size_in_block() as u64
                                        })
                                        .collect();
                                    txn.attach_block(b)?;
                                    attach_block_cell(&txn, b)?;
                                    mmr.push(b.digest())
                                        .map_err(|e| InternalErrorKind::MMR.other(e))?;

                                    self.insert_ok_ext(
                                        &txn,
                                        &b.header().hash(),
                                        ext.clone(),
                                        Some(&cache_entries),
                                        Some(txs_sizes),
                                    )?;
```

**File:** chain/src/verify.rs (L718-724)
```rust
            } else {
                txn.attach_block(b)?;
                attach_block_cell(&txn, b)?;
                mmr.push(b.digest())
                    .map_err(|e| InternalErrorKind::MMR.other(e))?;
                self.insert_ok_ext(&txn, &b.header().hash(), ext.clone(), None, None)?;
            }
```
