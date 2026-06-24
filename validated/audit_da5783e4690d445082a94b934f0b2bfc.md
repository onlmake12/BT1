All three code locations check out. Let me verify the `pre_check` function to confirm `check_tx_fee` is called there and is the sole fee gate.

Audit Report

## Title
`check_tx_fee` Enforces Minimum Fee Rate Using Serialized Size Instead of Weight, Allowing Below-Minimum Fee Rate Transactions Into the Pool — (File: `tx-pool/src/util.rs`)

## Summary
`check_tx_fee` gates pool admission using only the transaction's serialized byte size to compute the minimum required fee, while the rest of the system uses `get_transaction_weight(size, cycles) = max(size, cycles × DEFAULT_BYTES_PER_CYCLES)`. After `verify_rtx` returns actual cycles in `_process_tx`, no weight-based fee-rate check is applied before `submit_entry`. An attacker can submit a transaction with high VM cycles and small serialized size that passes the size-only admission check but carries an actual fee rate far below `min_fee_rate`, causing every receiving node to execute the full cycle budget and relay the transaction to all peers.

## Finding Description
`check_tx_fee` at `tx-pool/src/util.rs:45` computes:

```rust
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

The code itself acknowledges the mismatch with an inline comment at lines 42–44: *"Theoretically we cannot use size as weight directly to calculate fee_rate, here min fee rate is used as a cheap check, so we will use size to calculate fee_rate directly."* [1](#0-0) 

The weight-based formula used everywhere else is defined at `util/types/src/core/tx_pool.rs:298–303`:

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(
        tx_size as u64,
        (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64,
    )
}
``` [2](#0-1) 

`TxEntry::fee_rate` at `tx-pool/src/component/entry.rs:115–117` uses this weight formula for scoring and eviction, confirming the divergence from the admission check: [3](#0-2) 

In `_process_tx` (`tx-pool/src/process.rs:705–777`), the flow is:

1. `pre_check` at L715 calls `check_tx_fee` with size only (L289, L294)
2. `verify_rtx` at L724–734 executes scripts and returns actual `verified.cycles`
3. `TxEntry::new(rtx, verified.cycles, fee, tx_size)` is constructed at L751
4. `submit_entry` is called at L753 [4](#0-3) 

There is no post-execution fee-rate check using `get_transaction_weight(tx_size, verified.cycles)` between steps 2 and 4. `pre_check` is the sole fee gate, and it is called with size only at `process.rs:289` and `process.rs:294`. [5](#0-4) 

## Impact Explanation
**Concrete bypass with defaults (`min_fee_rate = 1000 shannons/KW`, `DEFAULT_BYTES_PER_CYCLES = 0.00017057`):**

| Parameter | Value |
|---|---|
| Serialized size | 200 bytes |
| Cycles | 70,000,000 |
| Weight | max(200, 11,940) = **11,940** |
| Admission min_fee (size-only) | 200 shannons |
| Fee paid | 200 shannons (passes) |
| Actual fee rate | ~16 shannons/KW (~60× below floor) |

Each such transaction causes every receiving node to execute up to `max_block_cycles` worth of script verification, then relay the transaction to all its peers, who repeat the same process. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation
The exploit requires only an unprivileged `send_transaction` RPC call or a P2P relay message. The attacker must control a RISC-V lock script that executes a tight loop — straightforward with any compute-heavy script stored in a cell (keeping the transaction itself small). No privileged access, key material, or majority hashpower is required. The attack is repeatable with many transactions, each costing only the size-based minimum fee.

## Recommendation
Add a post-execution fee-rate gate in `_process_tx` (`tx-pool/src/process.rs`) after `verify_rtx` returns actual cycles, before constructing `TxEntry` and calling `submit_entry`:

```rust
let weight = get_transaction_weight(tx_size, verified.cycles);
let actual_fee_rate = FeeRate::calculate(fee, weight);
if actual_fee_rate < tx_pool_config.min_fee_rate {
    return Some((Err(Reject::LowFeeRate(...)), snapshot));
}
```

The existing size-only check in `check_tx_fee` can remain as a cheap pre-filter but must not be the sole gate.

## Proof of Concept
1. Deploy a RISC-V lock script that executes a tight loop consuming ~70,000,000 cycles but compiles to a small binary stored in a cell (not inline in the transaction).
2. Construct a transaction spending a cell locked by that script, with `fee = 200 shannons` (matching the size-only minimum for a ~200-byte transaction).
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; fee equals `min_fee`; transaction passes the only fee gate.
5. After script execution, `cycles ≈ 70,000,000`, `weight = 11,940`, `actual_fee_rate ≈ 16 shannons/KW` — ~60× below `min_fee_rate`.
6. The transaction enters the pool and is relayed to all peers, each of which admits it via the same flawed check after executing 70M cycles, propagating the CPU cost across the network.

### Citations

**File:** tx-pool/src/util.rs (L42-53)
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
    Ok(fee)
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

**File:** tx-pool/src/component/entry.rs (L114-118)
```rust
    /// Returns fee rate
    pub fn fee_rate(&self) -> FeeRate {
        let weight = get_transaction_weight(self.size, self.cycles);
        FeeRate::calculate(self.fee, weight)
    }
```

**File:** tx-pool/src/process.rs (L288-295)
```rust
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
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
