Looking at the critical code path in `tx-pool/src/process.rs`, specifically `_process_tx`: [1](#0-0) 

The key line is:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [2](#0-1) 

When the async worker calls `_process_tx` with `declared_cycles = Some(80_000_000)`, `max_cycles` is set to `80_000_000` — not `max_tx_verify_cycles` (70_000_000). The VM then runs up to 80M cycles.

After verification, the only guard is:

```rust
if let Some(declared) = declared_cycles && declared != verified.cycles {
    return Some((Err(Reject::DeclaredWrongCycles(...)), snapshot));
}
``` [3](#0-2) 

If the tx actually consumes exactly 80M cycles and the peer declared 80M, `declared == verified.cycles` — no rejection. There is **no check** that `verified.cycles <= max_tx_verify_cycles` anywhere in `_process_tx`.

The worker in `verify_mgr.rs` passes `entry.remote.map(|e| e.0)` as `declared_cycles`: [4](#0-3) 

The `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` path has no pre-rejection for `declared_cycles > max_tx_verify_cycles`: [5](#0-4) 

Notably, `process_orphan_tx` confirms that txs with `cycle > max_tx_verify_cycles` are intentionally routed to the async verify queue — they are not rejected at enqueue time: [6](#0-5) 

---

### Title
Async worker path uses `declared_cycles` as VM cycle limit instead of `max_tx_verify_cycles`, allowing remote peers to bypass the configured per-transaction cycle cap — (`tx-pool/src/process.rs`)

### Summary
In `TxPoolService::_process_tx`, the cycle limit passed to `verify_rtx` is `declared_cycles` (peer-supplied) rather than `max_tx_verify_cycles`. A remote peer can declare cycles up to `max_block_cycles`, causing the node to spend up to `max_block_cycles` CPU cycles verifying a single transaction and allowing that transaction to enter the pool with cycles exceeding `max_tx_verify_cycles`.

### Finding Description
`_process_tx` at line 720 sets `max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles())`. For remote transactions processed by the async worker, `declared_cycles = Some(peer_declared_value)`. If the peer declares 80M cycles (above `max_tx_verify_cycles = 70M` but below `max_block_cycles`), the VM is allowed to run 80M cycles. The post-verification check only rejects if `declared != verified.cycles` — if the script actually consumes exactly 80M cycles, both values match and the transaction is accepted into the pool. No guard enforces `verified.cycles <= max_tx_verify_cycles`.

### Impact Explanation
- The node spends up to `max_block_cycles` CPU cycles verifying a single remote transaction, far exceeding the operator-configured `max_tx_verify_cycles` limit.
- Transactions with cycles > `max_tx_verify_cycles` enter the pending pool, potentially causing block assembly issues (the block assembler must then handle entries that exceed the per-tx policy limit).
- This is a CPU exhaustion / DoS vector: an attacker can submit many transactions each consuming near-`max_block_cycles` cycles, multiplying the verification cost per transaction by up to `max_block_cycles / max_tx_verify_cycles` (~1.43× with default values, but configurable to much larger ratios).

### Likelihood Explanation
Any unprivileged remote peer can relay a transaction via the standard P2P relay protocol with an arbitrary `declared_cycles` value (up to `max_block_cycles`). No PoW, key, or privileged access is required. The attack is trivially repeatable.

### Recommendation
In `_process_tx`, cap `max_cycles` at `max_tx_verify_cycles` when processing transactions through the async worker path:

```rust
let max_cycles = declared_cycles
    .unwrap_or_else(|| self.consensus.max_block_cycles())
    .min(self.tx_pool_config.max_tx_verify_cycles);  // add this
```

Additionally, after verification, explicitly reject if `verified.cycles > self.tx_pool_config.max_tx_verify_cycles` with `Reject::ExceededMaximumCycles`.

### Proof of Concept
1. Configure node with `max_tx_verify_cycles = 70_000_000`.
2. Craft a transaction whose CKB-VM script (using `spawn` or a tight loop) consumes exactly 80_000_000 cycles.
3. Relay the transaction via P2P with `declared_cycles = 80_000_000`.
4. The async worker calls `_process_tx` with `declared_cycles = Some(80_000_000)`, sets `max_cycles = 80_000_000`, runs the VM to 80M cycles, finds `declared == verified`, and inserts the entry into the pool with `cycles = 80_000_000 > max_tx_verify_cycles`.
5. Assert: the transaction is present in the pool with `cycles = 80_000_000`, violating the `max_tx_verify_cycles` invariant.

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

**File:** tx-pool/src/process.rs (L705-732)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

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
