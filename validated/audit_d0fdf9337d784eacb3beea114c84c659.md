Based on my thorough code analysis, here is my assessment:

---

### Title
Cycle Limit Mismatch in `readd_detached_tx` Causes Permanent Pool Drop of High-Cycle Transactions After Reorg — (`tx-pool/src/process.rs`)

### Summary

`readd_detached_tx` re-verifies detached transactions using `tx_pool_config.max_tx_verify_cycles` as the cycle cap, while the original admission path for remote transactions uses `declared_cycles` (bounded only by `consensus.max_block_cycles()`). A transaction with actual cycles between these two limits can be admitted, then permanently silently dropped from the pool after any reorg when the verify cache is cold.

### Finding Description

**Admission path** (`_process_tx`, line 720):

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

For a remote peer submission via `submit_remote_tx`, `max_cycles = declared_cycles`. The only upper bound enforced on `declared_cycles` before this point is `max_block_cycles()`, checked in `transactions_process.rs` lines 64–74. There is no check that `declared_cycles <= max_tx_verify_cycles`. The verify queue (`verify_queue.rs` lines 212–213) marks such txs as `is_large_cycle = true` but does **not** reject them.

So a remote peer can relay a tx with `declared_cycles = X` where `max_tx_verify_cycles < X <= max_block_cycles()`. If the tx's actual cycles equal `X` exactly (required by the `DeclaredWrongCycles` check at lines 736–748), the tx is admitted and cached.

**Re-admission path** (`readd_detached_tx`, line 884):

```rust
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
```

This is a **lower** limit. When `verify_rtx` is called without a cache hit, it runs full script verification with this lower cap. A tx whose actual cycles exceed `max_tx_verify_cycles` will fail with `ExceededCycles` and be silently dropped (the `if let Ok(verified) =` at line 895 simply skips it with no error logged to the user).

**Cache mitigation and its limits:**

`readd_detached_tx` does fetch the verify cache (`fetched_cache`) and passes it to `verify_rtx`. When the cache hits, `verify_rtx` (util.rs lines 96–100) skips script re-execution entirely and returns the cached cycles — bypassing `max_cycles`. However, the cache is an LRU structure of bounded size (`txs_verify_cache_size`). Under any realistic load (many txs processed between admission and reorg), the entry can be evicted, making the cache miss path reachable.

### Impact Explanation

Any transaction with cycles in the range `(max_tx_verify_cycles, max_block_cycles()]` that was validly admitted via the P2P relay path is permanently and silently removed from the mempool after a reorg if its verify cache entry has been evicted. The transaction is not re-queued, not logged as rejected to the submitter, and not recoverable without external resubmission. This degrades pool completeness after reorgs and can cause miners to lose valid high-fee transactions.

### Likelihood Explanation

- `max_tx_verify_cycles` defaults to `TWO_IN_TWO_OUT_CYCLES * 20` (util/app-config/src/legacy/tx_pool.rs line 14), which is well below `max_block_cycles()`. The gap is large enough for real transactions.
- The `SendLargeCyclesTxToRelay` integration test (test/src/specs/tx_pool/send_large_cycles_tx.rs lines 96–141) explicitly demonstrates that a node with a low `max_tx_verify_cycles` accepts high-cycle txs relayed from a peer — confirming the admission path is production-reachable.
- Reorgs are a normal network event. Cache eviction under load is expected behavior.
- No privileged access, no majority hashpower, no social engineering required.

### Recommendation

In `readd_detached_tx`, replace the cycle limit with `consensus.max_block_cycles()` to match the original admission limit:

```rust
// Before (line 884):
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;

// After:
let max_cycles = self.consensus.max_block_cycles();
```

Alternatively, store the original `max_cycles` used at admission time in `TxEntry` and reuse it during re-admission.

### Proof of Concept

1. Configure a node with `max_tx_verify_cycles = 5_000` (as in the existing test at line 77 of `send_large_cycles_tx.rs`).
2. Relay a transaction whose actual cycles are, say, `50_000` (between `max_tx_verify_cycles` and `max_block_cycles()`), with `declared_cycles = 50_000`.
3. Confirm the tx is admitted to the pool (pending state).
4. Process enough other transactions to evict the tx's verify cache entry (LRU eviction).
5. Trigger a reorg (e.g., mine a longer competing chain that detaches the block containing the tx's inputs).
6. Assert the tx is absent from the pool after `readd_detached_tx` completes — it was silently dropped.

---

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/process.rs (L719-720)
```rust
        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
```

**File:** tx-pool/src/process.rs (L884-884)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
```

**File:** tx-pool/src/process.rs (L895-903)
```rust
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
```

**File:** tx-pool/src/util.rs (L96-100)
```rust
    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
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

**File:** test/src/specs/tx_pool/send_large_cycles_tx.rs (L96-141)
```rust
impl Spec for SendLargeCyclesTxToRelay {
    crate::setup!(num_nodes: 2, retry_failed: 5);

    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let node1 = &nodes[1];

        node0.mine_until_out_bootstrap_period();
        node1.mine_until_out_bootstrap_period();
        node0.connect(node1);
        info!("Generate large cycles tx");

        let tx = build_tx(node1, &self.random_key.privkey, self.random_key.lock_arg());
        // send tx
        let ret = node1.rpc_client().send_transaction_result(tx.data().into());
        assert!(ret.is_ok());

        info!("Node1 submit large cycles tx");

        let result = wait_until(60, || {
            node1.get_tip_block_number() == node0.get_tip_block_number()
        });
        assert!(result, "node0 can't sync with node1");

        let result = wait_until(120, || {
            node0
                .rpc_client()
                .get_transaction(tx.hash())
                .transaction
                .is_some()
        });
        if !result {
            info!("node0 last 500 log begin");
            node0.print_last_500_lines_log(&node0.log_path());
            info!("node0 last 500 log end");
        }
        assert!(result, "Node0 should accept tx");
    }

    fn modify_app_config(&self, config: &mut ckb_app_config::CKBAppConfig) {
        let lock_arg = self.random_key.lock_arg();
        config.network.connect_outbound_interval_secs = 0;
        config.tx_pool.max_tx_verify_cycles = 5000u64;
        let block_assembler = new_block_assembler_config(lock_arg, ScriptHashType::Type);
        config.block_assembler = Some(block_assembler);
    }
```
