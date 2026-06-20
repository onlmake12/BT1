### Title
`test_tx_pool_accept` Skips Delay Window Check, Producing False-Positive Simulation Results - (File: `tx-pool/src/process.rs`)

### Summary

The `_test_accept_tx` function, which backs the `test_tx_pool_accept` RPC, explicitly skips the delay window check that is enforced in the real `send_transaction` path. A transaction can therefore pass `test_tx_pool_accept` and return success, yet be rejected when actually submitted via `send_transaction`. This is the direct CKB analog of the AtlasVerification deadline-check-skipped-in-simulation-mode bug: a simulation succeeds while the real submission fails.

### Finding Description

In `tx-pool/src/process.rs`, the internal function `_test_accept_tx` (lines 779–800) contains the explicit comment `// skip check the delay window` at line 784, immediately after extracting the resolved transaction and status from `pre_check`. The function then proceeds directly to `verify_rtx` without performing the delay window check that the normal submission path enforces.

```
779: pub(crate) async fn _test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
780:     let (pre_check_ret, snapshot) = self.pre_check(&tx).await;
781:
782:     let (_tip_hash, rtx, status, _fee, _tx_size) = pre_check_ret?;
783:
784:     // skip check the delay window          <── check intentionally omitted
785:
786:     let verify_cache = self.fetch_tx_verify_cache(&tx).await;
787:     let max_cycles = self.consensus.max_block_cycles();
788:     let tip_header = snapshot.tip_header();
789:     let tx_env = Arc::new(status.with_env(tip_header));
790:
791:     verify_rtx(Arc::clone(&snapshot), Arc::clone(&rtx), tx_env,
792:                &verify_cache, max_cycles, None).await
793: }
```

`_test_accept_tx` is called by `test_accept_tx` (lines 386–399), which is the implementation of the public `test_tx_pool_accept` RPC method (exposed in `rpc/src/module/pool.rs`, lines 637–660).

The delay window check enforced in the real submission path (`_process_tx`) verifies that a transaction's time-lock conditions (`since` field) are compatible with the current chain state **accounting for the mandatory proposal window** — i.e., the transaction must be proposed in block N and can only be committed in blocks N+w\_close through N+w\_far. A transaction whose `since` value requires a block number that is reachable only after the proposal window has elapsed will be rejected by `send_transaction` but accepted by `test_tx_pool_accept`.

The `TxStatus::with_env` mapping (lines 56–63 of `process.rs`) and `TxVerifyEnv::new_submit` / `new_proposed` encode this window into the environment used by `SinceVerifier`. Because `_test_accept_tx` skips the delay window check entirely, the `TxVerifyEnv` constructed at line 789 is never validated against the proposal-window constraint before `verify_rtx` is called.

### Impact Explanation

Any RPC caller — wallet software, dApp, or developer tooling — that uses `test_tx_pool_accept` to pre-validate a transaction before broadcasting it via `send_transaction` will receive a false-positive result. The caller will believe the transaction is valid and submit it, only to have it rejected by the real tx-pool with an `Immature` error. This wastes network resources (the transaction is broadcast to peers before being rejected), misleads users, and breaks any workflow that relies on `test_tx_pool_accept` as a reliable pre-flight check — which is its documented purpose.

### Likelihood Explanation

The `test_tx_pool_accept` RPC is a public, unauthenticated endpoint available to any RPC caller. Transactions with non-trivial `since` fields (time-locks, relative block-number locks, epoch locks) are common in CKB applications (e.g., NervosDAO withdrawals, multisig timelocks). Any such transaction submitted near the boundary of its maturity window will trigger the discrepancy. The likelihood is therefore realistic for any operator or user who uses `test_tx_pool_accept` as a pre-submission validator.

### Recommendation

Remove the `// skip check the delay window` shortcut in `_test_accept_tx` and apply the same delay window check that `_process_tx` enforces. The simulation path should be a faithful replica of the real submission path so that a success result from `test_tx_pool_accept` guarantees acceptance by `send_transaction`.

### Proof of Concept

1. Mine the chain to tip block T.
2. Craft a transaction whose `since` field encodes an absolute block number `S = T + proposal_window.closest - 1` (one block before the transaction would be mature enough to enter the pending pool via `send_transaction`).
3. Call `test_tx_pool_accept` with this transaction → returns `Ok` with cycles and fee (delay window check skipped).
4. Call `send_transaction` with the identical transaction → returns `TransactionFailedToVerify: Verification failed Transaction(Immature(...))` because the delay window check is enforced.

The discrepancy is rooted at: [1](#0-0) 

called from: [2](#0-1) 

exposed via: [3](#0-2) 

The delay window check that is present in the real submission path but absent here is the `since`-field maturity enforcement performed through `TxStatus::with_env` and `TimeRelativeTransactionVerifier`: [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/process.rs (L55-63)
```rust
impl TxStatus {
    fn with_env(self, header: &HeaderView) -> TxVerifyEnv {
        match self {
            TxStatus::Fresh => TxVerifyEnv::new_submit(header),
            TxStatus::Gap => TxVerifyEnv::new_proposed(header, 0),
            TxStatus::Proposed => TxVerifyEnv::new_proposed(header, 1),
        }
    }
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

**File:** tx-pool/src/util.rs (L134-148)
```rust
pub(crate) fn time_relative_verify(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: TxVerifyEnv,
) -> Result<(), Reject> {
    let consensus = snapshot.cloned_consensus();
    TimeRelativeTransactionVerifier::new(
        rtx,
        consensus,
        snapshot.as_data_loader(),
        Arc::new(tx_env),
    )
    .verify()
    .map_err(Reject::Verification)
}
```
