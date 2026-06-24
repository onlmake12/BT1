Looking at the actual code to validate each claim:

Audit Report

## Title
`check_tx_fee` Admission Gate Uses `tx_size`-Only Fee Check While Pool Prioritization Uses `get_transaction_weight(size, cycles)` — (File: `tx-pool/src/util.rs`)

## Summary
The `check_tx_fee` function enforces `min_fee_rate` using only the serialized byte size of a transaction, before script execution and before cycles are known. After verification completes and actual cycles are available, a `TxEntry` is created and inserted into the pool with no second fee-rate check against the true weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). An attacker can craft a transaction with a small serialized size but a high-cycle script, paying a fee proportional only to byte size while causing the node to expend significant CPU and occupy pool space at an effective fee rate far below `min_fee_rate`.

## Finding Description
In `tx-pool/src/util.rs` lines 42–52, `check_tx_fee` explicitly computes the minimum fee using only `tx_size`, and the code comment acknowledges this is intentional as a "cheap check":

```rust
// Theoretically we cannot use size as weight directly to calculate fee_rate,
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
if fee < min_fee {
    return Err(Reject::LowFeeRate(...));
}
```

This is called in `pre_check` (`tx-pool/src/process.rs:289`) before `verify_rtx` runs. In `_process_tx` (`process.rs:705–777`), after `verify_rtx` returns `verified` with actual cycles, the code immediately constructs `TxEntry::new(rtx, verified.cycles, fee, tx_size)` and calls `submit_entry` — with no intervening fee-rate check using the true weight.

Meanwhile, `TxEntry::fee_rate()` (`tx-pool/src/component/entry.rs:115–118`) and the pool's `EvictKey` (`entry.rs:234–247`) both use `get_transaction_weight(size, cycles)` (`util/types/src/core/tx_pool.rs:298–303`):

```rust
pub fn get_transaction_weight(tx_size: usize, cycles: u64) -> u64 {
    std::cmp::max(tx_size as u64, (cycles as f64 * DEFAULT_BYTES_PER_CYCLES) as u64)
}
```

The gap between the admission check (size-only) and the actual metric (weight) is the root cause. The `limit_size` eviction in `pool.rs:292–329` does use weight-based `EvictKey`, so such transactions are evicted first when the pool is full — but this does not prevent the CPU cost of verification from being incurred, nor does it prevent pool occupancy until eviction occurs.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker submits a transaction with ~200 bytes serialized size and ~70M cycles (within `max_tx_verify_cycles`). At `min_fee_rate = 1000 shannons/KW`:
- Admission check: `min_fee = 1000 × 200 / 1000 = 200 shannons`. Fee of 201 shannons passes.
- Actual weight: `max(200, 70_000_000 × 0.000170571) ≈ 11,940`. Effective fee rate ≈ 16.8 shannons/KW — ~60× below `min_fee_rate`.

The primary impact is CPU DoS with P2P amplification: the transaction is relayed to peer nodes, each of which independently runs the full script verification at 70M cycles. The attacker pays ~200 shannons once but triggers expensive verification across the entire network. Repeated submissions (each with a fresh UTXO) sustain the attack. The verify queue's 256MB size limit (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) bounds throughput but does not prevent the attack at sustained low cost.

## Likelihood Explanation
The `send_transaction` RPC endpoint requires no privileged access. Constructing a CKB-VM script that runs a tight loop consuming ~70M cycles is straightforward. The attacker only needs to control UTXOs sufficient to fund the tiny fees. The attack is repeatable as long as the attacker has spendable outputs, making it a low-cost, sustained DoS vector reachable by any RPC caller or P2P peer.

## Recommendation
After `verify_rtx` returns actual cycles in `_process_tx`, perform a second fee-rate check using the true weight before constructing `TxEntry` and calling `submit_entry`:

```rust
let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

// Second check: enforce min_fee_rate against actual weight
let weight = get_transaction_weight(tx_size, verified.cycles);
let min_fee_by_weight = tx_pool_config.min_fee_rate.fee(weight);
if fee < min_fee_by_weight {
    return Some((Err(Reject::LowFeeRate(
        tx_pool_config.min_fee_rate,
        min_fee_by_weight.as_u64(),
        fee.as_u64(),
    )), snapshot));
}

let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

This mirrors the pattern already used in `TxEntry::fee_rate()` and closes the gap between the admission gate and the actual fee-rate metric used for pool prioritization and eviction.

## Proof of Concept
1. Deploy a CKB lock script that executes a tight loop consuming ~70M cycles. Keep the script cell reference small so the transaction's serialized size is ~200 bytes.
2. Construct a transaction spending a UTXO locked by that script. Set fee = 201 shannons.
3. Submit via `send_transaction` RPC.
4. `check_tx_fee` computes `min_fee = 1000 × 200 / 1000 = 200 shannons`; fee 201 > 200 → admitted to verify queue.
5. `verify_rtx` runs the script, consuming ~70M cycles of CPU.
6. `TxEntry` is created with `cycles = 70_000_000`, `size = 200`, `fee = 201`. Effective fee rate = `201 / 11940 × 1000 ≈ 16.8 shannons/KW`.
7. The transaction enters the pool. Repeat with fresh UTXOs to sustain CPU load across the network via P2P relay.
8. To confirm: instrument `TxEntry::fee_rate()` on the admitted entry and observe the returned value is ~60× below `min_fee_rate`.