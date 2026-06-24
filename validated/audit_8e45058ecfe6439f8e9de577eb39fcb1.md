Audit Report

## Title
Fee Admission Check Uses Only Serialized Size, Not Cycle-Weighted Cost, Enabling Cheap Script-Execution DoS — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` enforces the minimum fee using only the transaction's serialized byte size, not its cycle-weighted cost (`max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because script execution occurs after this check and no second weight-based fee gate exists after cycles are known, an attacker can submit transactions with tiny serialized size but near-maximum cycle consumption while paying only the size-based minimum fee, forcing every receiving node to execute up to `max_block_cycles` VM cycles per transaction at a cost far below the actual resource consumed.

## Finding Description
In `tx-pool/src/util.rs` at lines 42–45, `check_tx_fee` computes the minimum required fee using only `tx_size`:

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code's own comment acknowledges the theoretical gap. This function is called inside `pre_check`, which runs before `verify_rtx` (script execution) in `_process_tx` at `tx-pool/src/process.rs` lines 715–717:

```rust
let (ret, snapshot) = self.pre_check(&tx).await;
let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
```

After `verify_rtx` returns the actual consumed cycles at line 734, a `TxEntry` is created and submitted at lines 751–754 with no second fee check against the cycle-weighted cost:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

`TxEntry::fee_rate()` in `tx-pool/src/component/entry.rs` at lines 115–118 does compute the correct weight-based fee rate using `get_transaction_weight(self.size, self.cycles)`, but this is used only for eviction scoring — never as an admission gate.

The weight formula in `util/types/src/core/tx_pool.rs` at lines 298–303 is:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

With `DEFAULT_BYTES_PER_CYCLES = 0.000_170_571_4` (line 279), a 200-byte transaction consuming 69,000,000 cycles has an actual weight of `max(200, 69_000_000 × 0.000_170_571_4) ≈ 11,769` bytes — roughly 59× larger than its serialized size. The size-only gate charges 200 shannons; the correct weight-based gate would require ~11,769 shannons. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

## Impact Explanation
This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker who pre-deploys a high-cycle lock script and creates many cells locked by it can flood the network with transactions that each pass the size-based fee gate (200 shannons) while forcing every node to execute ~69M VM cycles per transaction. The accepted transactions occupy the pool and saturate verification workers. Because the transactions are accepted into the pool, they propagate via P2P relay, multiplying the CPU load across the entire network. The attacker's cost per unit of forced computation is ~59× below what the fee model intends to charge.

## Likelihood Explanation
The attack is reachable by any unprivileged actor via the `send_transaction` RPC endpoint or P2P transaction relay (`submit_remote_tx`). No special role, key, or hashpower is required. The default parameters `min_fee_rate = 1_000 shannons/KB` and `max_tx_verify_cycles = 70_000_000` are public consensus parameters, making the attack fully predictable and repeatable. The attacker's on-chain setup cost (deploying the script, creating UTXOs) is a one-time investment. [5](#0-4) 

## Recommendation
After `verify_rtx` returns the actual consumed cycles, perform a second fee check using the cycle-weighted transaction weight before accepting the entry into the pool:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool.config.min_fee_rate.fee(actual_weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool.config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This mirrors how `TxEntry::fee_rate()` already computes the effective fee rate and ensures the admission gate is consistent with the actual resource cost of script execution. [3](#0-2) 

## Proof of Concept
1. Deploy a lock script cell whose script body loops for exactly `N = 69,000,000` cycles.
2. Create multiple cells locked by that script (each cell requires only minimum CKB capacity).
3. Construct a transaction spending one such cell:
   - Serialized size ≈ 200 bytes
   - Fee = `ceil(1000 × 200 / 1000)` = 200 shannons (satisfies size-only gate)
4. Submit via `send_transaction` RPC.
5. Observe: `pre_check` passes (200 shannons ≥ size-based minimum). Node runs 69M VM cycles via `verify_rtx`. Transaction is accepted into the pool with no cycle-weighted fee rejection.
6. Actual effective fee rate = `200 / 11,769 × 1000 ≈ 17 shannons/KW` — far below `min_fee_rate = 1000 shannons/KW`.
7. Repeat in a tight loop from multiple connections, spending different pre-created UTXOs, to saturate node verification workers across the network. [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }
```

**File:** tx-pool/src/process.rs (L715-754)
```rust
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
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/types/src/core/tx_pool.rs (L279-303)
```rust
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
