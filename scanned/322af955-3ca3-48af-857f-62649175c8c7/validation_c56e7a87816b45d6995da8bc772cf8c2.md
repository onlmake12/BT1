### Title
Minimum 1-Block Lookback Window in `get_fee_rate_statistics` Enables Single-Block Fee Rate Manipulation — (`File: rpc/src/util/fee_rate.rs`)

### Summary
The `get_fee_rate_statistics` (and its deprecated alias `get_fee_rate_statics`) RPC method accepts a caller-supplied `target` parameter that is clamped to a minimum of `1` block. This allows any RPC caller to request fee rate statistics computed over a single block's worth of transactions. A miner who mines that single block can fill it with self-transactions at an artificially high fee rate, causing the RPC to return a manipulated mean/median that misleads wallets and applications into overpaying fees.

### Finding Description

`rpc/src/util/fee_rate.rs` defines three constants that govern the lookback window:

```rust
const DEFAULT_TARGET: u64 = 21;
const MIN_TARGET: u64 = 1;
const MAX_TARGET: u64 = 101;
``` [1](#0-0) 

The `statistics()` method normalises the caller-supplied target but enforces no lower bound beyond `MIN_TARGET = 1`:

```rust
let mut target = target.unwrap_or(DEFAULT_TARGET);
if is_even(target) {
    target = target.saturating_add(1);   // 0 → 1
}
target = std::cmp::min(self.provider.max_target(), target);
``` [2](#0-1) 

`collect()` then computes the block range:

```rust
let start = std::cmp::max(
    MIN_TARGET,
    tip_number.saturating_add(1).saturating_sub(target),
);
``` [3](#0-2) 

When `target = 1`, `start = max(1, tip_number + 1 − 1) = tip_number`, so the iterator covers exactly the single tip block `[tip_number, tip_number]`. The RPC handler passes the caller's value through without any additional validation:

```rust
fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
    Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
        .statistics(target.map(Into::into)))
}
``` [4](#0-3) 

The same code path is shared by the deprecated `get_fee_rate_statics` alias. [5](#0-4) 

### Impact Explanation

A miner who mines the current tip block can pack it exclusively with self-to-self transactions carrying an arbitrarily high fee rate. Immediately after the block is confirmed, any RPC caller (including the miner's own tooling) that queries `get_fee_rate_statistics` with `target=1` receives a mean and median fee rate derived solely from those manipulated transactions. Wallets, DEX relayers, or other fee-estimation clients that honour a small target value will set their next transaction's fee rate to the inflated figure, paying excess fees that flow to the manipulating miner. The attack requires no consensus violation and leaves no on-chain evidence distinguishing it from a legitimate high-fee block.

### Likelihood Explanation

The entry path is fully unprivileged: any JSON-RPC caller can supply `target=1`. Mining a single block is a normal miner operation and requires no special access. The attack is cheap to execute repeatedly (once per block the miner wins) and is invisible to the victim. Wallets that expose a "fast" or "priority" fee preset by querying a small target are the primary victims.

### Recommendation

Raise `MIN_TARGET` to a value that spans enough blocks to smooth out single-block anomalies. Given CKB's ~10-second block time, a minimum of 10–21 blocks (roughly 100–210 seconds) would be analogous to the 2-minute TWAP floor adopted in the referenced fix. Alternatively, document that `target < N` is unsafe for fee estimation and enforce the floor server-side regardless of the caller's input.

### Proof of Concept

1. Miner mines block `T` containing only self-to-self transactions each paying 1 000 000 shannons/kW.
2. Attacker (or the miner) immediately calls:
   ```json
   {"method": "get_fee_rate_statistics", "params": ["0x1"]}
   ```
3. `statistics(Some(1))` → `target = 1` (odd, passes through) → `collect` iterates only block `T` → mean and median reflect the artificially high fee rate.
4. A wallet that calls the same RPC with `target=1` to populate its "priority fee" preset will submit its next transaction at the inflated rate, paying excess fees to the miner. [1](#0-0) [6](#0-5)

### Citations

**File:** rpc/src/util/fee_rate.rs (L6-8)
```rust
const DEFAULT_TARGET: u64 = 21;
const MIN_TARGET: u64 = 1;
const MAX_TARGET: u64 = 101;
```

**File:** rpc/src/util/fee_rate.rs (L40-43)
```rust
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );
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

**File:** rpc/src/module/chain.rs (L2124-2127)
```rust
    fn get_fee_rate_statics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```

**File:** rpc/src/module/chain.rs (L2129-2132)
```rust
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>> {
        Ok(FeeRateCollector::new(self.shared.snapshot().as_ref())
            .statistics(target.map(Into::into)))
    }
```
