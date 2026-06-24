Audit Report

## Title
Async worker path uses peer-supplied `declared_cycles` as VM cycle limit, bypassing `max_tx_verify_cycles` — (`tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the cycle cap passed to `verify_rtx` is set directly from the peer-supplied `declared_cycles` value with no upper bound enforced by `max_tx_verify_cycles`. A remote peer can declare cycles up to `max_block_cycles`, causing the node to run the VM for that many cycles and allowing the transaction to enter the pool with `cycles > max_tx_verify_cycles`, violating the operator-configured per-transaction cycle limit and enabling CPU exhaustion.

## Finding Description
At `tx-pool/src/process.rs` line 720, `_process_tx` computes:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

This value is passed directly to `verify_rtx` as the VM cycle cap (lines 724–732). No clamping to `self.tx_pool_config.max_tx_verify_cycles` occurs here.

The async worker in `verify_mgr.rs` (lines 147–154) calls `_process_tx` with `entry.remote.map(|e| e.0)` as `declared_cycles`, which is the raw peer-supplied value from the P2P relay message.

The only post-verification guard (lines 736–749) is:

```rust
if let Some(declared) = declared_cycles && declared != verified.cycles { ... }
```

This rejects only when `declared != verified.cycles`. If a peer crafts a script consuming exactly X cycles where `max_tx_verify_cycles < X <= max_block_cycles` and declares `X`, then `declared == verified.cycles` and the transaction is accepted into the pool with `cycles = X > max_tx_verify_cycles`. There is no guard anywhere in `_process_tx` enforcing `verified.cycles <= max_tx_verify_cycles`.

The `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` path (lines 335–353, 371–379) performs no pre-rejection for `declared_cycles > max_tx_verify_cycles`. `non_contextual_verify` checks only transaction size and cellbase status, not cycles.

`process_orphan_tx` (lines 598–614) explicitly routes orphans with `cycle > max_tx_verify_cycles` to the async verify queue, confirming this path is reachable for high-cycle transactions.

`VerifyQueue.add_tx` (lines 212–214) uses `declared_cycles > large_cycle_threshold` only to set the `is_large_cycle` routing flag, not to reject.

The `max_tx_verify_cycles` field is documented as "tx pool rejects txs that cycles greater than max_tx_verify_cycles" (`util/app-config/src/configs/tx_pool.rs` lines 20–21), but this invariant is not enforced in the async worker path.

## Impact Explanation
This is a CPU exhaustion / DoS vector. An attacker can submit many transactions each consuming near-`max_block_cycles` cycles, multiplying per-transaction verification cost far beyond the operator-configured `max_tx_verify_cycles` limit. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." The cost to the attacker is only crafting scripts with a precise cycle count and relaying them; the cost to the victim node scales with `max_block_cycles / max_tx_verify_cycles` per transaction.

## Likelihood Explanation
Any unprivileged remote peer can relay a transaction via the standard P2P relay protocol with an arbitrary `declared_cycles` value up to `max_block_cycles`. No proof-of-work, key material, or privileged access is required. The attack is trivially repeatable and requires only crafting a script that consumes a precise cycle count and relaying it with a matching `declared_cycles` field.

## Recommendation
In `_process_tx`, cap `max_cycles` at `max_tx_verify_cycles` for the async worker path:

```rust
let max_cycles = declared_cycles
    .unwrap_or_else(|| self.consensus.max_block_cycles())
    .min(self.tx_pool_config.max_tx_verify_cycles);
```

Additionally, after verification, explicitly reject if `verified.cycles > self.tx_pool_config.max_tx_verify_cycles` with `Reject::ExceededMaximumCycles`. Optionally, add a pre-enqueue rejection in `resumeble_process_tx` when `declared_cycles > max_tx_verify_cycles` to fail fast before any VM execution.

## Proof of Concept
1. Configure a node with `max_tx_verify_cycles = 70_000_000`.
2. Craft a CKB-VM script (e.g., using a tight loop or `spawn`) that consumes exactly `80_000_000` cycles.
3. Relay the transaction via P2P with `declared_cycles = 80_000_000`.
4. The async worker calls `_process_tx` with `declared_cycles = Some(80_000_000)`, sets `max_cycles = 80_000_000`, runs the VM to 80M cycles, finds `declared == verified.cycles`, and inserts the entry into the pool.
5. Assert: the transaction is present in the pool with `cycles = 80_000_000 > max_tx_verify_cycles = 70_000_000`, violating the configured invariant.
6. Repeat with many such transactions to demonstrate CPU exhaustion proportional to `max_block_cycles / max_tx_verify_cycles`.