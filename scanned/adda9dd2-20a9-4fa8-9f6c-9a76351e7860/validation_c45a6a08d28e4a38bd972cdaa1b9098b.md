### Title
Tx-Pool Front-Running DoS via Witness Manipulation Exploiting `proposal_short_id` Uniqueness Key - (File: `tx-pool/src/util.rs`)

---

### Summary

CKB's tx-pool uses `proposal_short_id` (10 bytes derived from `tx_hash`, which **excludes witnesses**) as the uniqueness key for duplicate detection. Because `non_contextual_verify` does not execute scripts or validate witness content, an attacker who observes a victim's pending transaction can submit a version with the same inputs/outputs but invalid witnesses. This occupies the `tx_hash` slot in the pool, causing the legitimate transaction to be rejected as `PoolRejectedDuplicatedTransaction` across nodes that receive the attacker's version first.

---

### Finding Description

In `tx-pool/src/util.rs`, `check_txid_collision` checks for duplicates using `proposal_short_id`:

```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
``` [1](#0-0) 

`proposal_short_id` is derived from `tx_hash`, which in CKB is the hash of the transaction **without witnesses**. Two transactions with identical inputs, outputs, and cell_deps but different witnesses share the same `tx_hash` and `proposal_short_id`. This is confirmed by the existing test:

```rust
assert_eq!(tx1.hash(), tx2.hash());
assert_eq!(tx1.proposal_short_id(), tx2.proposal_short_id());
assert_ne!(tx1.witness_hash(), tx2.witness_hash());
``` [2](#0-1) 

In both the RPC path (`process_tx`) and the P2P path (`resumeble_process_tx`), the duplicate check against `verify_queue_contains` and `orphan_contains` uses `tx_hash` before any script execution occurs:

```rust
if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
    return Err(Reject::Duplicated(tx.hash()));
}
``` [3](#0-2) 

The `non_contextual_verify` step that precedes these checks does **not** execute scripts and does not validate witness content — only structural properties. A transaction with garbage witnesses passes this gate and enters the verify queue, occupying the `tx_hash` slot. [4](#0-3) 

The `submit_entry` function, which runs under a write lock, calls `_submit_entry` → `add_pending` → `pool_map.add_entry`, which silently returns `Ok((false, evicts))` if the `proposal_short_id` already exists — no error is surfaced to the caller at this stage:

```rust
if self.entries.get_by_id(&tx_short_id).is_some() {
    return Ok((false, evicts));
}
``` [5](#0-4) 

---

### Impact Explanation

An attacker who observes a victim's transaction propagating over the P2P relay can craft a modified version with the same inputs/outputs but invalid witnesses (same `tx_hash`, different `witness_hash`). Nodes that receive the attacker's version first will:

1. Accept it (passes `non_contextual_verify`, enters verify queue).
2. Reject the victim's original as `PoolRejectedDuplicatedTransaction`.
3. Eventually remove the attacker's version after script verification fails.

The attacker can repeat this indefinitely, preventing the victim's transaction from propagating to miners across the network. The victim's originating node retains the valid transaction, but wide propagation is disrupted, delaying or preventing confirmation.

**Impact: Medium** — Transaction-level DoS; victim's transaction is not permanently lost but confirmation is delayed or blocked across much of the network.

---

### Likelihood Explanation

The attacker must be a P2P peer of nodes that have not yet received the victim's transaction, and must rebroadcast the modified version before the original arrives. This is achievable by any unprivileged peer connected to the CKB P2P network. No special privileges, keys, or majority hashpower are required. The `tx_hash` (the "predictable ID") is fully visible from the transaction's inputs and outputs, which are broadcast in plaintext.

**Likelihood: Medium** — Requires P2P connectivity and a race condition, but no cryptographic capability.

---

### Recommendation

Use `witness_hash` (which commits to witnesses) as the uniqueness key in the tx-pool instead of `tx_hash`/`proposal_short_id`, or perform a lightweight witness well-formedness check before a transaction is allowed to occupy a `tx_hash` slot in the verify queue. This prevents a transaction with garbage witnesses from blocking the legitimate transaction that shares the same `tx_hash`.

---

### Proof of Concept

1. Victim constructs transaction `T` with valid witnesses and submits to node A via RPC.
2. Node A broadcasts `T` to P2P peers.
3. Attacker (peer of node B) receives `T` via relay.
4. Attacker constructs `T'`: same inputs/outputs as `T`, witnesses replaced with arbitrary bytes. `T'.tx_hash == T.tx_hash`, `T'.witness_hash != T.witness_hash`.
5. Attacker submits `T'` to node B (via P2P or RPC) before `T` arrives.
6. Node B: `non_contextual_verify(T')` passes → `T'` enters verify queue.
7. Node B receives `T` from node A → `verify_queue_contains(T)` returns true (same `tx_hash`) → `Reject::Duplicated`.
8. Node B's async verifier executes `T'`'s scripts → fails → `T'` removed from pool.
9. Attacker repeats steps 4–8 for every node in the network, preventing `T` from reaching miners.

The `TransactionHashCollisionDifferentWitnessHashes` integration test in `test/src/specs/tx_pool/collision.rs` already demonstrates step 7 as reproducible behavior:

```rust
node.submit_transaction(&tx1);
let result = node.rpc_client().send_transaction_result(tx2.data().into());
assert!(result.err().unwrap().to_string().contains("PoolRejectedDuplicatedTransaction"));
``` [6](#0-5)

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

**File:** test/src/specs/tx_pool/collision.rs (L26-37)
```rust
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

**File:** tx-pool/src/process.rs (L318-333)
```rust
    pub(crate) async fn non_contextual_verify(
        &self,
        tx: &TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<(), Reject> {
        if let Err(reject) = non_contextual_verify(&self.consensus, tx) {
            if reject.is_malformed_tx()
                && let Some(remote) = remote
            {
                self.ban_malformed(remote.1, format!("reject {reject}"))
                    .await;
            }
            return Err(reject);
        }
        Ok(())
    }
```

**File:** tx-pool/src/process.rs (L409-411)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
```

**File:** tx-pool/src/component/pool_map.rs (L207-209)
```rust
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
```
