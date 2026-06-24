Audit Report

## Title
Fee Admission Check Uses Byte Size Only, Ignoring CKB-VM Cycle Cost — (`tx-pool/src/util.rs`)

## Summary
`check_tx_fee` in `tx-pool/src/util.rs` enforces the minimum fee rate using only the transaction's serialized byte size, never the actual computational weight (`max(tx_size, cycles × DEFAULT_BYTES_PER_CYCLES)`). Because cycles are unknown at pre-check time and no second fee gate exists after `verify_rtx` returns the true cycle count, an attacker can submit a minimal-size transaction containing a cycle-exhausting script, pay ~200 shannons, and force every receiving node to run the CKB-VM for up to `max_block_cycles` cycles — a ~3,000× underpayment relative to the correct weight-based fee.

## Finding Description
`check_tx_fee` (`tx-pool/src/util.rs` L28–54) computes the minimum fee exclusively from `tx_size`:

```rust
// here min fee rate is used as a cheap check,
// so we will use size to calculate fee_rate directly
let min_fee = tx_pool.config.min_fee_rate.fee(tx_size as u64);
```

This is called inside `pre_check` (`tx-pool/src/process.rs` L289–290), before script execution. After `pre_check` passes, `_process_tx` (`process.rs` L720) sets the VM cycle cap:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

For a locally submitted transaction (`declared_cycles = None`), `max_cycles` becomes the full consensus `max_block_cycles`. `verify_rtx` then runs the VM up to that cap. After `verify_rtx` returns the actual `verified.cycles`, the code at `process.rs` L751 simply constructs the pool entry:

```rust
let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
```

There is no second fee check using `get_transaction_weight(tx_size, verified.cycles)`. The weight-based function `get_transaction_weight` (`util/types/src/core/tx_pool.rs` L298–303) is only used for post-admission sorting (`AncestorsScoreSortKey`, `entry.rs` L221–231) and eviction (`EvictKey`, `entry.rs` L234–247), never for the admission gate.

For the relay path, `transactions_process.rs` L63–74 only bans peers whose `declared_cycles > max_block_cycles`; declaring exactly `max_block_cycles` is permitted, and the fee check still uses size only.

## Impact Explanation
An attacker who controls valid UTXOs can submit transactions that pass the size-based fee gate (~200 shannons for a ~200-byte tx) while containing a lock/type script that loops until the cycle limit is exhausted. Each such transaction forces every receiving node to run the CKB-VM for up to `max_block_cycles` (~3.5B) cycles during pool admission. With `max_ancestors_count = 25` chained descendants per UTXO, the per-UTXO impact is multiplied 25×. Sustained submission saturates the tx-pool verification workers, delays block template assembly, and degrades node responsiveness across the network.

This matches the allowed bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The entry path is fully unprivileged: any `send_transaction` RPC caller or P2P relay peer qualifies. The attacker needs valid UTXOs and a deployed looping script cell, but the fee cost per transaction is negligible (~200 shannons ≈ 0.000002 CKB). No special knowledge or privileged access is required. The attack is repeatable as long as the attacker controls UTXOs, and the relay path is also exploitable by declaring `declared_cycles = max_block_cycles` with a script that actually consumes that many cycles.

## Recommendation
Replace the size-only fee check in `check_tx_fee` with a weight-based check. For the pre-check stage (before cycles are known), use the declared cycles from the relay message as an upper bound, or use `max_tx_verify_cycles` as a conservative proxy:

```rust
let weight = get_transaction_weight(tx_size, declared_cycles.unwrap_or(max_tx_verify_cycles));
let min_fee = tx_pool.config.min_fee_rate.fee(weight);
```

Alternatively, add a second fee check after `verify_rtx` returns the actual cycle count, using `get_transaction_weight(tx_size, verified.cycles)` to compute the correct minimum fee before constructing the `TxEntry`.

## Proof of Concept
1. Deploy a CKB script cell whose bytecode loops consuming cycles until the VM halts at the cycle limit.
2. Create a transaction spending any UTXO with that script as the lock, sized to ~200 bytes.
3. Submit via `send_transaction` RPC. The fee check passes (200 shannons ≥ size-based minimum of 200 shannons at 1,000 shannons/KB).
4. The node's `verify_rtx` runs the script for up to `max_block_cycles` cycles before rejecting (cycle limit exceeded) or accepting.
5. Repeat with 25 chained descendants (up to `max_ancestors_count`) per UTXO to multiply impact.
6. Observe that verification workers are saturated and block template assembly is delayed.
7. For the relay path: relay the same transaction with `declared_cycles = max_block_cycles`; the relay handler permits it (only `declared_cycles > max_block_cycles` triggers a ban), and the fee check still uses size only.