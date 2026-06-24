Audit Report

## Title
`max_tx_verify_cycles` Not Enforced as Rejection Limit in Relay Path — (File: `sync/src/relayer/transactions_process.rs`, `tx-pool/src/process.rs`)

## Summary

`TxPoolConfig::max_tx_verify_cycles` (default ~70M cycles) is used only as a scheduling classifier in `VerifyQueue::add_tx`, not as a rejection gate. The relay path in `TransactionsProcess::execute()` checks only `max_block_cycles` (~3.5B) before accepting a transaction, and `_process_tx` sets the VM cycle ceiling directly to the peer-declared value. Any connected peer can force the node to run CKB-VM for up to ~3.5B cycles per transaction — approximately 50× the operator-configured cap — by relaying transactions with `declared_cycles` just below `max_block_cycles`.

## Finding Description

**`large_cycle_threshold` is a scheduler label, not a rejection gate:**
In `tx-pool/src/component/verify_queue.rs` lines 212–214, `large_cycle_threshold` (initialized from `max_tx_verify_cycles`) is used only to set `is_large_cycle` for worker scheduling priority. No rejection occurs when `declared_cycles > large_cycle_threshold`.

**Relay path enforces only `max_block_cycles`:**
In `sync/src/relayer/transactions_process.rs` lines 63–74, the sole guard is `declared_cycles > max_block_cycles`. There is no check against `max_tx_verify_cycles`. Transactions with `declared_cycles = max_block_cycles - 1` pass unconditionally.

**`_process_tx` uses the peer-declared value as the VM cycle ceiling:**
In `tx-pool/src/process.rs` line 720:
```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```
For relay transactions, `declared_cycles` is always `Some(peer_value)`, so `max_cycles` is set to the attacker-controlled value. This is passed directly to `verify_rtx` (line 729) as the VM limit. The config's `max_tx_verify_cycles` is never consulted on this path.

**`DeclaredWrongCycles` forces genuine cycle consumption:**
In `tx-pool/src/process.rs` lines 736–748, if `declared != verified.cycles` the transaction is rejected. This means the attacker must craft a script that genuinely consumes the declared cycles — achievable with a tight RISC-V loop.

**Exploit flow:**
1. Attacker connects to the target node via the P2P relay protocol.
2. Sends `RelayTransactionHashes` to announce a transaction hash; node adds it to `unknown_tx_hashes` and requests it.
3. Attacker responds with `RelayTransactions` containing `declared_cycles = max_block_cycles - 1` (~3.499B) and a script that genuinely consumes that many cycles.
4. `TransactionsProcess::execute()` passes the `max_block_cycles` check (line 66).
5. `_process_tx` sets `max_cycles = 3_499_999_999` and runs CKB-VM for the full duration.
6. Node spends ~50× more CPU per transaction than `max_tx_verify_cycles` (default ~70M) intended.
7. Repeat with distinct transaction hashes to sustain continuous CPU saturation of verification workers.

## Impact Explanation

A remote, unprivileged peer can sustain continuous high-CPU load on any CKB node by relaying high-cycle transactions. The per-transaction cycle cap — the primary DoS mitigation for the tx-pool — is rendered ineffective for the relay path. Verification workers are saturated, degrading the node's ability to process legitimate transactions and potentially causing it to fall behind in block and transaction processing. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. The attacker requires no key material, no hashpower, and no special privileges — only a standard P2P connection.

## Likelihood Explanation

Any peer connected to the CKB P2P network can execute this attack using the standard relay announcement/request flow (`RelayTransactionHashes` → `GetRelayTransactions` → `RelayTransactions`), which requires no special access. Crafting a RISC-V loop that consumes a precise number of cycles is straightforward. Multiple attackers from different peers multiply the effect linearly. The 256 MB verify queue cap provides partial mitigation but does not prevent the bypass of `max_tx_verify_cycles`.

## Recommendation

Enforce `max_tx_verify_cycles` as a hard rejection limit in `TransactionsProcess::execute()`, immediately after the existing `max_block_cycles` check:

```rust
// existing check
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_block_cycles) {
    self.nc.ban_peer(self.peer, DEFAULT_BAN_TIME,
        String::from("relay declared cycles greater than max_block_cycles"));
    return Status::ok();
}

// add: enforce per-tx operator limit (no ban — this is local config, not consensus)
let max_tx_verify_cycles = self.relayer.shared().tx_pool_config().max_tx_verify_cycles;
if txs.iter().any(|(_, declared_cycles)| declared_cycles > &max_tx_verify_cycles) {
    return Status::ok();
}
```

This mirrors the existing `max_block_cycles` guard and ensures the operator-configured cap is respected before any VM execution occurs.

## Proof of Concept

1. Configure a CKB node with `max_tx_verify_cycles = 70_000_000` (the default, `TWO_IN_TWO_OUT_CYCLES * 20`).
2. Craft a CKB transaction whose lock script contains a RISC-V loop consuming exactly `N = max_block_cycles - 1` cycles (~3.499B).
3. Connect to the target node as a peer via the P2P relay protocol.
4. Send `RelayTransactionHashes` to announce the transaction hash; wait for the node to send `GetRelayTransactions`.
5. Respond with `RelayTransactions` containing `declared_cycles = N`.
6. Observe: `TransactionsProcess::execute()` passes (`N < max_block_cycles`); `_process_tx` sets `max_cycles = N` and runs CKB-VM for `N` cycles.
7. Measure CPU time per transaction; confirm it is ~50× the time expected under `max_tx_verify_cycles = 70_000_000`.
8. Repeat with distinct transaction hashes to sustain saturation of verification workers.