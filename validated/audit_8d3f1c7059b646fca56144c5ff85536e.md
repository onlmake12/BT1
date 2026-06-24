Audit Report

## Title
Minimum Fee Check Uses Only Serialized Size, Ignoring Cycles-Based Weight — (`tx-pool/src/util.rs`)

## Summary

`check_tx_fee` in `tx-pool/src/util.rs` computes the minimum required fee using only the transaction's serialized byte size, not the canonical weight `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` returns the actual consumed cycles in `_process_tx`, no second fee check is performed using the true weight. A transaction with small serialized size but near-maximum cycle consumption can enter the pool and be relayed while paying ~60× less than the intended minimum fee rate.

## Finding Description

`check_tx_fee` explicitly uses only `tx_size` for the minimum fee calculation, with a code comment acknowledging this is an approximation: [1](#0-0) 

The canonical weight function `get_transaction_weight` in `util/types/src/core/tx_pool.rs` uses `max(tx_size, cycles * DEFAULT_BYTES_PER_CYCLES)`: [2](#0-1) 

In `_process_tx`, `pre_check` (which calls `check_tx_fee`) runs before cycles are known. After `verify_rtx` returns the actual `verified.cycles`, the code proceeds directly to build the `TxEntry` with no second weight-based fee check: [3](#0-2) 

By contrast, the fee rate statistics path in `rpc/src/util/fee_rate.rs` correctly uses `get_transaction_weight` with both size and cycles: [4](#0-3) 

This confirms the enforcement path is the only place where the weight formula is not applied.

## Impact Explanation

An attacker submits a transaction with ~200 bytes serialized size and ~70,000,000 cycles consumed. At `min_fee_rate = 1,000` shannons/KB:

- Fee paid: `1000 * 200 / 1000 = 200 shannons` (passes `check_tx_fee`)
- True weight: `max(200, 70_000_000 * 0.000_170_571_4) ≈ 11,940`
- True minimum fee: `1000 * 11940 / 1000 = 11,940 shannons`

The attacker pays ~60× below the intended minimum. Such transactions enter the pool, consume full verification resources, and are relayed to peers. Repeated submission floods the network with maximum-cost verification work at near-zero fee cost. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation

The attack requires no special privileges, keys, or configuration. Any unprivileged caller can trigger it via the `send_transaction` or `test_tx_pool_accept` RPC endpoints. The default shipped configuration sets `min_fee_rate = 1_000` and `max_tx_verify_cycles = 70_000_000`. The attack is fully repeatable and maximally effective whenever a script can consume cycles near the limit with minimal witness/output data.

## Recommendation

After `verify_rtx` returns the actual consumed cycles in `_process_tx`, perform a second fee check using the true weight before building the `TxEntry`:

```rust
let actual_weight = get_transaction_weight(tx_size, verified.cycles);
let actual_min_fee = tx_pool_config.min_fee_rate.fee(actual_weight);
if fee < actual_min_fee {
    return Some((Err(Reject::LowFeeRate(tx_pool_config.min_fee_rate, actual_min_fee.as_u64(), fee.as_u64())), snapshot));
}
```

This requires acquiring the tx_pool read lock briefly after `verify_rtx`, or passing `min_fee_rate` into `_process_tx`. The pattern mirrors the weight calculation already used in `rpc/src/util/fee_rate.rs`.

## Proof of Concept

1. Construct a CKB transaction with a lock script that loops near 70,000,000 cycles but has minimal witness/output data (serialized size ~200 bytes).
2. Set `inputs_capacity - outputs_capacity = 200 shannons` (fee = 200 shannons).
3. Submit via `send_transaction` RPC to a node with default `min_fee_rate = 1000`.
4. `check_tx_fee` computes `min_fee = 1000 * 200 / 1000 = 200 shannons`; fee equals threshold → accepted.
5. `verify_rtx` runs and consumes ~70,000,000 cycles; no second fee check occurs.
6. Transaction enters the pool and is relayed to peers. True minimum fee should be `11,940 shannons`; attacker paid ~60× less.
7. Repeat to exhaust peer verification capacity at minimal cost.

### Citations

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

**File:** util/types/src/core/tx_pool.rs (L298-303)
```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
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

**File:** rpc/src/util/fee_rate.rs (L103-106)
```rust
                        let weight = get_transaction_weight(*size as usize, cycles);
                        if weight > 0 {
                            fee_rates.push(FeeRate::calculate(fee, weight).as_u64());
                        }
```
