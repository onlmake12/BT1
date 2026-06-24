Audit Report

## Title
`check_tx_fee` Uses Raw Serialized Size Instead of Weight for `min_fee_rate` Enforcement, Allowing Cycle-Heavy Transactions to Bypass Fee-Rate Admission — (File: `tx-pool/src/util.rs`)

## Summary

In `tx-pool/src/util.rs`, `check_tx_fee` enforces `min_fee_rate` by calling `tx_pool.config.min_fee_rate.fee(tx_size as u64)`, passing only the raw serialized byte size. `FeeRate` is defined as shannons per kilo-weight, where weight is `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. For cycle-heavy transactions, weight can be up to ~60× larger than `tx_size`, so the admission check computes a minimum fee far below what `min_fee_rate` actually requires. The code itself acknowledges this with the comment "Theoretically we cannot use size as weight directly to calculate fee_rate" but proceeds to do exactly that, creating a meaningful cost reduction for spam attacks.

## Finding Description

`FeeRate` is documented and implemented as shannons per kilo-weight: [1](#0-0) 

The correct transaction weight is computed as `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`: [2](#0-1) 

The admission gate in `check_tx_fee` instead passes only `tx_size`, with an explicit comment acknowledging the limitation: [3](#0-2) 

The reason cycles are unavailable at this point is structural: `pre_check` (which calls `check_tx_fee`) runs first, then `verify_rtx` produces the actual cycle count, and only then is `TxEntry::new(rtx, verified.cycles, fee, tx_size)` constructed: [4](#0-3) 

After admission, eviction and sorting correctly use `get_transaction_weight(entry.size, entry.cycles)`: [5](#0-4) 

This creates a split: the admission gate uses size-only fee rate, while post-admission mechanisms use the correct weight-based fee rate. A transaction with small serialized size but high actual cycles passes the admission gate at a fraction of the intended minimum fee, then sits in the pool deprioritized for mining but occupying pool space. The default config even documents this behavior: `min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB`. [6](#0-5) 

## Impact Explanation

An unprivileged submitter can craft a transaction with small serialized size (e.g., 200 bytes) but high actual VM cycles (e.g., 70,000,000 — the default `max_tx_verify_cycles`). With `min_fee_rate = 1,000` shannons/kilo-weight:

- Correct weight = `max(200, 70_000_000 × 0.000_170_571_4) ≈ 11,940`
- Correct minimum fee = `1,000 × 11,940 / 1,000 = 11,940 shannons`
- Actual check computes: `1,000 × 200 / 1,000 = 200 shannons`

The attacker pays ~200 shannons instead of ~11,940 shannons — a ~60× reduction — to pass the admission gate. Repeated submissions fill the pool with sub-threshold-fee-rate transactions, degrading the spam-prevention policy. This matches the allowed impact: **High — bad designs which could cause CKB network congestion with few costs**. [7](#0-6) 

## Likelihood Explanation

The exploit requires only that the attacker control UTXOs locked by a script that consumes high cycles (e.g., a loop-heavy script). This is a normal user capability — no admin keys, no privileged access, no majority hashpower. The `send_transaction` RPC endpoint is the trigger path. The `max_tx_verify_cycles` limit (70M by default) bounds the maximum weight amplification to ~60×, but even at modest cycle counts the discrepancy is significant. The attack is repeatable as long as the attacker holds UTXOs, and UTXOs locked by high-cycle scripts can be prepared in advance in bulk. [8](#0-7) 

## Recommendation

Replace the raw `tx_size` argument in `check_tx_fee` with the actual transaction weight. Since cycles are not available at `pre_check` time, the declared cycles (for relay transactions) or a conservative upper bound (for local submissions) should be used:

```rust
// In check_tx_fee, replace:
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);

// With:
let cycles = rtx.transaction.cycles().unwrap_or(0);
let weight = get_transaction_weight(tx_size, cycles);
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

For local RPC submissions where cycles are unknown before verification, a conservative bound (e.g., `max_tx_verify_cycles`) can be used to ensure the admission check is at least as strict as the eviction check. The `declared_cycles` parameter already flows through `_process_tx` and could be threaded into `pre_check` and then into `check_tx_fee`. [9](#0-8) 

## Proof of Concept

1. Deploy a CKB script that performs a tight computation loop consuming ~70,000,000 cycles.
2. Lock a UTXO with that script.
3. Construct a transaction spending that UTXO with a single output; serialized size ≈ 200 bytes.
4. Set the transaction fee to 200 shannons.
5. Submit via `send_transaction` RPC to a node with `min_fee_rate = 1_000`.
6. Observe: the transaction is admitted (fee check passes: `1000 × 200 / 1000 = 200 ≤ 200`).
7. Verify via `get_raw_tx_pool` that the transaction is in the pool despite having an effective weight-based fee rate of `1000 × 200 / 11940 ≈ 16 shannons/kilo-weight`, far below the configured `min_fee_rate = 1,000`.
8. Repeat with different UTXOs to fill the pool with sub-threshold-fee-rate transactions.

The root cause is at `tx-pool/src/util.rs` line 45, where `tx_pool.config.min_fee_rate.fee(tx_size as u64)` is used instead of `tx_pool.config.min_fee_rate.fee(get_transaction_weight(tx_size, cycles))`. [10](#0-9)

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
```

**File:** util/types/src/core/tx_pool.rs (L279-279)
```rust
pub const DEFAULT_BYTES_PER_CYCLES: f64 = 0.000_170_571_4_f64;
```

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
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

**File:** tx-pool/src/process.rs (L715-751)
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
```

**File:** tx-pool/src/component/entry.rs (L221-231)
```rust
impl From<&TxEntry> for AncestorsScoreSortKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let ancestors_weight = get_transaction_weight(entry.ancestors_size, entry.ancestors_cycles);
        AncestorsScoreSortKey {
            fee: entry.fee,
            weight,
            ancestors_fee: entry.ancestors_fee,
            ancestors_weight,
        }
    }
```

**File:** resource/ckb.toml (L212-212)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
