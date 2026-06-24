Audit Report

## Title
Async worker path uses peer-supplied `declared_cycles` as VM cycle limit, bypassing `max_tx_verify_cycles` — (`tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the cycle limit passed to `verify_rtx` is taken directly from the peer-supplied `declared_cycles` value without being capped at `max_tx_verify_cycles`. A remote peer can declare cycles up to `max_block_cycles`, causing the node to run the VM for that many cycles. If the script actually consumes exactly the declared number of cycles, the transaction passes the only post-verification guard and enters the pool with `cycles > max_tx_verify_cycles`, violating the operator-configured per-transaction cycle limit and enabling CPU exhaustion.

## Finding Description
At `tx-pool/src/process.rs` line 720, `_process_tx` computes:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [1](#0-0) 

This value is passed directly to `verify_rtx` as the VM cycle cap:

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
``` [2](#0-1) 

The async worker in `verify_mgr.rs` calls `_process_tx` with `entry.remote.map(|e| e.0)` as `declared_cycles`, which is the raw peer-supplied value with no prior capping: [3](#0-2) 

The only post-verification guard is:

```rust
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{ ... return Err(Reject::DeclaredWrongCycles(...)) }
``` [4](#0-3) 

This rejects only when `declared != verified.cycles`. If a peer crafts a script consuming exactly X cycles (where `max_tx_verify_cycles < X <= max_block_cycles`) and declares `X`, then `declared == verified.cycles` and the transaction is accepted into the pool with `cycles = X > max_tx_verify_cycles`. There is no guard anywhere in `_process_tx` or `submit_entry` enforcing `verified.cycles <= max_tx_verify_cycles`.

The entry path `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` performs no pre-rejection for `declared_cycles > max_tx_verify_cycles`: [5](#0-4) 

`non_contextual_verify` checks only transaction size and cellbase status, not cycles: [6](#0-5) 

`VerifyQueue.add_tx` uses `declared_cycles > large_cycle_threshold` only to set the `is_large_cycle` routing flag, not to reject: [7](#0-6) 

`process_orphan_tx` explicitly routes orphans with `cycle > max_tx_verify_cycles` to the async verify queue, confirming this path is reachable for high-cycle transactions: [8](#0-7) 

The `max_tx_verify_cycles` config field is documented as a DoS mitigation but is never enforced as a cap in `_process_tx`: [9](#0-8) 

## Impact Explanation
This is a CPU exhaustion / DoS vector against a CKB node. An attacker can submit many transactions each consuming near-`max_block_cycles` cycles, multiplying per-transaction verification cost far beyond the operator-configured `max_tx_verify_cycles` limit. The cost multiplier is `max_block_cycles / max_tx_verify_cycles`. Additionally, transactions with `cycles > max_tx_verify_cycles` enter the pending pool, potentially causing block assembly issues. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
Any unprivileged remote peer can relay a transaction via the standard P2P relay protocol with an arbitrary `declared_cycles` value up to `max_block_cycles`. No proof-of-work, key material, or privileged access is required. The attack is trivially repeatable and requires only crafting a script that consumes a precise cycle count and relaying it with a matching `declared_cycles` field.

## Recommendation
In `_process_tx`, cap `max_cycles` at `max_tx_verify_cycles` for the async worker path:

```rust
let max_cycles = declared_cycles
    .unwrap_or_else(|| self.consensus.max_block_cycles())
    .min(self.tx_pool_config.max_tx_verify_cycles);
```

Additionally, after verification, explicitly reject if `verified.cycles > self.tx_pool_config.max_tx_verify_cycles` with `Reject::ExceededMaximumCycles`. Optionally, add a pre-enqueue rejection in `resumeble_process_tx` when `declared_cycles > max_tx_verify_cycles` to fail fast before any VM execution.

## Proof of Concept
1. Configure a node with `max_tx_verify_cycles = 70_000_000`.
2. Craft a CKB-VM script (e.g., using a tight loop) that consumes exactly `80_000_000` cycles.
3. Relay the transaction via P2P with `declared_cycles = 80_000_000`.
4. The async worker calls `_process_tx` with `declared_cycles = Some(80_000_000)`, sets `max_cycles = 80_000_000`, runs the VM to 80M cycles, finds `declared == verified.cycles`, and inserts the entry into the pool.
5. Assert: the transaction is present in the pool with `cycles = 80_000_000 > max_tx_verify_cycles = 70_000_000`, violating the configured invariant.
6. Repeat with many such transactions to demonstrate CPU exhaustion proportional to `max_block_cycles / max_tx_verify_cycles`.

### Citations

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L598-614)
```rust
                if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
                    debug!(
                        "process_orphan {} added to verify queue; find previous from {}",
                        orphan.tx.hash(),
                        tx.hash(),
                    );
                    let orphan_id = orphan.tx.proposal_short_id();
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
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

**File:** tx-pool/src/util.rs (L56-83)
```rust
pub(crate) fn non_contextual_verify(
    consensus: &Consensus,
    tx: &TransactionView,
) -> Result<(), Reject> {
    NonContextualTransactionVerifier::new(tx, consensus)
        .verify()
        .map_err(Reject::Verification)?;

    // The ckb consensus does not limit the size of a single transaction,
    // but if the size of the transaction is close to the limit of the block,
    // it may cause the transaction to fail to be packed
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
    // cellbase is only valid in a block, not as a loose transaction
    if tx.is_cellbase() {
        return Err(Reject::Malformed(
            "cellbase like".to_owned(),
            Default::default(),
        ));
    }

    Ok(())
}
```

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```
