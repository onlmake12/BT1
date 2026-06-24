Audit Report

## Title
Min-Fee-Rate Invariant Broken by Size-Only Weight Assumption in `check_tx_fee` — (File: tx-pool/src/util.rs)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the raw serialized byte size of a transaction as the weight, rather than the actual transaction weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because this check runs before script execution (cycles are unknown at that point), and no weight-based fee rate gate exists after cycles are determined, an unprivileged RPC caller can submit cycle-heavy, byte-small transactions that are admitted to the pool at effective fee rates far below the configured `min_fee_rate`, bypassing the pool's anti-spam invariant and consuming verification resources cheaply.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight, where weight is `max(tx_size_bytes, cycles × DEFAULT_BYTES_PER_CYCLES)`. [1](#0-0) 

`check_tx_fee` uses only `tx_size` as the weight: [2](#0-1) 

The code comment explicitly acknowledges this is theoretically incorrect, treating it as an acceptable "cheap check." The critical issue is that no weight-based fee rate check is performed after script execution completes and actual cycles are known.

The admission flow in `_process_tx` is:
1. `pre_check` → calls `check_tx_fee` with size only (cycles unknown)
2. `verify_rtx` → actual cycles determined
3. `submit_entry` → **no subsequent weight-based fee rate check** [3](#0-2) 

The weight-based `fee_rate()` method exists on `TxEntry` and uses `get_transaction_weight(self.size, self.cycles)`, but it is only used for pool ordering and eviction after admission — never as an admission gate. [4](#0-3) 

For a transaction with `cycles = 70,000,000` (the per-transaction cap) and serialized size of 100 bytes:
- Actual weight: `max(100, 70,000,000 × 0.000_170_571_4) ≈ 11,940 bytes`
- `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`
- Actual fee rate if 100 shannons paid: `100 × 1000 / 11,940 ≈ 8 shannons/KW` — **125× below the configured minimum of 1000 shannons/KW**

The transaction is admitted. Note: the report's claim that such transactions are "evicted last" is incorrect — since eviction uses actual weight-based fee rate, these transactions would be evicted *first* when the pool is full. However, this does not mitigate the core admission bypass.

## Impact Explanation

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

An attacker can continuously submit cycle-heavy, byte-small transactions via `send_transaction` RPC that are admitted at fee rates far below `min_fee_rate`. Each admitted transaction consumes up to 70M cycles of verification CPU resources while paying a fraction of the required fee. This undermines the anti-spam fee floor, pollutes the pool with economically underpriced work, and can be used to saturate the node's verification pipeline at negligible cost.

## Likelihood Explanation

- **Entry point**: `send_transaction` RPC, reachable by any unprivileged caller with no keys or special roles.
- **Craft cost**: attacker writes a CKB-VM script (e.g., a tight loop) consuming ~70M cycles, stored in a cell dep to keep the transaction body small (~100–200 bytes serialized).
- **No consensus violation**: the transaction is valid; it just pays less than the weight-based minimum.
- **Repeatability**: the attacker can submit many such transactions continuously, each consuming significant verification CPU. [5](#0-4) 

## Recommendation

After `verify_rtx` returns the actual cycle count, perform a second weight-based fee rate check before calling `submit_entry`. Alternatively, refactor `check_tx_fee` to accept a `cycles` parameter and use `get_transaction_weight(tx_size, cycles)` when cycles are available. If cycles are not yet known (pre-execution path), the size-only pre-check can remain as a necessary approximation, but a mandatory post-execution weight-based check must be added:

```rust
// After verify_rtx returns `verified`:
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

## Proof of Concept

1. Write a CKB-VM lock script that executes a tight loop consuming ~70,000,000 cycles. Store it in a cell dep to keep the transaction body small (~100 bytes serialized).
2. Construct a transaction spending a cell locked by this script, with `outputs_capacity = inputs_capacity - 100` (fee = 100 shannons).
3. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1000`.
4. `check_tx_fee` computes `min_fee = 1000 × 100 / 1000 = 100 shannons`. Fee equals min_fee → **admitted**.
5. Actual fee rate = `100 × 1000 / 11,940 ≈ 8 shannons/KW` — 125× below the configured minimum.
6. Repeat to continuously consume the node's verification CPU at negligible fee cost. [6](#0-5) [7](#0-6)

### Citations

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

**File:** tx-pool/src/process.rs (L286-295)
```rust
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
```

**File:** tx-pool/src/process.rs (L715-753)
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
```

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L9-12)
```rust
// default min fee rate, 1000 shannons per kilobyte
const DEFAULT_MIN_FEE_RATE: FeeRate = FeeRate::from_u64(1000);
// default min rbf rate, 1500 shannons per kilobyte
const DEFAULT_MIN_RBF_RATE: FeeRate = FeeRate::from_u64(1500);
```
