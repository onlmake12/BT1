### Title
Arithmetic Mean in `get_fee_rate_statistics` RPC Computed Over All Confirmed Transactions Without Outlier Filtering, Susceptible to Manipulation - (File: rpc/src/util/fee_rate.rs)

---

### Summary

`FeeRateCollector::statistics()` computes a plain arithmetic mean of **every** fee rate from every non-coinbase transaction in the last N confirmed blocks. Because no outlier filtering is applied, an unprivileged transaction sender can skew the returned `mean` field of `get_fee_rate_statistics` by flooding confirmed blocks with transactions carrying extreme fee rates. Wallets and tooling that consume the `mean` field to set their own fee rates are the downstream victims.

---

### Finding Description

`rpc/src/util/fee_rate.rs` exposes `FeeRateCollector::statistics()`, which iterates over every block in the target window and pushes the per-transaction fee rate into a flat `Vec<u64>` with no filtering:

```rust
// rpc/src/util/fee_rate.rs  lines 86-111
let mut fee_rates = self.provider.collect(target, |mut fee_rates, block_ext| {
    ...
    for (fee, cycles, size) in itertools::izip!(txs_fees, cycles, txs_sizes.iter().skip(1)) {
        let weight = get_transaction_weight(*size as usize, cycles);
        if weight > 0 {
            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
        }
    }
    fee_rates
});
```

The resulting vector is then passed to the unweighted arithmetic mean:

```rust
// rpc/src/util/fee_rate.rs  lines 14-18
fn mean(numbers: &[u64]) -> u64 {
    let sum: u128 = numbers.iter().map(|number| u128::from(*number)).sum();
    (sum / numbers.len() as u128) as u64
}
```

and the result is placed in the `mean` field of `FeeRateStatistics` returned by both `get_fee_rate_statistics` (line 1617) and the deprecated `get_fee_rate_statics` (line 1575).

The `collect()` helper iterates over **all** blocks from `tip - target + 1` to `tip` with no quality gate:

```rust
// rpc/src/util/fee_rate.rs  lines 45-47
let block_ext_iter =
    (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
block_ext_iter.fold(Vec::new(), f)
```

There is no minimum-liquidity check, no trimming of outliers, and no weighting by block fullness. A single block containing many attacker-controlled transactions with extreme fee rates contributes equally to the mean as a fully organic block.

---

### Impact Explanation

Any wallet, SDK, or tooling that reads the `mean` field from `get_fee_rate_statistics` to decide what fee rate to attach to a new transaction is exposed:

- **Inflated mean (high-fee attack):** Attacker submits many transactions paying abnormally high fees. Miners include them eagerly. The mean rises sharply. Downstream users overpay fees on every transaction until the attack window rolls out of the target range (up to 101 blocks).
- **Deflated mean (low-fee attack):** On a low-activity network, attacker submits many minimum-fee transactions. Miners include them to fill blocks. The mean drops. Downstream users underpay, causing their transactions to sit unconfirmed or be dropped from the pool — a targeted, fee-based denial of confirmation.

The `median` field returned alongside `mean` is resistant to this manipulation, but the `mean` field is explicitly documented and returned as a first-class output, making it a natural choice for simple wallet implementations.

---

### Likelihood Explanation

The attack entry point is the standard tx-pool submission path, accessible to any unprivileged peer or RPC caller. No special role, key, or majority hashpower is required. The attacker only needs to pay the fees for their own transactions — a cost proportional to the number of transactions needed to move the mean significantly. On a low-activity chain the cost is low; on a high-activity chain the attacker must outweigh organic traffic. The attack is fully repeatable and self-sustaining as long as the attacker keeps submitting transactions.

---

### Recommendation

1. **Stop exposing the arithmetic mean as a fee-rate signal.** The `median` is already computed and is outlier-resistant; it should be the sole recommended field. The `mean` field should be deprecated or removed from `FeeRateStatistics`.
2. **If a mean is retained**, apply a trimmed mean (e.g., discard the top and bottom 10 % of samples) before computing, analogous to replacing `quoteAllAvailablePoolsWithTimePeriod` with a curated, filtered subset.
3. **Weight by block occupancy**: blocks that are nearly empty (few organic transactions) should contribute less to the aggregate, preventing a sparse block filled with attacker transactions from dominating the result.

---

### Proof of Concept

1. Attacker calls `send_transaction` (or relays via P2P) to submit 500 transactions each paying a fee rate of 10 000 000 shannons/kB — far above the organic network rate.
2. Miners include them over the next few blocks (they are the highest-paying transactions).
3. A victim wallet calls `get_fee_rate_statistics` with default `target = 21`.
4. `FeeRateCollector::statistics()` collects all fee rates including the 500 attacker transactions; the arithmetic mean is pulled sharply upward.
5. The wallet reads `result.mean` and attaches that inflated fee rate to the user's next transaction.
6. The user overpays by an order of magnitude.

Conversely, on a quiet testnet or low-activity period, the attacker submits 500 transactions at the minimum fee rate (1 000 shannons/kB). The mean collapses toward the minimum. Wallets following the mean submit under-fee transactions that are never proposed, achieving a targeted confirmation-denial without any consensus-layer attack. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rpc/src/util/fee_rate.rs (L14-18)
```rust
fn mean(numbers: &[u64]) -> u64 {
    // The average of u64 values fits in u64, but the intermediate sum may not.
    let sum: u128 = numbers.iter().map(|number| u128::from(*number)).sum();
    (sum / numbers.len() as u128) as u64
}
```

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

**File:** rpc/src/util/fee_rate.rs (L86-111)
```rust
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
```

**File:** rpc/src/util/fee_rate.rs (L113-120)
```rust
        if fee_rates.is_empty() {
            None
        } else {
            Some(FeeRateStatistics {
                mean: mean(&fee_rates).into(),
                median: median(&mut fee_rates).into(),
            })
        }
```

**File:** rpc/src/module/chain.rs (L1571-1576)
```rust
    #[deprecated(
        since = "0.109.0",
        note = "Please use the RPC method [`get_fee_rate_statistics`](#chain-get_fee_rate_statistics) instead"
    )]
    #[rpc(name = "get_fee_rate_statics")]
    fn get_fee_rate_statics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>>;
```

**File:** rpc/src/module/chain.rs (L1617-1618)
```rust
    #[rpc(name = "get_fee_rate_statistics")]
    fn get_fee_rate_statistics(&self, target: Option<Uint64>) -> Result<Option<FeeRateStatistics>>;
```
