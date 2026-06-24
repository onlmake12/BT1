Audit Report

## Title
Relay Path Enforces Only `max_block_cycles`, Not `max_tx_verify_cycles`, Allowing Remote Peers to Force Excessive Script Verification CPU Cost — (`sync/src/relayer/transactions_process.rs`)

## Summary
The CKB relay ingestion path validates a remotely declared transaction's cycle count only against the consensus-level `max_block_cycles` (~3.5B), never against the operator-configured `max_tx_verify_cycles` (~70M). Because `_process_tx` sets the script verifier's cycle budget directly to `declared_cycles` for remote transactions, an unprivileged P2P peer can force a receiving node to spend up to `max_block_cycles` of CPU time verifying a single transaction — approximately 50× the intended per-transaction limit — defeating the DoS-reduction purpose of `max_tx_verify_cycles`.

## Finding Description

**Relay check (only `max_block_cycles`):**

In `sync/src/relayer/transactions_process.rs` lines 63–74, the only cycle-related guard is:

```rust
let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) {
    self.nc.ban_peer(...);
    return Status::ok();
}
```

Any `declared_cycles` value in `(0, max_block_cycles]` passes this check unconditionally. There is no comparison against `max_tx_verify_cycles`.

**Script verifier budget set to `declared_cycles`:**

In `tx-pool/src/process.rs` line 720:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

For remote transactions, `declared_cycles` is always `Some(...)` (set by the relay path), so `max_cycles` equals the peer-supplied value. This is passed directly to `verify_rtx` as the cycle ceiling for `ContextualTransactionVerifier`.

**`max_tx_verify_cycles` is only a classification threshold:**

In `tx-pool/src/component/verify_queue.rs` lines 63–65 and 212–214, `large_cycle_threshold` (sourced from `max_tx_verify_cycles`) is used only to set the `is_large_cycle` flag for queue scheduling priority — it is never used to reject or cap a transaction's cycle budget.

**Exploit path:**
1. Attacker deploys an on-chain script that genuinely consumes `N` cycles, where `max_tx_verify_cycles < N ≤ max_block_cycles` (e.g., a tight RISC-V loop consuming 500M cycles).
2. Attacker creates a valid transaction using that script and declares `N` cycles.
3. Attacker connects as a P2P peer and sends a `RelayTransactions` message.
4. The relay check at lines 63–74 passes (`N ≤ max_block_cycles`).
5. `_process_tx` sets `max_cycles = N` and runs script verification with that budget.
6. The script actually consumes `N` cycles; `declared == verified`, so the `DeclaredWrongCycles` rejection at lines 736–748 does not trigger.
7. The node has spent `N` cycles of CPU on one transaction from one peer.
8. Repeat with many transactions or many peers.

**Note on the submitted PoC:** The PoC as written uses `ALWAYS_SUCCESS` (537 actual cycles) with `declared_cycles = max_block_cycles - 1`. This specific example would be immediately rejected by the `declared != verified.cycles` check at line 736, because 537 ≠ 3,499,999,999. The PoC is incorrect as stated. However, the underlying vulnerability is real: an attacker who uses a script that genuinely consumes `N > max_tx_verify_cycles` cycles and declares exactly `N` cycles bypasses the limit entirely.

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** An attacker can force any reachable CKB node to spend up to `max_block_cycles` (~3.5B) of CPU per relayed transaction, versus the intended cap of `max_tx_verify_cycles` (~70M). The ratio of attacker cost (one script deployment + transaction fees) to victim cost (50× CPU per transaction) is highly asymmetric. Sustained relay of such transactions can saturate a node's verification workers, degrading its ability to process legitimate transactions and participate in the network.

## Likelihood Explanation

Any unprivileged P2P peer with a single TCP connection can trigger this. The only prerequisite is deploying a script on-chain that genuinely consumes cycles above `max_tx_verify_cycles` — a one-time cost. The attacker can then reuse the same script across arbitrarily many transactions. No special keys, majority hashpower, or victim mistakes are required.

## Recommendation

In `TransactionsProcess::execute` (`sync/src/relayer/transactions_process.rs`), add a check immediately after the existing `max_block_cycles` guard:

```rust
let max_tx_verify_cycles = self.relayer.shared().tx_pool_config().max_tx_verify_cycles;
if txs.iter().any(|(_, declared_cycles)| *declared_cycles > max_tx_verify_cycles) {
    self.nc.ban_peer(
        self.peer,
        DEFAULT_BAN_TIME,
        String::from("relay declared cycles greater than max_tx_verify_cycles"),
    );
    return Status::ok();
}
```

This ensures the relay ingestion path enforces the same per-transaction cycle cap as the rest of the tx-pool, consistent with the stated purpose of `max_tx_verify_cycles`.

## Proof of Concept

1. Write a CKB script (RISC-V) containing a loop that executes exactly `N` CKB VM cycles, where `70_000_001 ≤ N ≤ max_block_cycles`. Deploy it on-chain.
2. Construct a valid transaction whose only input script is the above, so actual verified cycles = `N`.
3. Connect to a target CKB node as a P2P peer supporting `RelayV3`.
4. Send a `RelayTransactions` message with `declared_cycles = N`.
5. Observe: the relay check at `transactions_process.rs:64–74` passes (`N ≤ max_block_cycles`).
6. `_process_tx` sets `max_cycles = N`; the verifier runs the script for `N` cycles; `declared == verified`, so no `DeclaredWrongCycles` rejection occurs.
7. The node has spent `N > max_tx_verify_cycles` cycles on one peer-supplied transaction.
8. Repeat in a loop (with distinct transactions or after the first is evicted) to sustain CPU load on the target node's verification workers.