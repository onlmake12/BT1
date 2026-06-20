### Title
Unclamped `max_tx_verify_cycles` in `TxPoolConfig` Causes Silent Transaction Loss After Chain Reorganization — (`tx-pool/src/process.rs`, `util/app-config/src/configs/tx_pool.rs`)

---

### Summary

`max_tx_verify_cycles` is a node-operator-configurable parameter in `TxPoolConfig` with no minimum or maximum validation. When set to `0` (or any value below the actual cycle cost of a transaction's scripts), the `readd_detached_tx` function uses it directly as the cycle limit for script verification. This causes every transaction being re-added to the pool after a chain reorganization to fail with `ExceededMaximumCycles` and be silently dropped — permanently. The analog to the Morpho `maxGas = 0` case is exact: a configurable resource-accounting parameter with no floor causes a critical processing loop to always fail.

---

### Finding Description

`TxPoolConfig.max_tx_verify_cycles` is read from `ckb.toml` and stored without any minimum/maximum bounds check. [1](#0-0) 

In `readd_detached_tx`, this value is used **directly** as the cycle limit passed to `verify_rtx`: [2](#0-1) 

`verify_rtx` then passes it as `max_tx_verify_cycles` to `ContextualTransactionVerifier::verify`: [3](#0-2) 

`ContextualTransactionVerifier::verify` passes it to `TransactionScriptsVerifier::verify`: [4](#0-3) 

Which calls `scheduler.run(RunMode::LimitCycles(max_cycles))`: [5](#0-4) 

If `max_cycles = 0`, the CKB-VM immediately returns `CyclesExceeded` because every instruction costs at least 1 cycle. The error propagates back through `verify_rtx` → `readd_detached_tx`, which silently discards the transaction without re-adding it to the pool: [6](#0-5) 

There is no floor validation anywhere in the config loading path: [7](#0-6) 

The default is `TWO_IN_TWO_OUT_CYCLES * 20`, but the field accepts any `u64` including `0`. [8](#0-7) 

A secondary effect: in `process_orphan_tx`, `max_tx_verify_cycles` is used as a routing threshold. With value `0`, every orphan with any declared cycles (`> 0`) is routed to the async verify queue instead of direct processing, altering the processing path for all orphan transactions. [9](#0-8) 

---

### Impact Explanation

When `max_tx_verify_cycles` is set to `0` or below the actual cycle cost of any real transaction script:

1. **After every chain reorganization**, `readd_detached_tx` is called to re-verify and re-add all previously pending transactions. With `max_cycles = 0`, every script verification immediately fails. All pending transactions are permanently and silently dropped from the pool — they are not re-broadcast, not returned to the user, and not recorded in the reject store (since the error path in `readd_detached_tx` does not call `put_recent_reject`).

2. **Users who submitted transactions** to this node before the reorg lose their pending transactions with no notification. The transactions are valid under consensus rules (which uses `max_block_cycles` from the chain spec, not `max_tx_verify_cycles`), but the node's pool discards them.

3. The node continues to sync and validate blocks normally (block validation uses `consensus.max_block_cycles()`), so the misconfiguration is not immediately obvious.

---

### Likelihood Explanation

The entry path is a node operator (supported local CLI/RPC user) setting `max_tx_verify_cycles = 0` in `ckb.toml`. This is a supported, documented configuration field. The misconfiguration is easy to make accidentally (e.g., setting to `0` to "disable the limit" or setting an extremely low value for testing). There is no startup validation, no warning log, and no documented minimum value. Integration tests confirm the field is routinely set to low values like `100`, `500`, `1300`, `5000` for testing purposes, demonstrating the field is expected to be freely configurable. [10](#0-9) [11](#0-10) 

---

### Recommendation

1. Add a minimum floor validation when loading `TxPoolConfig`. Reject or clamp `max_tx_verify_cycles` to at least `TWO_IN_TWO_OUT_CYCLES` (the cost of a standard 2-in-2-out secp256k1 transaction) with a startup error or warning if the configured value is below a safe minimum.

2. In `readd_detached_tx`, use `max(self.tx_pool_config.max_tx_verify_cycles, consensus.max_block_cycles())` as the cycle limit, or use `consensus.max_block_cycles()` directly, since the purpose of re-adding detached transactions is to restore valid pool state — not to apply the operator's per-tx admission filter.

3. Document the minimum safe value for `max_tx_verify_cycles` in `ckb.toml` and the `TxPoolConfig` struct comment.

---

### Proof of Concept

1. Set `max_tx_verify_cycles = 0` in `ckb.toml` under `[tx_pool]`.
2. Start the node and submit several valid transactions (e.g., standard secp256k1 transfers). They enter the pool normally via `_process_tx`, which uses `declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())` — **not** `max_tx_verify_cycles` — so they are accepted.
3. Trigger a chain reorganization (e.g., in a dev environment with `permanent_difficulty_in_dummy = true`, mine a longer fork).
4. `readd_detached_tx` is called. It calls `verify_rtx(..., max_cycles=0, None)` for each transaction.
5. `ContextualTransactionVerifier::verify(0, false)` → `script.verify(0)` → `scheduler.run(RunMode::LimitCycles(0))` → immediate `CyclesExceeded`.
6. All transactions are silently dropped. The pool is empty. No reject records are written. Users receive no notification.

The existing test `DeclaredWrongCyclesChunk` (which sets `max_tx_verify_cycles = 500` below the actual script cost of `537`) already demonstrates that a below-threshold value causes rejection — the same mechanism applies to `readd_detached_tx` with `max_tx_verify_cycles = 0`. [12](#0-11)

### Citations

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** tx-pool/src/process.rs (L597-599)
```rust
            for orphan in orphans.into_iter() {
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
                    debug!(
```

**File:** tx-pool/src/process.rs (L884-912)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
                {
                    let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
                    if let Err(e) = _submit_entry(tx_pool, status, entry, &self.callbacks) {
                        error!("readd_detached_tx submit_entry {} error {}", tx_hash, e);
                    } else {
                        debug!("readd_detached_tx submit_entry {}", tx_hash);
                    }
                }
            }
```

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
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
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** verification/src/transaction_verifier.rs (L162-172)
```rust
    pub fn verify(&self, max_cycles: Cycle, skip_script_verify: bool) -> Result<Completed, Error> {
        self.time_relative.verify()?;
        self.capacity.verify()?;
        let cycles = if skip_script_verify {
            0
        } else {
            self.script.verify(max_cycles)?
        };
        let fee = self.fee_calculator.transaction_fee()?;
        Ok(Completed { cycles, fee })
    }
```

**File:** script/src/verify.rs (L553-564)
```rust
    fn run(&self, script_group: &ScriptGroup, max_cycles: Cycle) -> Result<Cycle, ScriptError> {
        let result = self.detailed_run(script_group, max_cycles)?;

        if result.exit_code == 0 {
            Ok(result.consumed_cycles)
        } else {
            Err(ScriptError::validation_failure(
                &script_group.script,
                result.exit_code,
            ))
        }
    }
```

**File:** util/app-config/src/legacy/tx_pool.rs (L13-14)
```rust
// default max tx verify cycles
const DEFAULT_MAX_TX_VERIFY_CYCLES: Cycle = TWO_IN_TWO_OUT_CYCLES * 20;
```

**File:** util/app-config/src/legacy/tx_pool.rs (L79-99)
```rust
impl Default for TxPoolConfig {
    fn default() -> Self {
        Self {
            max_mem_size: None,
            max_tx_pool_size: DEFAULT_MAX_TX_POOL_SIZE,
            max_cycles: None,
            max_verify_cache_size: None,
            max_conflict_cache_size: None,
            max_committed_txs_hash_cache_size: None,
            max_tx_verify_workers: default_max_tx_verify_workers(),
            keep_rejected_tx_hashes_days: default_keep_rejected_tx_hashes_days(),
            keep_rejected_tx_hashes_count: default_keep_rejected_tx_hashes_count(),
            min_fee_rate: DEFAULT_MIN_FEE_RATE,
            min_rbf_rate: DEFAULT_MIN_RBF_RATE,
            max_tx_verify_cycles: DEFAULT_MAX_TX_VERIFY_CYCLES,
            max_ancestors_count: DEFAULT_MAX_ANCESTORS_COUNT,
            persisted_data: Default::default(),
            recent_reject: Default::default(),
            expiry_hours: DEFAULT_EXPIRY_HOURS,
        }
    }
```

**File:** test/src/specs/relay/transaction_relay.rs (L176-178)
```rust
    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.tx_pool.max_tx_verify_cycles = 100;
    }
```

**File:** test/src/specs/tx_pool/declared_wrong_cycles.rs (L36-67)
```rust
pub struct DeclaredWrongCyclesChunk;

impl Spec for DeclaredWrongCyclesChunk {
    crate::setup!(num_nodes: 1);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &mut nodes[0];
        node0.mine_until_out_bootstrap_period();

        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::RelayV3],
        );
        net.connect(node0);

        let tx = node0.new_transaction_spend_tip_cellbase();

        relay_tx(&net, node0, tx, ALWAYS_SUCCESS_SCRIPT_CYCLE + 1);

        let result = wait_until(5, || {
            let tx_pool_info = node0.get_tip_tx_pool_info();
            tx_pool_info.orphan.value() == 0 && tx_pool_info.pending.value() == 0
        });
        assert!(result, "Declared wrong cycles should be rejected");
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 500; // ALWAYS_SUCCESS_SCRIPT_CYCLE: u64 = 537
    }
}
```
