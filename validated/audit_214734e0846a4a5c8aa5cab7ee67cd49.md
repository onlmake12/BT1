### Title
Witness-Stripped Transaction Front-Running Allows Permanent Tx-Pool DoS — (`tx-pool/src/util.rs`, `tx-pool/src/process.rs`)

---

### Summary

CKB's transaction pool uses `ProposalShortId` — the first 10 bytes of `tx_hash` — as the unique pool identifier. Because `tx_hash` is computed over `RawTransaction` only (inputs, outputs, cell\_deps, header\_deps, version) and **excludes witnesses**, any unprivileged attacker who observes a victim's pending transaction can craft a structurally identical transaction with garbage witnesses, submit it first, and cause the victim's valid transaction to be permanently rejected as `PoolRejectedDuplicatedTransaction`. The attacker pays no fee and can repeat the attack indefinitely.

---

### Finding Description

**Root cause — `tx_hash` excludes witnesses**

`calc_tx_hash` hashes only `RawTransaction`: [1](#0-0) 

`ProposalShortId` is the first 10 bytes of that hash: [2](#0-1) 

Two transactions that share the same inputs/outputs/cell\_deps but carry different witnesses therefore produce the **same** `tx_hash` and the **same** `ProposalShortId`. The codebase itself documents this: [3](#0-2) 

**Root cause — pool uniqueness is keyed on `ProposalShortId`**

Every admission gate in the tx-pool checks by `proposal_short_id`, not by `witness_hash`:

`check_txid_collision` (main pool): [4](#0-3) 

`verify_queue_contains` (async verify queue): [5](#0-4) 

`orphan_contains` (orphan pool): [6](#0-5) 

All three gates are checked before script execution in `process_tx`: [7](#0-6) 

And in `resumeble_process_tx` (P2P relay path): [8](#0-7) 

**The misleading comment**

The comment at line 280 of `process.rs` states *"Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc."* — this is factually incorrect. Same `proposal_short_id` means same `RawTransaction` content; witnesses are not covered. [9](#0-8) 

---

### Impact Explanation

An attacker who observes victim transaction `T` (inputs `I`, outputs `O`, valid witness `W_victim`) over the P2P relay can:

1. Construct `T'` with the same `I` and `O` but garbage witnesses `W_garbage`.
2. Submit `T'` via `send_transaction` RPC or P2P relay to the target node.
3. `T'` passes `non_contextual_verify` (witnesses are not script-checked there) and enters the verify queue under the same `ProposalShortId`.
4. When `T` arrives, every admission check (`verify_queue_contains`, `check_txid_collision`) returns a collision → `T` is rejected with `PoolRejectedDuplicatedTransaction`.
5. `T'` eventually fails contextual script verification and is evicted.
6. Attacker immediately resubmits `T'`. The cycle repeats at zero cost.

The victim's valid transaction is permanently blocked from entering any pool the attacker can reach. For time-sensitive operations (DAO withdrawals, since-locked cells, oracle updates) this is a critical liveness failure.

---

### Likelihood Explanation

- **Entry path**: any unprivileged caller of `send_transaction` RPC or any P2P peer relaying transactions.
- **Cost**: zero — `T'` fails verification and is evicted without consuming any on-chain resource or fee.
- **Information required**: only the public transaction content (`inputs`, `outputs`, `cell_deps`), which is broadcast in plaintext over P2P.
- **Timing**: the attacker must submit `T'` before `T` is accepted into the pool. As a direct P2P peer of the target node, or by monitoring the relay network, this is straightforward.
- **Repeatability**: unlimited; each eviction of `T'` opens a new window for resubmission.

---

### Recommendation

1. **Key the pool on `witness_hash` instead of (or in addition to) `proposal_short_id`** for the duplicate-rejection check. Two transactions with the same `tx_hash` but different `witness_hash` are distinct objects and should not collide.
2. **Alternatively**, before enqueuing into the verify queue, perform a lightweight witness-presence check (e.g., reject if witnesses are empty when inputs are non-null) to raise the cost of submitting garbage-witness transactions.
3. Fix the misleading comment at `tx-pool/src/process.rs:280` which incorrectly claims same `txid` implies same witnesses.

---

### Proof of Concept

The existing integration test already demonstrates the collision:

```rust
// test/src/specs/tx_pool/collision.rs
let tx1 = node.new_transaction_spend_tip_cellbase();
let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
assert_eq!(tx1.hash(), tx2.hash());          // same tx_hash → same ProposalShortId
assert_ne!(tx1.witness_hash(), tx2.witness_hash()); // different witnesses

node.submit_transaction(&tx1);
let result = node.rpc_client().send_transaction_result(tx2.data().into());
// → "PoolRejectedDuplicatedTransaction"
``` [10](#0-9) 

**Adversarial extension**: replace `tx1` with the attacker's garbage-witness version submitted *first*. `tx2` (the victim's valid transaction) is then rejected. The attacker loops: each time `tx1` is evicted after failing script verification, they resubmit it, permanently blocking `tx2`.

### Citations

**File:** util/gen-types/src/extension/calc_hash.rs (L125-133)
```rust
impl<'r> packed::RawTransactionReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the transaction hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawTransaction, calc_tx_hash);
```

**File:** util/gen-types/src/extension/shortcut.rs (L27-43)
```rust
impl packed::ProposalShortId {
    /// Creates a new `ProposalShortId` from a transaction hash.
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }

    /// Creates a new `ProposalShortId` whose bits are all zeros.
    pub fn zero() -> Self {
        Self::default()
    }

    /// Creates a new `ProposalShortId`.
    pub fn new(v: [u8; 10]) -> Self {
        v.into()
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

**File:** tx-pool/src/process.rs (L237-240)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }
```

**File:** tx-pool/src/process.rs (L242-245)
```rust
    pub(crate) async fn orphan_contains(&self, tx: &TransactionView) -> bool {
        let orphan = self.orphan.read().await;
        orphan.contains_key(&tx.proposal_short_id())
    }
```

**File:** tx-pool/src/process.rs (L280-283)
```rust
                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

```

**File:** tx-pool/src/process.rs (L335-352)
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
