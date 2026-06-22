### Title
Remote Peer Bypasses `max_tx_verify_cycles` via `declared_cycles` in `_process_tx` — (`tx-pool/src/process.rs`)

---

### Summary

An unprivileged remote peer can force a CKB node to execute up to `consensus.max_block_cycles()` cycles of script verification per transaction by setting `declared_cycles` to a value exceeding the operator-configured `max_tx_verify_cycles`. The `_process_tx` function uses `declared_cycles` directly as the `max_cycles` argument to `verify_rtx`, completely bypassing the `TxPoolConfig.max_tx_verify_cycles` limit for remote transactions.

---

### Finding Description

The vulnerability lives in `_process_tx`: [1](#0-0) 

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
// ...
let verified_ret = verify_rtx(..., max_cycles, command_rx).await;
```

When `declared_cycles` is `Some(N)` (always the case for remote transactions), `max_cycles` is set to `N` — the attacker-supplied value — not to `self.tx_pool_config.max_tx_verify_cycles`.

The `max_tx_verify_cycles` field in `TxPoolConfig` is documented as "tx pool rejects txs that cycles greater than max_tx_verify_cycles": [2](#0-1) 

But in `_process_tx`, it is never consulted. The `VerifyQueue` uses `max_tx_verify_cycles` only as a classification threshold (`large_cycle_threshold`) to sort txs into "large" vs "small" buckets — it does **not** reject or cap verification: [3](#0-2) 

The full call chain from P2P message to unbounded verification:

1. **P2P entry**: `TransactionsProcess::execute()` receives `RelayTransactions`, checks only `declared_cycles > max_block_cycles` (bans peer if so), then calls `submit_remote_tx(tx, declared_cycles, peer)`: [4](#0-3) 

2. **`submit_remote_tx`** → `resumeble_process_tx` → `enqueue_verify_queue(tx, false, Some((declared_cycles, peer)))`: [5](#0-4) 

3. **`VerifyMgr::Worker::process_inner`** pops the entry and calls `_process_tx(entry.tx, entry.remote.map(|e| e.0), ...)`, passing `declared_cycles` as-is: [6](#0-5) 

4. **`_process_tx`** sets `max_cycles = declared_cycles` and runs full verification: [7](#0-6) 

The `DeclaredWrongCycles` check at line 736–748 only fires if `declared != verified.cycles`. If the attacker crafts a tx whose actual cycle cost equals `declared_cycles`, the check passes and the tx is accepted into the pool after consuming the full declared cycles. [8](#0-7) 

By contrast, `readd_detached_tx` (the reorg path) correctly uses `max_tx_verify_cycles`: [9](#0-8) 

---

### Impact Explanation

An attacker who is a connected peer can force the node to run up to `consensus.max_block_cycles()` (e.g., ~3.5 billion cycles on mainnet) of CKB-VM script execution per transaction, regardless of the operator's `max_tx_verify_cycles` setting (e.g., 70,000,000). By sending many such transactions in sequence, the attacker can saturate the node's verify workers, causing severe CPU exhaustion, degraded block propagation, and potential economic damage (missed mining rewards, delayed transaction relay).

---

### Likelihood Explanation

The attack requires only a standard P2P peer connection. The `unknown_tx_hashes` filter in `TransactionsProcess::execute()` requires the node to have previously requested the tx hash, but this is easily satisfied: the attacker announces a tx hash, the node requests it, and the attacker responds with the high-cycle tx. This is a normal relay flow and requires no special privileges.

The existing integration test `SendLargeCyclesTxToRelay` (with `max_tx_verify_cycles = 5000`) explicitly validates that a node accepts a large-cycles tx relayed from a peer, confirming the bypass is reachable in production: [10](#0-9) 

---

### Recommendation

In `_process_tx`, cap `max_cycles` at `min(declared_cycles, self.tx_pool_config.max_tx_verify_cycles)` for remote transactions. If `declared_cycles > max_tx_verify_cycles`, the tx should be rejected immediately (or the verification should be bounded by `max_tx_verify_cycles`, causing a `DeclaredWrongCycles` rejection if the tx actually needs more cycles). This ensures the operator-configured limit is always the hard ceiling on verification work.

---

### Proof of Concept

**Preconditions**: Node configured with `max_tx_verify_cycles = 70_000_000`; `consensus.max_block_cycles() = 3_500_000_000`.

**Steps**:
1. Connect as a peer to the target node.
2. Announce a tx hash via `RelayTransactionHashes`.
3. Wait for the node to send `GetRelayTransactions` for that hash.
4. Craft a transaction whose scripts consume exactly `N` cycles where `N = 500_000_000` (> `max_tx_verify_cycles`, ≤ `max_block_cycles`).
5. Send `RelayTransactions` with `declared_cycles = N`.
6. The node passes the `declared_cycles > max_block_cycles` check (line 66), enqueues the tx, and `_process_tx` sets `max_cycles = 500_000_000`.
7. `verify_rtx` runs 500M cycles. Since `declared == verified.cycles`, `DeclaredWrongCycles` is not triggered.
8. Tx is accepted into the pool after consuming 500M cycles — 7× the operator's configured limit.
9. Repeat with many transactions to exhaust verify workers.

**Assert**: Verification consumed 500M cycles, not the configured 70M limit. The invariant `max_tx_verify_cycles` bounds all remote verification work is broken.

### Citations

**File:** tx-pool/src/process.rs (L371-379)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
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
