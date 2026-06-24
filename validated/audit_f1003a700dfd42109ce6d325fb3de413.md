Audit Report

## Title
`max_tx_verify_cycles` Bypassed for Local RPC Submissions, Allowing Excessive CPU Consumption — (`tx-pool/src/process.rs`)

## Summary

In `TxPoolService::_process_tx`, when a transaction is submitted via the local RPC `send_transaction` endpoint (`declared_cycles` is `None`), the cycle budget falls back to `consensus.max_block_cycles()` rather than the operator-configured `TxPoolConfig.max_tx_verify_cycles`. This allows any local process to force the node to run CKB-VM script verification with a budget up to ~50× larger than the operator intended, consuming disproportionate CPU per submission and potentially delaying block assembly and legitimate transaction processing.

## Finding Description

`TxPoolConfig` carries a dedicated field to cap per-transaction verification cost:

```rust
// util/app-config/src/configs/tx_pool.rs line 21
pub max_tx_verify_cycles: Cycle,   // default 70_000_000
``` [1](#0-0) 

For the async remote-tx (relay) path, this value is correctly threaded into `VerifyQueue` at service startup:

```rust
// tx-pool/src/service.rs lines 576-578
let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
    self.tx_pool_config.max_tx_verify_cycles,
)));
``` [2](#0-1) 

However, in `_process_tx` — which handles **both** remote and local submissions — the cycle limit is computed as:

```rust
// tx-pool/src/process.rs line 720
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [3](#0-2) 

For a locally submitted transaction (RPC `send_transaction`), `declared_cycles` is `None`, so `max_cycles` becomes `consensus.max_block_cycles()` — the consensus-level cap for an entire block (3,500,000,000 cycles on mainnet), not the operator-configured `max_tx_verify_cycles` (70,000,000 by default). This value is forwarded directly to `verify_rtx`: [4](#0-3) 

Inside `verify_rtx`, it is passed as the hard cycle ceiling to `ContextualTransactionVerifier::verify` and `verify_with_pause`: [5](#0-4) 

The configured `max_tx_verify_cycles` stored in `self.tx_pool_config` is never read in this code path. The existing integration test `SendLargeCyclesTxInBlock` (which sets `max_tx_verify_cycles = 5000u64` and expects a large-cycles tx submitted via local RPC to be accepted) independently confirms this bypass is present and observable: [6](#0-5) 

## Impact Explanation

**Impact: Low (501–2000 points) — Important performance improvement for CKB.**

The operator-configured `max_tx_verify_cycles` resource limit is silently ineffective for the local RPC submission path. A local process can submit transactions whose scripts consume up to `max_block_cycles` (3.5B) cycles, forcing the node to run CKB-VM for ~50× longer per submission than the configured cap allows. Repeated submissions can saturate the verification thread pool, delaying block assembly and legitimate transaction processing on the affected node. This does not crash the node or cause network-wide congestion, placing it in the "Low" performance improvement category rather than "High" or above.

## Likelihood Explanation

The RPC endpoint is bound to `127.0.0.1:8114` by default, restricting access to local processes. Any process on the same host — including a compromised indexer, a malicious script, or a user with shell access — can reach this path without privileged credentials. No special transaction crafting is required beyond constructing a script that consumes many cycles. The attack is repeatable with minimal cost to the submitter.

## Recommendation

Replace the fallback in `_process_tx` with the configured `max_tx_verify_cycles`:

```diff
- let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
+ let max_cycles = declared_cycles.unwrap_or(self.tx_pool_config.max_tx_verify_cycles);
```

This mirrors the pattern already used for the async remote-tx path via `VerifyQueue` and ensures the operator-configured limit is consistently enforced regardless of submission origin. [7](#0-6) 

## Proof of Concept

1. Start a CKB node with `tx_pool.max_tx_verify_cycles = 70_000_000` (default config).
2. Craft a transaction whose lock script runs a tight loop consuming close to `max_block_cycles` (3.5B) cycles before exiting with code 0.
3. Submit via `send_transaction` RPC (`curl` or `ckb-cli`).
4. Observe (via CPU profiling or timing) that the verification thread runs the script for the full `max_block_cycles` budget, not the 70M limit.
5. Repeat submissions in a loop to saturate the verification thread pool and measure the delay imposed on block assembly.

The existing test `SendLargeCyclesTxInBlock` (setting `max_tx_verify_cycles = 5000u64` and expecting a large-cycles tx to be accepted via local RPC) already serves as a functional proof that the bypass is present. [8](#0-7)

### Citations

**File:** util/app-config/src/configs/tx_pool.rs (L20-22)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```

**File:** tx-pool/src/service.rs (L576-578)
```rust
        let verify_queue = Arc::new(RwLock::new(VerifyQueue::new(
            self.tx_pool_config.max_tx_verify_cycles,
        )));
```

**File:** tx-pool/src/process.rs (L719-720)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L724-732)
```rust
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

**File:** test/src/specs/tx_pool/send_large_cycles_tx.rs (L34-80)
```rust
impl Spec for SendLargeCyclesTxInBlock {
    crate::setup!(num_nodes: 2);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let node1 = &nodes[1];

        node1.mine_until_out_bootstrap_period();
        info!("Generate large cycles tx");
        let tx = build_tx(node1, &self.random_key.privkey, self.random_key.lock_arg());

        info!("Node0 mine large cycles tx");
        node0.connect(node1);
        let result = wait_until(60, || {
            node1.get_tip_block_number() == node0.get_tip_block_number()
        });
        assert!(result, "node0 can't sync with node1");
        node0.disconnect(node1);
        let ret = node0.rpc_client().send_transaction_result(tx.data().into());
        ret.expect("package large cycles tx");
        let result = wait_until(60, || {
            let ret = node0
                .rpc_client()
                .get_transaction_with_verbosity(tx.hash(), 1);
            matches!(ret.tx_status.status, Status::Pending)
        });
        assert!(result, "large cycles tx rejected by node0");
        node0.mine_until_transaction_confirm(&tx.hash());
        let block: BlockView = node0.get_tip_block();
        assert_eq!(block.transactions()[1], tx);
        node0.connect(node1);

        info!("Wait block relay to node1");
        let result = wait_until(60, || {
            let block2: BlockView = node1.get_tip_block();
            block2.hash() == block.hash()
        });
        assert!(result, "block can't relay to node1");
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        let lock_arg = self.random_key.lock_arg();
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 5000u64;
        let block_assembler = new_block_assembler_config(lock_arg, ScriptHashType::Type);
        config.block_assembler = Some(block_assembler);
    }
```
