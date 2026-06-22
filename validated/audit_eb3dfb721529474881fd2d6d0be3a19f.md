### Title
Witness-Stripping Front-Run Enables Persistent Transaction DoS via `proposal_short_id` Deduplication — (`File: tx-pool/src/util.rs`)

---

### Summary

The CKB tx-pool deduplicates transactions using `proposal_short_id`, which is derived from `tx_hash`. Because `tx_hash` covers only the `RawTransaction` (inputs, outputs, cell_deps, etc.) and **excludes witnesses**, an unprivileged attacker who observes a pending transaction in the P2P relay network can strip its witnesses and submit the stripped version first. The pool then rejects the legitimate user's transaction as `PoolRejectedDuplicatedTransaction`. The attacker can repeat this indefinitely, creating a persistent, targeted DoS against any specific transaction.

---

### Finding Description

In CKB, a transaction has two distinct hashes:

- `tx_hash` = hash of `RawTransaction` (no witnesses) — used as the canonical transaction identifier and for `proposal_short_id`
- `witness_hash` = hash of the full `Transaction` (raw + witnesses) — used for the witness Merkle root in the block header

The tx-pool deduplication function `check_txid_collision` in `tx-pool/src/util.rs` checks only `proposal_short_id` (the first 10 bytes of `tx_hash`):

```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

This function is called in `pre_check` before script verification:

```rust
check_txid_collision(tx_pool, tx)?;
```

The misleading comment directly above this call reads: *"Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc."* — but this is factually incorrect. Two transactions with identical inputs/outputs but different witnesses share the same `tx_hash` and `proposal_short_id`, yet carry entirely different authorization proofs (witnesses).

The integration test `test/src/specs/tx_pool/collision.rs` explicitly demonstrates this:

```rust
fn cousin_txs_with_same_hash_different_witness_hash(node: &Node) -> (TransactionView, TransactionView) {
    let tx1 = node.new_transaction_spend_tip_cellbase();
    let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
    assert_eq!(tx1.hash(), tx2.hash());
    assert_ne!(tx1.witness_hash(), tx2.witness_hash());
    (tx1, tx2)
}
```

When `tx1` is in the pool and `tx2` (stripped witnesses) is submitted, the result is `PoolRejectedDuplicatedTransaction`. The attack inverts this: the attacker submits the stripped version **first**, so the legitimate transaction is the one rejected.

Additionally, `resumeble_process_tx` checks `verify_queue_contains` before enqueuing, which also uses `tx.hash()` as the key, meaning the block occurs even before the tx reaches the pool — at the verify queue stage.

---

### Impact Explanation

An attacker who is a P2P relay peer of the victim node (or who can submit via RPC) can:

1. Observe a user's pending transaction (with valid witnesses/signatures) in the relay network.
2. Strip all witnesses and submit the stripped transaction to the victim node first.
3. The stripped transaction enters the verify queue and then fails script verification (for any real lock script that validates witnesses).
4. During the verification window, the user's legitimate transaction is rejected as `PoolRejectedDuplicatedTransaction`.
5. After the stripped tx is evicted, the user resubmits — but the attacker repeats step 2 immediately.

This creates a **persistent, targeted DoS** against any specific transaction. The user's cells are effectively frozen: they cannot be spent as long as the attacker keeps front-running. For time-sensitive transactions (e.g., those with `since` locks near expiry, or DAO withdrawal transactions), this can cause permanent loss of a time window.

In the degenerate case where a lock script does not validate witnesses (e.g., an always-success script or a misconfigured script), the stripped-witness transaction would pass script verification and be committed to the chain, spending the user's cells without their authorization.

---

### Likelihood Explanation

- Any P2P relay peer connected to the victim node can observe relayed transactions before they are admitted to the pool.
- The attack requires no special privileges, no key material, and no majority hashpower.
- The attacker only needs to be faster than the legitimate user at submitting to the same node — trivially achievable for a co-located or well-connected peer.
- The attack is repeatable with negligible cost (no fee is paid for rejected transactions).

---

### Recommendation

The pool and verify queue should deduplicate by `witness_hash` rather than (or in addition to) `proposal_short_id`. Specifically:

1. `check_txid_collision` should check whether a transaction with the same `witness_hash` is already present. If a tx with the same `tx_hash` but a **different** `witness_hash` is already in the pool, the node should either reject the new one or apply a replacement policy (similar to RBF) that prefers the version with valid witnesses.
2. The verify queue's `verify_queue_contains` check should similarly use `witness_hash` as the deduplication key, so that a stripped-witness version does not block the legitimate version from entering the queue.
3. Correct the misleading comment in `pre_check` that claims `tx_hash` covers witnesses.

---

### Proof of Concept

1. User constructs `tx_user` spending a cell, with a valid secp256k1 witness (signature).
2. Attacker observes `tx_user` in the P2P relay stream.
3. Attacker constructs `tx_attacker = tx_user` with `witnesses = []` (stripped). Note: `tx_attacker.hash() == tx_user.hash()`, `tx_attacker.proposal_short_id() == tx_user.proposal_short_id()`, but `tx_attacker.witness_hash() != tx_user.witness_hash()`.
4. Attacker submits `tx_attacker` to the victim node via RPC or relay. It enters the verify queue.
5. User submits `tx_user`. The node calls `verify_queue_contains(&tx_user)` → returns `true` (same `tx_hash`) → returns `Err(Reject::Duplicated(...))`.
6. `tx_attacker` fails script verification (secp256k1 lock rejects empty witness) and is evicted.
7. Attacker immediately repeats step 3–4. User's transaction is permanently blocked.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
        (ret, snapshot)
    }
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

**File:** util/gen-types/src/extension/calc_hash.rs (L135-152)
```rust
impl<'r> packed::TransactionReader<'r> {
    /// Calls [`RawTransactionReader.calc_tx_hash()`] for [`self.raw()`].
    ///
    /// [`RawTransactionReader.calc_tx_hash()`]: struct.RawTransactionReader.html#method.calc_tx_hash
    /// [`self.raw()`]: #method.raw
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.raw().calc_tx_hash()
    }

    /// Calculates the hash for [self.as_slice()] as the witness hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_witness_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(Transaction, calc_tx_hash);
impl_calc_special_hash_for_entity!(Transaction, calc_witness_hash);
```
