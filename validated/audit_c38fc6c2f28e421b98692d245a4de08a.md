### Title
Tx-Pool Accepts Transactions Exceeding Block Cycle Cap When Peer-Declared Cycles Bypass `max_block_cycles()` Guard — (`tx-pool/src/process.rs`)

---

### Summary

The CKB tx-pool uses a peer-supplied `declared_cycles` value as the script verification cycle limit when processing relayed transactions, without first checking whether that value exceeds the consensus `max_block_cycles()` cap enforced by block validation. A transaction whose actual cycles exceed `max_block_cycles()` can therefore be admitted into the tx-pool and will permanently occupy pool space, because no valid block can ever commit it.

---

### Finding Description

In `_process_tx`, the cycle ceiling passed to `verify_rtx` is chosen as:

```rust
let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
``` [1](#0-0) 

When a remote peer relays a transaction it supplies `(declared_cycles, peer_index)` as the `remote` argument. That declared value is forwarded directly into `_process_tx` as `declared_cycles: Option<Cycle>` with no upper-bound check against `consensus.max_block_cycles()`. [2](#0-1) 

After verification completes, the only guard is a **mismatch** check:

```rust
if let Some(declared) = declared_cycles
    && declared != verified.cycles
{
    return Some((Err(Reject::DeclaredWrongCycles(declared, verified.cycles)), snapshot));
}
``` [3](#0-2) 

If an attacker crafts a transaction whose script genuinely consumes `C` cycles where `C > max_block_cycles()`, and relays it with `declared_cycles = C`, then:

1. `max_cycles = C` (above the block cap).
2. `verify_rtx` succeeds because actual cycles ≤ `max_cycles`.
3. The mismatch check passes because `declared == verified`.
4. The transaction is inserted into the pool.

Block validation, however, enforces a hard aggregate cap:

```rust
if sum > self.context.consensus.max_block_cycles() {
    Err(BlockErrorKind::ExceededMaximumCycles.into())
}
``` [4](#0-3) 

A single transaction with `cycles > max_block_cycles()` makes the block's cycle sum exceed the cap, so the transaction can **never** be committed. The tx-pool has no analogous per-transaction cap check at admission time.

The `non_contextual_verify` path (called before `_process_tx`) only checks version, size, empty inputs/outputs, duplicate deps, outputs-data length, and script hash type — no cycle cap: [5](#0-4) 

The `TxPoolConfig` field `max_tx_verify_cycles` is documented as a rejection threshold, but in `_process_tx` it is **not** consulted — only `declared_cycles` is used when a remote value is present: [6](#0-5) 

---

### Impact Explanation

- **Pool pollution / resource exhaustion**: An attacker can fill the tx-pool with transactions that are permanently uncommittable. Because the pool enforces a size cap (`max_tx_pool_size`) and evicts by fee rate, these high-cycle transactions (which may carry a normal fee) displace legitimate transactions.
- **Wasted verification work**: Each such transaction triggers full script execution up to `declared_cycles`, consuming CPU on every node that relays it.
- **Block-assembly interference**: If the block assembler selects such a transaction before its cycle total is checked, it produces a template that fails consensus validation, wasting miner work.

---

### Likelihood Explanation

The attack path is reachable by any unprivileged P2P peer using the relay protocol (`RelayV3`). The attacker must:

1. Control a UTXO (to construct a valid transaction with real inputs/outputs and sufficient fee).
2. Deploy or reference a script that provably consumes `C > max_block_cycles()` cycles.
3. Relay the transaction with `declared_cycles = C`.

No privileged access, key material, or majority hash power is required. The relay entry point is the standard `SupportProtocols::RelayV3` handler, which feeds into `resumeble_process_tx` → `enqueue_verify_queue` → `_process_tx`. [7](#0-6) 

---

### Recommendation

Add an explicit guard in `_process_tx` (or at the verify-queue admission point) that rejects any transaction whose `declared_cycles` exceeds `consensus.max_block_cycles()`:

```rust
if let Some(declared) = declared_cycles {
    if declared > self.consensus.max_block_cycles() {
        return Some((
            Err(Reject::DeclaredWrongCycles(declared, self.consensus.max_block_cycles())),
            snapshot,
        ));
    }
}
```

Alternatively, clamp `max_cycles` to `min(declared_cycles, consensus.max_block_cycles())` so that any transaction whose actual cycles exceed the block cap is caught by the existing `DeclaredWrongCycles` path.

---

### Proof of Concept

1. Attacker owns UTXO `U` on CKB mainnet/testnet.
2. Attacker deploys a lock script that loops until it has consumed `max_block_cycles() + 1` cycles (e.g., by counting iterations in a tight RISC-V loop).
3. Attacker constructs transaction `T` spending `U`, locked by that script, with a fee satisfying `min_fee_rate`.
4. Attacker connects to a target node via `RelayV3` and sends a `RelayTransactionV3` message with `cycles = max_block_cycles() + 1`.
5. The node calls `resumeble_process_tx` → `_process_tx` with `declared_cycles = max_block_cycles() + 1`.
6. `verify_rtx` runs the script with limit `max_block_cycles() + 1`; the script completes in exactly that many cycles.
7. `declared == verified` → no `DeclaredWrongCycles` rejection.
8. `T` is inserted into the pending pool.
9. `T` can never appear in a committed block: any block containing it has `sum ≥ max_block_cycles() + 1`, failing `ExceededMaximumCycles` in `contextual_block_verifier.rs`.
10. Repeating with many UTXOs fills the pool, evicting legitimate transactions.

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

**File:** verification/contextual/src/contextual_block_verifier.rs (L468-470)
```rust
        if sum > self.context.consensus.max_block_cycles() {
            Err(BlockErrorKind::ExceededMaximumCycles.into())
        } else {
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

**File:** util/app-config/src/configs/tx_pool.rs (L20-22)
```rust
    /// tx pool rejects txs that cycles greater than max_tx_verify_cycles
    pub max_tx_verify_cycles: Cycle,
    /// max tx verify workers, default is 3/4 of cpu cores
```
