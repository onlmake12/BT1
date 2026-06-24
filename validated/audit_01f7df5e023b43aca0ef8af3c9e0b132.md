Audit Report

## Title
Remote Peer Bypasses `max_tx_verify_cycles` via `declared_cycles` in `_process_tx` — (`tx-pool/src/process.rs`)

## Summary
The `_process_tx` function sets `max_cycles` directly from the attacker-supplied `declared_cycles` value, never consulting `TxPoolConfig.max_tx_verify_cycles`. Any connected P2P peer can force the node to execute up to `consensus.max_block_cycles()` cycles of CKB-VM script verification per transaction, completely ignoring the operator-configured verification limit. By sending many such transactions, an attacker can saturate verify workers and cause sustained CPU exhaustion and network congestion.

## Finding Description

**Root cause — `_process_tx` ignores `max_tx_verify_cycles`:**

At `tx-pool/src/process.rs:720`, `max_cycles` is set from the peer-supplied value:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [1](#0-0) 

`self.tx_pool_config.max_tx_verify_cycles` is never consulted here. The value flows directly into `verify_rtx`, which runs the CKB-VM for up to `declared_cycles` cycles.

**The only P2P guard is insufficient:**

In `sync/src/relayer/transactions_process.rs`, the only check on `declared_cycles` is whether it exceeds `max_block_cycles` (banning the peer if so): [2](#0-1) 

Any value in the range `(max_tx_verify_cycles, max_block_cycles]` passes unchecked and is forwarded to `submit_remote_tx` → `_process_tx`.

**`max_tx_verify_cycles` is only used in the reorg path:**

`readd_detached_tx` correctly caps verification at `max_tx_verify_cycles`: [3](#0-2) 

But `_process_tx` — the path taken for all remote relay transactions — does not.

**`VerifyQueue` uses `max_tx_verify_cycles` only for classification:**

The `large_cycle_threshold` (set from `max_tx_verify_cycles`) is used only to sort transactions into "large" vs "small" worker buckets, not to reject or cap them: [4](#0-3) 

**`DeclaredWrongCycles` check does not protect against this:**

The mismatch check at lines 736–748 only fires if `declared != verified.cycles`: [5](#0-4) 

An attacker who crafts a transaction whose scripts consume exactly `N` cycles (where `N > max_tx_verify_cycles` but `N ≤ max_block_cycles`) will pass this check, and the transaction will be accepted into the pool after consuming the full `N` cycles.

**Call chain:**

`TransactionsProcess::execute()` → `submit_remote_tx(tx, declared_cycles, peer)` → `resumeble_process_tx` → `enqueue_verify_queue` → `VerifyMgr::Worker::process_inner` → `_process_tx(entry.tx, entry.remote.map(|e| e.0), ...)`: [6](#0-5) 

## Impact Explanation

This matches the **High** impact category: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker with a standard P2P connection can force each verify worker to spend up to `max_block_cycles` (~3.5 billion on mainnet) of CKB-VM execution per transaction, versus the operator-configured `max_tx_verify_cycles` (e.g., 70,000,000 — a 50× amplification). With multiple verify workers and a stream of such transactions, the node's CPU is saturated, block propagation degrades, and mining rewards may be missed. The cost to the attacker is negligible (a single peer connection and crafted transactions).

## Likelihood Explanation

The attack requires only a standard P2P peer connection and the normal relay flow: announce a tx hash via `RelayTransactionHashes`, wait for the node to send `GetRelayTransactions`, then respond with a high-cycle transaction. No special privileges, leaked keys, or victim mistakes are required. The existing integration test `SendLargeCyclesTxToRelay` (with `max_tx_verify_cycles = 5000`) explicitly asserts that a node accepts a large-cycles tx relayed from a peer, confirming the bypass is reachable in production: [7](#0-6) 

The attack is repeatable and can be sustained indefinitely.

## Recommendation

In `_process_tx`, cap `max_cycles` at `min(declared_cycles, self.tx_pool_config.max_tx_verify_cycles)` for remote transactions. If `declared_cycles > max_tx_verify_cycles`, the transaction should be rejected immediately before verification begins (returning `Reject::ExceededMaximumCycles` or similar), ensuring the operator-configured limit is always the hard ceiling on per-transaction verification work. The check in `TransactionsProcess::execute()` should also be extended to ban peers that declare cycles exceeding `max_tx_verify_cycles`, not just `max_block_cycles`.

## Proof of Concept

**Preconditions:** Node configured with `max_tx_verify_cycles = 70_000_000`; `consensus.max_block_cycles() = 3_500_000_000`.

1. Connect as a peer to the target node.
2. Announce a tx hash via `RelayTransactionHashes`.
3. Wait for the node to send `GetRelayTransactions` for that hash.
4. Craft a transaction whose scripts consume exactly `N = 500_000_000` cycles (`N > max_tx_verify_cycles`, `N ≤ max_block_cycles`).
5. Send `RelayTransactions` with `declared_cycles = 500_000_000`.
6. The node passes the `declared_cycles > max_block_cycles` guard (line 66 of `transactions_process.rs`), enqueues the tx, and `_process_tx` sets `max_cycles = 500_000_000`.
7. `verify_rtx` runs 500M cycles. Since `declared == verified.cycles`, `DeclaredWrongCycles` is not triggered.
8. The tx is accepted into the pool after consuming 500M cycles — 7× the operator's configured limit.
9. Repeat with concurrent transactions to exhaust all verify workers.

**Assertion:** Verification consumed 500M cycles, not the configured 70M limit. The invariant that `max_tx_verify_cycles` bounds all remote verification work is broken. This is directly confirmed by the existing `SendLargeCyclesTxToRelay` integration test, which sets `max_tx_verify_cycles = 5000` and asserts the node accepts a large-cycles relayed tx. [8](#0-7)

### Citations

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

**File:** tx-pool/src/process.rs (L736-749)
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
```

**File:** tx-pool/src/process.rs (L884-884)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
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

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
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

**File:** test/src/specs/tx_pool/send_large_cycles_tx.rs (L135-141)
```rust
    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        let lock_arg = self.random_key.lock_arg();
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 5000u64;
        let block_assembler = new_block_assembler_config(lock_arg, ScriptHashType::Type);
        config.block_assembler = Some(block_assembler);
    }
```
