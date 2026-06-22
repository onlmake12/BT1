## Code Trace Analysis

Let me trace the exact execution path and check each guard.

**Guard 1 — `TransactionsProcess` relay check:** [1](#0-0) 

The check is `declared_cycles > max_block_cycles`. A value of exactly `max_block_cycles` passes through.

**Guard 2 — `_process_tx` sets `max_cycles` from `declared_cycles` directly:** [2](#0-1) 

`max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())`. With `declared_cycles = Some(max_block_cycles)`, `max_cycles = max_block_cycles`. There is **no cap from `max_tx_verify_cycles`** applied here.

**Guard 3 — `verify_rtx` runs full script execution up to `max_cycles`:** [3](#0-2) 

When `command_rx` is `Some` (the worker path), it calls `verify_with_pause(max_block_cycles, command_rx)` — full VM execution up to `max_block_cycles` cycles.

**Guard 4 — `DeclaredWrongCycles` is classified as `is_malformed_tx() == true`:** [4](#0-3) 

**Guard 5 — `after_process` bans the peer only AFTER full execution completes:** [5](#0-4) 

The ban fires after `verify_rtx` has already consumed up to `max_block_cycles` cycles.

**`max_tx_verify_cycles` does NOT cap execution for remote txs:** [6](#0-5) 

This config is used only as a routing threshold (`large_cycle_threshold`) in the verify queue to decide which worker handles the tx, not to cap `max_cycles` in `_process_tx`. [7](#0-6) 

---

### Title
Remote peer can force full `max_block_cycles` script execution per verify worker via declared-cycles manipulation — (`tx-pool/src/process.rs`, `tx-pool/src/util.rs`)

### Summary
An unprivileged remote peer can submit a transaction with `declared_cycles = max_block_cycles` (the maximum value that passes the relay guard), causing each verify worker to run CKB-VM script execution up to `max_block_cycles` before rejecting with `DeclaredWrongCycles`. With N workers, N such transactions saturate the entire verification pipeline. Since rejected transactions do not consume UTXOs, the attacker can reuse the same UTXO (with different witnesses/outputs to produce different tx hashes) indefinitely at near-zero cost.

### Finding Description

The relay guard in `TransactionsProcess::execute` rejects only `declared_cycles > max_block_cycles`, so `declared_cycles == max_block_cycles` is admitted. [1](#0-0) 

Inside `_process_tx`, `max_cycles` is set directly from `declared_cycles` with no secondary cap: [8](#0-7) 

`verify_rtx` then runs `ContextualTransactionVerifier::verify_with_pause(max_block_cycles, command_rx)`, which executes the CKB-VM scheduler up to `max_block_cycles` cycles: [3](#0-2) 

Only after the VM finishes does `_process_tx` compare declared vs actual cycles and return `DeclaredWrongCycles`: [9](#0-8) 

`after_process` then bans the peer — but the CPU cost has already been paid: [5](#0-4) 

### Impact Explanation

Each malicious transaction monopolizes one verify worker for the maximum possible verification duration (running `max_block_cycles` = 3.5 billion cycles on mainnet). With the default worker count of `3/4 * cpu_cores`, a small number of transactions (equal to the worker count) saturates the entire verification pipeline, halting admission of legitimate transactions. Since rejected transactions do not result in confirmed on-chain state, the attacker's UTXO is never consumed; new transactions with different content (different witnesses, different outputs) spending the same UTXO produce different tx hashes and bypass the `tx_filter` deduplication check.

### Likelihood Explanation

The relay protocol requires the node to have previously requested the tx hash (via `unknown_tx_hashes`), but this is trivially satisfied: the attacker announces the tx hash via `RelayTransactionHashes`, the node requests it, and the attacker delivers the full tx with `declared_cycles = max_block_cycles`. The attacker needs one valid UTXO with a high-cycle lock script (a one-time on-chain cost), then can generate an unbounded stream of distinct rejected transactions at no further cost (no fees are paid on rejection). Multiple peer connections bypass the per-peer ban.

### Recommendation

Cap `max_cycles` in `_process_tx` at `min(declared_cycles, self.tx_pool_config.max_tx_verify_cycles)` for remote transactions, so that no single remote transaction can consume more than the configured per-tx cycle budget regardless of what the peer declares. The `max_tx_verify_cycles` config already exists for this purpose but is currently only used for worker routing, not for execution limiting. [10](#0-9) 

### Proof of Concept

1. Deploy a lock script on-chain that loops for exactly `max_block_cycles - 1` cycles.
2. Create a UTXO locked by this script.
3. Connect to the target node as a peer.
4. Announce the tx hash via `RelayTransactionHashes`; wait for the node to send `GetRelayTransactions`.
5. Send `RelayTransactions` with `declared_cycles = max_block_cycles`.
6. Observe the verify worker blocked for the full `max_block_cycles` duration before emitting `DeclaredWrongCycles`.
7. Repeat with a new tx (different witness/output, same UTXO) from a new peer connection.
8. With N simultaneous such transactions (N = worker count), measure that legitimate transaction throughput drops to zero.

### Citations

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

**File:** tx-pool/src/process.rs (L513-516)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
```

**File:** tx-pool/src/process.rs (L719-732)
```rust
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
```

**File:** tx-pool/src/process.rs (L736-748)
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
```

**File:** tx-pool/src/util.rs (L101-115)
```rust
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
```

**File:** util/types/src/core/tx_pool.rs (L89-97)
```rust
    pub fn is_malformed_tx(&self) -> bool {
        match self {
            Reject::Malformed(_, _) => true,
            Reject::DeclaredWrongCycles(..) => true,
            Reject::Verification(err) => is_malformed_from_verification(err),
            Reject::Resolve(OutPointError::OverMaxDepExpansionLimit) => true,
            _ => false,
        }
    }
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```
