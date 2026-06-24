The code has been verified. All cited line numbers and logic match the actual source.

- `process.rs:720`: `max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())` — no cap at `max_tx_verify_cycles`. [1](#0-0) 
- `util.rs:90,108`: `verify_rtx`'s parameter is named `max_tx_verify_cycles` but receives the attacker-controlled value; it is passed directly to `verify_with_pause`. [2](#0-1) 
- `verify_mgr.rs:151`: `entry.remote.map(|e| e.0)` extracts the peer-supplied declared cycles and passes them as `declared_cycles` to `_process_tx`. [3](#0-2) 
- `transactions_process.rs:66`: Only `> max_block_cycles` is rejected; values up to and including `max_block_cycles` pass. [4](#0-3) 
- `process.rs:884`: `readd_detached_tx` correctly uses `self.tx_pool_config.max_tx_verify_cycles`, confirming the config value exists but is never applied in the relay path. [5](#0-4) 

---

Audit Report

## Title
`max_tx_verify_cycles` Not Enforced for Remote Relay Transactions — (`tx-pool/src/process.rs`)

## Summary
`_process_tx` sets `max_cycles` directly from the peer-supplied `declared_cycles` value with no cap at `max_tx_verify_cycles`. The only upstream guard rejects values strictly greater than `max_block_cycles` (3.5 B), so any declared cycle count in the range `(max_tx_verify_cycles, max_block_cycles]` passes unchecked. An unprivileged relay peer can force the victim node to run the CKB-VM for up to 3.5 B cycles per transaction instead of the configured 70 M-cycle cap — a ~50× CPU amplification per transaction.

## Finding Description
**Entry guard** (`sync/src/relayer/transactions_process.rs:63–74`): rejects only `declared_cycles > max_block_cycles`; values equal to `max_block_cycles` (3 500 000 000) are accepted.

**`_process_tx`** (`tx-pool/src/process.rs:720`):
```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```
`max_cycles` is set to the peer-supplied value with no `.min(self.tx_pool_config.max_tx_verify_cycles)` applied.

**`verify_rtx` call** (`tx-pool/src/process.rs:724–732`): `max_cycles` (attacker-controlled) is passed as the `max_tx_verify_cycles` argument to `verify_rtx`.

**`verify_rtx`** (`tx-pool/src/util.rs:108`): passes the value directly to `verify_with_pause(max_tx_verify_cycles, command_rx)`, which is the CKB-VM cycle budget.

**`verify_mgr.rs:151`**: `entry.remote.map(|e| e.0)` extracts the peer-declared cycles and forwards them unchanged to `_process_tx`.

**Post-verification check** (`tx-pool/src/process.rs:736–749`): only rejects if `declared != verified.cycles`; if the script genuinely consumes exactly N cycles and N was declared, the transaction is accepted into the pool with no check against `max_tx_verify_cycles`.

**Contrast**: `readd_detached_tx` (`tx-pool/src/process.rs:884`) correctly uses `self.tx_pool_config.max_tx_verify_cycles` as the hard cap, confirming the config value exists and is intentionally used elsewhere but is absent from the relay path.

## Impact Explanation
Any unprivileged P2P peer can cause the victim node's tx-pool worker to execute the CKB-VM for up to 3.5 B cycles per transaction instead of the 70 M-cycle cap. Repeated submissions across multiple connections cause sustained CPU saturation of the tx-pool verification thread, degrading block-template assembly and peer-relay throughput. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**. The attacker's cost is only valid UTXOs; the UTXO is not consumed until the transaction is mined, enabling repeated reuse across connections.

## Likelihood Explanation
The path is reachable by any unprivileged P2P peer. The attacker needs: (1) valid UTXOs on CKB, (2) a lock script containing a counted loop that consumes a known exact cycle count N where `max_tx_verify_cycles < N ≤ max_block_cycles`. Both are straightforward to construct. No PoW, no majority hashpower, no leaked keys, and no victim mistakes are required. The attack is repeatable and can be parallelized across multiple peer connections.

## Recommendation
In `_process_tx` (`tx-pool/src/process.rs`), cap `max_cycles` at `max_tx_verify_cycles` when `declared_cycles` is present:

```rust
let max_cycles = declared_cycles
    .map(|d| d.min(self.tx_pool_config.max_tx_verify_cycles))
    .unwrap_or_else(|| self.consensus.max_block_cycles());
```

Alternatively, reject the transaction before verification if `declared_cycles > self.tx_pool_config.max_tx_verify_cycles` and ban the relaying peer, consistent with the existing guard in `transactions_process.rs`.

## Proof of Concept
1. Craft a CKB lock script containing a tight counted loop that consumes exactly N cycles, where `N = max_block_cycles = 3_500_000_000`.
2. Build a valid transaction spending a UTXO locked by that script.
3. Connect to the victim node as a RelayV3 peer.
4. Send a `RelayTransactions` message with `cycles = N` for that transaction.
5. Observe via `perf`/`top` that the victim node's tx-pool worker thread saturates a CPU core for the full 3.5 B-cycle window (wall-clock time proportional to 3.5 B cycles, not 70 M cycles).
6. Confirm the transaction enters the pending pool (since `declared == verified`).
7. Repeat with fresh UTXOs or across multiple peer connections to sustain CPU exhaustion.

The invariant that `max_tx_verify_cycles` bounds per-transaction CKB-VM cost in the relay path is falsified by the code at `tx-pool/src/process.rs:720`.

### Citations

**File:** tx-pool/src/process.rs (L720-720)
```rust
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L884-884)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
```

**File:** tx-pool/src/util.rs (L90-108)
```rust
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
```

**File:** tx-pool/src/verify_mgr.rs (L147-154)
```rust
            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
```

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```
