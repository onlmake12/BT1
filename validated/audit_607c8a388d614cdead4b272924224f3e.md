### Title
Tx-Pool Deduplication via Truncated `ProposalShortId` Allows Attacker to DOS Victim's Transaction Submission — (`tx-pool/src/util.rs`, `tx-pool/src/component/orphan.rs`)

---

### Summary

The CKB tx-pool uses `ProposalShortId` — the first 10 bytes of `tx_hash` — as the sole deduplication key across the pending pool, verify queue, and orphan pool. Because `tx_hash` is computed over inputs, outputs, cell_deps, header_deps, and version **but excludes witnesses**, two transactions with identical inputs/outputs but different witnesses share the same `ProposalShortId`. An unprivileged attacker who observes a victim's pending transaction on the P2P relay network can craft a structurally identical transaction with invalid witnesses, submit it first, and cause the victim's transaction to be permanently rejected with `PoolRejectedDuplicatedTransaction` for as long as the attacker repeats the submission.

---

### Finding Description

**Root cause 1 — `check_txid_collision` in `tx-pool/src/util.rs`:** [1](#0-0) 

The function derives the deduplication key as `tx.proposal_short_id()`, which is the first 10 bytes of `tx_hash`: [2](#0-1) 

`tx_hash` is computed over the raw transaction fields (inputs, outputs, cell_deps, header_deps, version) and **does not cover witnesses**. Therefore two transactions that differ only in their witness fields produce the same `tx_hash` and the same `ProposalShortId`. This is explicitly confirmed by the integration test helper: [3](#0-2) 

**Root cause 2 — `add_orphan_tx` in `tx-pool/src/component/orphan.rs`:** [4](#0-3) 

The orphan pool is also keyed by `proposal_short_id`. If an attacker's transaction occupies the slot, a legitimate transaction with the same `ProposalShortId` is silently dropped.

**Root cause 3 — `resumeble_process_tx` / `process_tx` in `tx-pool/src/process.rs`:** [5](#0-4) [6](#0-5) 

Both the relay path (`resumeble_process_tx`) and the RPC path (`process_tx`) check the orphan pool and verify queue by `proposal_short_id` before admitting a transaction. If the attacker's transaction is in either location, the victim's transaction is rejected immediately.

**Non-contextual verification does not check witnesses:** [7](#0-6) 

A transaction with empty or invalid witnesses passes all non-contextual checks and enters the verify queue. Script failure only occurs later, during contextual verification. During that window the `ProposalShortId` slot is occupied and the victim's transaction is rejected.

---

### Impact Explanation

An attacker can continuously block any specific victim transaction from entering the tx-pool:

1. The attacker's malformed transaction (invalid witnesses) passes non-contextual checks and occupies the `ProposalShortId` slot in the verify queue.
2. The victim's valid transaction is rejected with `PoolRejectedDuplicatedTransaction`.
3. The attacker's transaction eventually fails script verification and is evicted.
4. The attacker immediately resubmits a new malformed transaction.
5. The victim's transaction is rejected again.

This loop can be sustained indefinitely at low cost to the attacker (only transaction submission fees, if any). The victim's transaction never confirms. For time-sensitive operations (e.g., DAO withdrawals, time-locked cells, or any transaction with a `since` constraint), this can cause the victim to miss a valid window permanently.

---

### Likelihood Explanation

- **Attacker entry path**: Any unprivileged P2P relay peer or RPC caller. No keys or privileges required.
- **Information needed**: The victim's transaction inputs and outputs. These are visible in the relay network as soon as the victim broadcasts the transaction (`RelayTransaction` message).
- **Execution**: The attacker strips or replaces the witness field (trivial), producing a transaction with the same `tx_hash` and `ProposalShortId`. The attacker submits this to the target node via RPC or relay before the victim's transaction arrives. A direct P2P connection to the target node makes this reliable.
- **Cost**: Negligible. No PoW, no stake, no privileged access.

---

### Recommendation

1. **Use the full `tx_hash` (or `witness_hash`) as the pool deduplication key** instead of the 10-byte `ProposalShortId`. The `ProposalShortId` is appropriate for the proposal zone (where witnesses are irrelevant) but not for pool admission deduplication.
2. **In `check_txid_collision`**, compare the full `tx.hash()` against the stored transaction's hash when a `ProposalShortId` collision is detected. If the hashes differ, the incoming transaction is a distinct transaction and should not be rejected as a duplicate.
3. **In `OrphanPool::add_orphan_tx`**, apply the same full-hash comparison before silently dropping a transaction.

---

### Proof of Concept

```
1. Alice constructs tx_alice:
     inputs:  [outpoint_X]
     outputs: [cell_Y]
     witnesses: [valid_secp256k1_signature]
   tx_hash(tx_alice) = H   (witnesses excluded)
   proposal_short_id(tx_alice) = H[0..10]

2. Alice broadcasts tx_alice via the P2P relay network.

3. Attacker observes tx_alice (inputs/outputs are plaintext in RelayTransaction).

4. Attacker constructs tx_attack:
     inputs:  [outpoint_X]   ← identical
     outputs: [cell_Y]       ← identical
     witnesses: []           ← empty / invalid
   tx_hash(tx_attack) = H   ← same, witnesses excluded
   proposal_short_id(tx_attack) = H[0..10]  ← same

5. Attacker submits tx_attack to the node via send_transaction RPC.
   - non_contextual_verify: PASS (no witness check)
   - orphan_contains(H[0..10]): false → PASS
   - verify_queue_contains(H[0..10]): false → PASS
   - tx_attack enters verify queue.

6. tx_alice arrives at the node.
   - orphan_contains(H[0..10]): false
   - verify_queue_contains(H[0..10]): TRUE → REJECTED: Duplicated(H)

7. tx_attack fails script verification (invalid witness), evicted from queue.

8. Attacker repeats step 4–7 indefinitely.
   Alice's transaction never confirms.
```

The existing integration test `TransactionHashCollisionDifferentWitnessHashes` already demonstrates that step 6 produces `PoolRejectedDuplicatedTransaction`, confirming the mechanism is reachable: [8](#0-7)

### Citations

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
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

**File:** util/gen-types/src/extension/shortcut.rs (L29-33)
```rust
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }
```

**File:** test/src/specs/tx_pool/collision.rs (L14-37)
```rust
pub struct TransactionHashCollisionDifferentWitnessHashes;

impl Spec for TransactionHashCollisionDifferentWitnessHashes {
    // Case: `tx1` and `tx2` have the same tx_hash, but different witness_hash.
    fn run(&self, nodes: &mut Vec<Node>) {
        let node = &nodes[0];
        let window = node.consensus().tx_proposal_window();
        let start_issue = window.farthest() + 2;
        node.mine(start_issue.saturating_sub(node.get_tip_block_number()));

        let (tx1, tx2) = cousin_txs_with_same_hash_different_witness_hash(node);

        // Prepare Phase: Send both `tx1` and `tx2` into pool
        node.submit_transaction(&tx1);
        let result = node.rpc_client().send_transaction_result(tx2.data().into());

        assert!(
            result
                .err()
                .unwrap()
                .to_string()
                .contains("PoolRejectedDuplicatedTransaction")
        );
    }
```

**File:** test/src/specs/tx_pool/collision.rs (L214-223)
```rust
fn cousin_txs_with_same_hash_different_witness_hash(
    node: &Node,
) -> (TransactionView, TransactionView) {
    let tx1 = node.new_transaction_spend_tip_cellbase();
    let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
    assert_eq!(tx1.hash(), tx2.hash());
    assert_eq!(tx1.proposal_short_id(), tx2.proposal_short_id());
    assert_ne!(tx1.witness_hash(), tx2.witness_hash());

    (tx1, tx2)
```

**File:** tx-pool/src/component/orphan.rs (L134-158)
```rust
    pub fn add_orphan_tx(
        &mut self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) -> Vec<Byte32> {
        if self.entries.contains_key(&tx.proposal_short_id()) {
            return vec![];
        }

        debug!("add_orphan_tx {}", tx.hash());
        self.entries.insert(
            tx.proposal_short_id(),
            Entry::new(tx.clone(), peer, declared_cycle),
        );

        for out_point in tx.input_pts_iter() {
            self.by_out_point
                .entry(out_point)
                .or_default()
                .insert(tx.proposal_short_id());
        }

        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

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

**File:** tx-pool/src/process.rs (L401-426)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
```
