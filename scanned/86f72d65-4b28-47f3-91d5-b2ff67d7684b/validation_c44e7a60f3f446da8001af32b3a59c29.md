Audit Report

## Title
Fee Rate Minimum Check Uses Serialized Size Instead of Transaction Weight, Allowing Cycle-Heavy Transactions to Bypass the Minimum Fee Rate — (File: `tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size as the weight argument to `FeeRate::fee`. CKB's canonical transaction weight is `max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`, so for cycle-heavy transactions the true weight can be up to ~12× larger than the byte size. No second fee-rate check using the actual cycle count (returned by `verify_rtx`) is performed anywhere in the admission path, meaning cycle-heavy transactions can be admitted to the mempool at a fraction of the intended minimum cost.

## Finding Description

`FeeRate` is defined as shannons per kilo-weight. [1](#0-0) 

The canonical weight formula is implemented in `get_transaction_weight`: [2](#0-1) 

`check_tx_fee` passes raw `tx_size` as the weight, with a developer comment explicitly acknowledging the mismatch: [3](#0-2) 

`check_tx_fee` is called in `pre_check`, which runs before script execution: [4](#0-3) 

After `verify_rtx` returns the actual `verified.cycles`, the code only checks for a `DeclaredWrongCycles` mismatch — there is no second fee-rate check using the true weight: [5](#0-4) 

By contrast, the RPC fee-rate statistics path correctly uses `get_transaction_weight(size, cycles)`: [6](#0-5) 

`TxEntry::fee_rate()` also uses the correct weight for sorting/eviction, but this does not gate admission: [7](#0-6) 

The split is clear: the admission gate uses size; all post-admission accounting uses weight.

## Impact Explanation

An attacker can submit cycle-heavy transactions whose fee satisfies the size-based check but is far below `min_fee_rate` when measured against the true weight. With default `min_fee_rate = 1000` shannons/KW, a 1 000-byte transaction consuming 70 000 000 cycles has a true weight of ~11 940 and requires only 1 000 shannons to pass `check_tx_fee`, while the correct minimum would be 11 940 shannons — an ~11.9× shortfall. Each such transaction also forces the node to execute 70M cycles of script verification. Repeated submission fills the mempool with underpriced, CPU-intensive transactions, displacing legitimately priced transactions and degrading node performance. This matches the allowed High impact: **Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation

No privileged access is required. Any CKB transaction with a non-trivial lock or type script can consume many cycles while remaining small in serialized size. The attacker fully controls both the script (cycle count) and the capacity delta (fee). The `max_tx_verify_cycles` limit of 70 000 000 bounds the amplification factor at ~12×, but this is still sufficient for a sustained, low-cost flood. The attack is fully parameterizable and repeatable.

## Recommendation

After `verify_rtx` returns the actual cycle count, perform a second fee-rate check using the true weight:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}
```

This check should be inserted in `_process_tx` in `tx-pool/src/process.rs` immediately after the `verify_rtx` result is unwrapped and before `TxEntry::new` is constructed.

## Proof of Concept

1. Craft a CKB transaction whose lock script runs a tight loop consuming ~70 000 000 cycles. Keep the serialized transaction body small (~1 000 bytes).
2. Set the fee to exactly `min_fee_rate × tx_size / 1000 = 1 000 × 1 000 / 1 000 = 1 000` shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = FeeRate(1000).fee(1000) = 1 000 shannons`; the fee equals the threshold, so the transaction passes the admission gate.
5. `verify_rtx` executes the script, consuming 70 000 000 cycles. No subsequent fee-rate check is performed.
6. The transaction is admitted with an effective fee rate of `1 000 × 1 000 / 11 940 ≈ 84` shannons/KW — ~11.9× below the configured minimum.
7. Repeat to fill the mempool with cycle-heavy, underpriced transactions, each forcing full script execution on the node.

### Citations

**File:** util/types/src/core/fee_rate.rs (L3-7)
```rust
/// shannons per kilo-weight
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FeeRate(pub u64);

const KW: u64 = 1000;
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

**File:** tx-pool/src/util.rs (L42-52)
```rust
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
```

**File:** tx-pool/src/process.rs (L715-734)
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
```

**File:** tx-pool/src/process.rs (L736-751)
```rust
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

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```

**File:** tx-pool/src/component/entry.rs (L115-118)
```rust
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```
