Audit Report

## Title
Async worker path uses peer-supplied `declared_cycles` as VM cycle limit, bypassing `max_tx_verify_cycles` cap — (`tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, `max_cycles` is set directly from peer-supplied `declared_cycles` without being capped at `self.tx_pool_config.max_tx_verify_cycles`. The async worker in `verify_mgr.rs` feeds raw peer-supplied cycle values into this path. As a result, a remote peer can force the node to run the VM for up to `max_block_cycles` cycles per transaction, and transactions exceeding the operator-configured `max_tx_verify_cycles` limit can enter the pending pool, violating the documented invariant.

## Finding Description
At `tx-pool/src/process.rs:720`, `max_cycles` is assigned without any cap:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [1](#0-0) 

This value is passed directly to `verify_rtx` at lines 724–731: [2](#0-1) 

The async worker in `verify_mgr.rs` calls `_process_tx` with `entry.remote.map(|e| e.0)` — the raw peer-supplied cycle count — as `declared_cycles`: [3](#0-2) 

The only post-verification guard checks `declared != verified.cycles` (lines 736–749), but never checks `verified.cycles <= max_tx_verify_cycles`: [4](#0-3) 

`submit_entry` (lines 96–137) also performs no such check before inserting the entry into the pool: [5](#0-4) 

By contrast, `readd_detached_tx` at line 884 correctly caps the VM limit:

```rust
let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
``` [6](#0-5) 

`process_orphan_tx` at lines 598–614 explicitly routes orphan txs with `cycle > max_tx_verify_cycles` to the async verify queue with their full declared cycle count, confirming the async path is the intended handler for high-cycle txs — but it never enforces the cap: [7](#0-6) 

`TxPoolConfig` documents the invariant that is violated:

```rust
/// tx pool rejects txs that cycles greater than max_tx_verify_cycles
pub max_tx_verify_cycles: Cycle,
``` [8](#0-7) 

The `Reject` enum has no `ExceededMaximumCycles` variant, confirming no rejection path exists for this condition: [9](#0-8) 

## Impact Explanation
An unprivileged remote peer can relay a transaction with `declared_cycles = max_block_cycles`, causing the node to run the VM for up to `max_block_cycles` cycles per transaction instead of the operator-configured `max_tx_verify_cycles`. Operators who lower `max_tx_verify_cycles` (e.g., to 1,000,000) face a CPU amplification ratio of up to ~5,900× with default `max_block_cycles`. Additionally, transactions with `cycles > max_tx_verify_cycles` enter the pending pool, violating the documented invariant and potentially causing block assembly issues. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
Any unprivileged remote peer can relay a transaction via the standard P2P relay protocol with an arbitrary `declared_cycles` value up to `max_block_cycles`. No PoW, key, or privileged access is required. The attacker must pay transaction fees, but the attack is repeatable and the CPU cost amplification can be large depending on operator configuration. The `DeclaredWrongCycles` check requires the script to actually consume exactly the declared cycles, which is achievable with a deterministic CKB-VM loop.

## Recommendation
In `_process_tx` at `tx-pool/src/process.rs:720`, cap `max_cycles` at `max_tx_verify_cycles` for all paths:

```rust
let max_cycles = declared_cycles
    .unwrap_or_else(|| self.consensus.max_block_cycles())
    .min(self.tx_pool_config.max_tx_verify_cycles);
```

Additionally, after verification, explicitly reject if `verified.cycles > self.tx_pool_config.max_tx_verify_cycles` with a new `Reject::ExceededMaximumCycles` variant in `util/types/src/core/tx_pool.rs`. [9](#0-8) 

## Proof of Concept
1. Configure a CKB node with `max_tx_verify_cycles = 1_000_000` and default `max_block_cycles` (~5.9B).
2. Craft a CKB-VM script (tight deterministic loop) that consumes exactly `N` cycles where `N > max_tx_verify_cycles` and `N <= max_block_cycles`.
3. Relay the transaction via P2P with `declared_cycles = N`.
4. The async worker in `verify_mgr.rs` calls `_process_tx` with `declared_cycles = Some(N)`.
5. `_process_tx` sets `max_cycles = N` (line 720), runs the VM to `N` cycles, finds `declared == verified.cycles`, and calls `submit_entry`.
6. Assert: the transaction is present in the pool with `cycles = N > max_tx_verify_cycles`, violating the documented invariant. The node spent `N` CPU cycles on verification instead of the configured `max_tx_verify_cycles` limit.

### Citations

**File:** tx-pool/src/process.rs (L96-137)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
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

**File:** tx-pool/src/process.rs (L720-720)
```rust
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

**File:** tx-pool/src/process.rs (L884-884)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
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

**File:** util/app-config/src/configs/tx_pool.rs (L20-21)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
```

**File:** util/types/src/core/tx_pool.rs (L17-67)
```rust
pub enum Reject {
    /// Transaction fee lower than config
    #[error(
        "The min fee rate is {0}, requiring a transaction fee of at least {1} shannons, but the fee provided is only {2}"
    )]
    LowFeeRate(FeeRate, u64, u64),

    /// Transaction exceeded maximum ancestors count limit
    #[error("Transaction exceeded maximum ancestors count limit; try later")]
    ExceededMaximumAncestorsCount,

    /// Transaction exceeded maximum size limit
    #[error("Transaction size {0} exceeded maximum limit {1}")]
    ExceededTransactionSizeLimit(u64, u64),

    /// Transaction are replaced because the pool is full
    #[error("Transaction is replaced because the pool is full, {0}")]
    Full(String),

    /// Transaction already exists in transaction_pool
    #[error("Transaction({0}) already exists in transaction_pool")]
    Duplicated(Byte32),

    /// Malformed transaction
    #[error("Malformed {0} transaction")]
    Malformed(String, String),

    /// Declared wrong cycles
    #[error("Declared wrong cycles {0}, actual {1}")]
    DeclaredWrongCycles(Cycle, Cycle),

    /// Resolve failed
    #[error("Resolve failed {0}")]
    Resolve(OutPointError),

    /// Verification failed
    #[error("Verification failed {0}")]
    Verification(Error),

    /// Expired
    #[error("Expiry transaction, timestamp {0}")]
    Expiry(u64),

    /// RBF rejected
    #[error("RBF rejected: {0}")]
    RBFRejected(String),

    /// Invalidated by cell consuming Tx
    #[error("Invalidated: {0}")]
    Invalidated(String),
}
```
