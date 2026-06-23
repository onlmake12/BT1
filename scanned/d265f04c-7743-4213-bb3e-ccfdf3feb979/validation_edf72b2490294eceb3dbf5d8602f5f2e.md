### Title
Tx-Pool Admission Assumes `weight = tx_size`, Ignoring Cycles — Allows Sub-`min_fee_rate` Transactions Into the Pool - (File: `tx-pool/src/util.rs`)

### Summary

The `check_tx_fee` function in `tx-pool/src/util.rs` enforces the `min_fee_rate` threshold by computing the minimum required fee using only the serialized transaction size as the weight. However, the canonical transaction weight used everywhere else in the system — block assembly, fee-rate sorting, and eviction — is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For computationally expensive transactions, cycles dominate the true weight by up to ~60×. An unprivileged `send_transaction` RPC caller can craft a transaction that passes the size-only fee check but whose actual fee rate is far below `min_fee_rate`, causing it to be admitted to the pool and relayed to peers.

### Finding Description

In `tx-pool/src/util.rs`, `check_tx_fee` computes the minimum required fee as:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
``` [1](#0-0) 

The canonical weight function used by `TxEntry::fee_rate()`, block assembly, and eviction is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

where `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4`. [3](#0-2) 

The full processing pipeline in `_process_tx` is:

1. `pre_check` → `check_tx_fee(tx_size)` — size-only fee gate
2. `verify_rtx` — determines actual cycles
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` — entry created with real cycles
4. `submit_entry` — admitted to pool with no second fee-rate check [4](#0-3) 

There is no second fee-rate check after cycles are known. The `TxEntry::fee_rate()` method uses the true weight: [5](#0-4) 

but this is only consulted for eviction and sorting, not for admission gating.

### Impact Explanation

An attacker submits a transaction with:
- `tx_size = 200` bytes (small)
- `cycles = 70,000,000` (at `max_tx_verify_cycles` limit)
- `fee = min_fee_rate * tx_size / 1000 + 1 = 201 shannons`

Fee check passes: `201 >= 1000 * 200 / 1000 = 200`. ✓

True weight: `max(200, 70,000,000 × 0.000_170_571_4) = max(200, 11,940) = 11,940`.

True fee rate: `201 × 1000 / 11,940 ≈ 16 shannons/KW` — roughly **60× below** `min_fee_rate`.

The transaction is admitted to the pool, relayed to all peers, and occupies pool space. Miners will not include it because its true fee rate is far too low. The pool's eviction mechanism (`limit_size`) uses the true fee rate and will eventually evict it, but only after it has consumed pool space and relay bandwidth. An attacker can continuously submit such transactions to keep the pool polluted with unmineable, low-fee-rate entries, degrading mempool quality and wasting relay resources across the network.

### Likelihood Explanation

Any unprivileged user with access to the `send_transaction` RPC endpoint can exploit this. Crafting a high-cycles transaction requires only deploying a script that performs expensive computation (e.g., a tight loop up to the cycle limit). No special privileges, keys, or network position are required. The `max_tx_verify_cycles` default of `TWO_IN_TWO_OUT_CYCLES * 20 = 70,000,000` is the only bound on the cycle count, and it is reachable by any script author. [6](#0-5) 

### Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight before calling `submit_entry`:

```rust
let true_weight = get_transaction_weight(tx_size, verified.cycles);
let true_min_fee = tx_pool_config.min_fee_rate.fee(true_weight);
if fee < true_min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This mirrors how `TxEntry::fee_rate()` and `get_transaction_weight` already compute the canonical weight, and closes the gap between the admission check and the true economic cost of including the transaction.

### Proof of Concept

1. Deploy a CKB script that consumes close to `max_tx_verify_cycles` (70,000,000) cycles via a tight computation loop.
2. Construct a transaction using that script as the lock, with `tx_size ≈ 200` bytes.
3. Set the transaction fee to `min_fee_rate * tx_size / 1000 + 1 = 201 shannons` (with default `min_fee_rate = 1000`).
4. Submit via `send_transaction` RPC.
5. Observe: the transaction is accepted into the pool (fee check passes on size-only weight of 200).
6. Compute true fee rate: `201 * 1000 / max(200, 70_000_000 * 0.000_170_571_4) = 201_000 / 11_940 ≈ 16 shannons/KW`.
7. Observe: the true fee rate (≈16) is ~62× below `min_fee_rate` (1000), yet the transaction is relayed to peers and sits in the pool until evicted by the size limiter. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/util.rs (L28-54)
```rust
pub(crate) fn check_tx_fee(
    tx_pool: &TxPool,
    snapshot: &Snapshot,
    rtx: &ResolvedTransaction,
    tx_size: usize,
) -> Result<Capacity, Reject> {
    let fee = DaoCalculator::new(snapshot.consensus(), &snapshot.borrow_as_data_loader())
        .transaction_fee(rtx)
        .map_err(|err| {
            Reject::Malformed(
                format!("{err}"),
                "expect (outputs capacity) <= (inputs capacity)".to_owned(),
            )
        })?;
    // Theoretically we cannot use size as weight directly to calculate fee_rate,
    // here min fee rate is used as a cheap check,
    // so we will use size to calculate fee_rate directly
    let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
    // reject txs which fee lower than min fee rate
    if fee < min_fee {
        let reject =
            Reject::LowFeeRate(tx_pool.config.min_fee_rate, min_fee.as_u64(), fee.as_u64());
        ckb_logger::debug!("Reject tx {}", reject);
        return Err(reject);
    }
    Ok(fee)
}
```

**File:** util/types/src/core/tx_pool.rs (L276-303)
```rust
/// Equal to MAX_BLOCK_BYTES / MAX_BLOCK_CYCLES, see ckb-chain-spec.
/// The precision is set so that the difference between MAX_BLOCK_CYCLES * DEFAULT_BYTES_PER_CYCLES
/// and MAX_BLOCK_BYTES is less than 1.
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;

/// vbytes has been deprecated, renamed to weight to prevent ambiguity
#[deprecated(
    since = "0.107.0",
    note = "Please use the get_transaction_weight instead"
)]
pub fn get_transaction_virtual_bytes(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}

/// The miners select transactions to fill the limited block space which gives the highest fee.
/// Because there are two different limits, serialized size and consumed cycles,
/// the selection algorithm is a multi-dimensional knapsack problem.
/// Introducing the transaction weight converts the multi-dimensional knapsack to a typical knapsack problem,
/// which has a simple greedy algorithm.
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
```

**File:** tx-pool/src/process.rs (L705-753)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L14-14)
```rust
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```
