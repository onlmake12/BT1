All code references check out. Let me verify one more detail about whether `_test_accept_tx` updates the cache (unlike `_process_tx`).

Audit Report

## Title
Unbounded CPU Exhaustion via `test_tx_pool_accept` RPC: No Rate Limit, No Cache Update, Full `max_block_cycles` Verification Per Call — (`tx-pool/src/process.rs`, `rpc/src/module/pool.rs`)

## Summary

`test_tx_pool_accept` triggers a full `ContextualTransactionVerifier` execution (up to `max_block_cycles` = 3.5 billion cycles) on every call via `block_in_place`, but `_test_accept_tx` never inserts the transaction into the verify queue, orphan pool, or the verify cache. The duplicate guard therefore always passes for the same transaction, and the cache is never populated, meaning every repeated call — even with identical witnesses — incurs the full CPU cost. An attacker with RPC access can saturate the tx-pool thread pool, stalling `send_transaction` and `get_block_template`.

## Finding Description

The RPC handler in `rpc/src/module/pool.rs` (L637–660) delegates directly to `tx_pool.test_accept_tx(tx)` with no rate limiting or concurrency guard. [1](#0-0) 

`test_accept_tx` in `tx-pool/src/process.rs` (L386–399) checks `verify_queue_contains` and `orphan_contains` before calling `_test_accept_tx`. Both checks use `proposal_short_id()` to look up the transaction in the verify queue and orphan pool respectively. [2](#0-1) 

`_test_accept_tx` (L779–800) **never inserts the transaction into either structure and never updates the verify cache**. It calls `verify_rtx` with `command_rx = None` and `max_cycles = self.consensus.max_block_cycles()`. [3](#0-2) 

Contrast with `_process_tx` (L758–764), which explicitly updates the cache after successful verification: [4](#0-3) 

Because `_test_accept_tx` never writes to the cache, `fetch_tx_verify_cache` (L76–79, keyed by `witness_hash`) always returns `None` for any transaction that has only been submitted via `test_tx_pool_accept`, regardless of how many times it has been called. [5](#0-4) 

`verify_rtx` in `tx-pool/src/util.rs` (L116–131), when `cache_entry` is `None` and `command_rx` is `None`, executes `block_in_place(|| ContextualTransactionVerifier::new(...).verify(max_tx_verify_cycles, false))`, blocking a tokio worker thread for the full duration of CKB-VM script execution. [6](#0-5) 

The cycle limit passed is `consensus.max_block_cycles()` — the full block cycle budget of 3.5 billion cycles (`MAX_BLOCK_CYCLES = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT = 3_500_000 * 1_000`), not the per-transaction limit `max_tx_verify_cycles` (70,000,000 per `ckb.toml`), making each call up to 50× more expensive than a normal `send_transaction`. [7](#0-6) [8](#0-7) 

The complete attack path:
1. Duplicate guard checks verify queue and orphan pool → both empty for this tx → passes.
2. `_test_accept_tx` → `pre_check` → `fetch_tx_verify_cache` → cache miss (never populated by this path).
3. `verify_rtx` with `command_rx = None` → `block_in_place(ContextualTransactionVerifier::verify(3.5B, false))`.
4. Thread blocked. Transaction not inserted anywhere. Cache not updated.
5. Next call: duplicate guard passes again (nothing was inserted). Cache miss again. Full verification again.

## Impact Explanation

Repeated concurrent calls to `test_tx_pool_accept` with a max-cycle script saturate the tokio blocking thread pool used by `block_in_place`. Once saturated, all tx-pool service operations that require a worker thread — including `send_transaction` and `get_block_template` — queue up behind the blocked workers, causing them to stall or time out. This constitutes a targeted denial-of-service that can render a CKB node's transaction pool and block assembly inoperable, matching the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**.

## Likelihood Explanation

The `Pool` module is enabled by default and `test_tx_pool_accept` requires no authentication. The RPC binds to `127.0.0.1:8114` by default, but any local process (wallet, tooling, proxied service) has access. Mining pools and exchanges routinely expose the RPC port more broadly. The attacker needs only: (a) RPC access, (b) a transaction referencing a script cell that loops to the cycle limit. No on-chain funds need to be spent; the transaction need not be valid for inclusion — it only needs to pass `pre_check` (cell resolution). The attack is repeatable indefinitely at zero marginal cost to the attacker.

## Recommendation

1. **Cap the cycle limit** in `_test_accept_tx` to `self.tx_pool_config.max_tx_verify_cycles` (70M) instead of `self.consensus.max_block_cycles()` (3.5B), consistent with normal transaction submission limits.
2. **Populate the verify cache** after a successful `_test_accept_tx` run (mirroring `_process_tx` L758–764), so repeated identical calls hit the cheap `TimeRelativeTransactionVerifier` path.
3. **Add a global concurrency semaphore** for `_test_accept_tx` (e.g., permit count = 1 or a small fixed number) to prevent concurrent saturation of the blocking thread pool.
4. **Apply per-IP or global rate limiting** at the RPC server layer for `test_tx_pool_accept`.

## Proof of Concept

1. Deploy (or reference an existing) script cell containing a tight loop consuming `max_tx_verify_cycles - 1` cycles.
2. Construct transaction `T` spending any live cell with that script as the lock, with any witness bytes.
3. In a loop (or concurrently from multiple threads):
   ```
   POST /rpc {"jsonrpc":"2.0","method":"test_tx_pool_accept","params":[T,"passthrough"],"id":1}
   ```
4. Each call enters `test_accept_tx` → duplicate guard passes (nothing in verify queue or orphan pool) → `_test_accept_tx` → `fetch_tx_verify_cache` returns `None` (cache never updated) → `verify_rtx` with `command_rx=None` → `block_in_place(ContextualTransactionVerifier::verify(3.5B, false))`.
5. Tokio blocking thread pool saturates. Subsequent `send_transaction` and `get_block_template` RPC calls time out.
6. Witness variation is not even required: since `_test_accept_tx` never writes to the cache, the same `witness_hash` always produces a cache miss.

### Citations

**File:** rpc/src/module/pool.rs (L637-660)
```rust
    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }
```

**File:** tx-pool/src/process.rs (L76-79)
```rust
    pub(crate) async fn fetch_tx_verify_cache(&self, tx: &TransactionView) -> Option<CacheEntry> {
        let guard = self.txs_verify_cache.read().await;
        guard.peek(&tx.witness_hash()).cloned()
    }
```

**File:** tx-pool/src/process.rs (L386-399)
```rust
    pub(crate) async fn test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, None).await?;

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }
        self._test_accept_tx(tx.clone()).await
    }
```

**File:** tx-pool/src/process.rs (L758-764)
```rust
        if verify_cache.is_none() {
            // update cache
            let txs_verify_cache = Arc::clone(&self.txs_verify_cache);
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
```

**File:** tx-pool/src/process.rs (L779-800)
```rust
    pub(crate) async fn _test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        let (pre_check_ret, snapshot) = self.pre_check(&tx).await;

        let (_tip_hash, rtx, status, _fee, _tx_size) = pre_check_ret?;

        // skip check the delay window

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = self.consensus.max_block_cycles();
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            None,
        )
        .await
    }
```

**File:** tx-pool/src/util.rs (L116-131)
```rust
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

**File:** spec/src/consensus.rs (L70-84)
```rust
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years

/// The default maximum allowed size in bytes for a block
pub const MAX_BLOCK_BYTES: u64 = TWO_IN_TWO_OUT_BYTES * TWO_IN_TWO_OUT_COUNT;
pub(crate) const MAX_BLOCK_CYCLES: u64 = TWO_IN_TWO_OUT_CYCLES * TWO_IN_TWO_OUT_COUNT;
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
