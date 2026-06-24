Audit Report

## Title
Witness-Stripping Front-Run Enables Persistent Transaction DoS via `proposal_short_id` Deduplication — (`File: tx-pool/src/util.rs`, `tx-pool/src/component/verify_queue.rs`)

## Summary
The CKB tx-pool and verify queue both deduplicate transactions using `ProposalShortId`, which is derived from `tx_hash` and excludes witnesses. An unprivileged attacker who observes a pending transaction in the P2P relay network can strip its witnesses and submit the stripped version first, causing the victim node to reject the legitimate transaction as `PoolRejectedDuplicatedTransaction`. The attack is repeatable with negligible cost, creating a persistent targeted DoS against any specific transaction on any reachable node.

## Finding Description

**Root cause — verify queue keyed on `ProposalShortId`:**

`VerifyQueue` in `tx-pool/src/component/verify_queue.rs` declares its unique index on `ProposalShortId`:

```rust
#[multi_index(hashed_unique)]
id: ProposalShortId,
``` [1](#0-0) 

`add_tx` inserts using `tx.proposal_short_id()` as the key: [2](#0-1) 

`contains_key` checks by `ProposalShortId`: [3](#0-2) 

**Blocking path in `resumeble_process_tx`:**

Before a transaction can enter the verify queue, `resumeble_process_tx` calls `verify_queue_contains`, which checks by `proposal_short_id`. If the stripped-witness tx is already in the queue, the legitimate tx is immediately rejected:

```rust
if self.verify_queue_contains(&tx).await {
    return Err(Reject::Duplicated(tx.hash()));
}
``` [4](#0-3) 

**Secondary block in `check_txid_collision`:**

Even if the stripped tx has already been processed into the pool, `check_txid_collision` in `pre_check` also deduplicates by `proposal_short_id`: [5](#0-4) 

The comment directly above this call (`"Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc."`) is factually incorrect — two transactions with identical `RawTransaction` content but different witnesses share the same `tx_hash` and `proposal_short_id`. [6](#0-5) 

**Stripped-witness tx passes `non_contextual_verify`:**

`NonContextualTransactionVerifier` checks version, size, empty inputs/outputs, duplicate deps, outputs data, and script hash types — it does **not** check witnesses: [7](#0-6) 

A transaction with `witnesses = []` passes all non-contextual checks and enters the verify queue normally.

**Confirmed by existing integration test:**

The test `cousin_txs_with_same_hash_different_witness_hash` explicitly constructs two transactions with the same `tx_hash` but different `witness_hash` and confirms the second is rejected as `PoolRejectedDuplicatedTransaction`: [8](#0-7) [9](#0-8) 

**Exploit flow:**
1. Attacker (a P2P relay peer) observes `tx_user` with valid secp256k1 witness in the relay stream.
2. Attacker constructs `tx_attacker = tx_user` with `witnesses = []`. `tx_attacker.hash() == tx_user.hash()`, `tx_attacker.proposal_short_id() == tx_user.proposal_short_id()`.
3. Attacker submits `tx_attacker` to the victim node. It passes `non_contextual_verify` and enters the verify queue under the shared `ProposalShortId`.
4. User submits `tx_user`. `verify_queue_contains` returns `true` → `Err(Reject::Duplicated(...))`.
5. `tx_attacker` fails script verification (secp256k1 rejects empty witness) and is evicted.
6. Attacker immediately repeats step 2–3. The user's transaction is permanently blocked on this node.

## Impact Explanation

This is a **persistent, targeted transaction DoS** with negligible attacker cost. The user's cells are effectively frozen on the targeted node for as long as the attacker repeats the front-run. For time-sensitive transactions (e.g., DAO withdrawal windows, `since`-locked transactions near expiry), this can cause permanent loss of a time window. If the attacker is a peer of multiple nodes simultaneously, the DoS extends across the user's reachable network. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the attack requires no fee payment, no key material, and no hashpower, and is trivially repeatable.

## Likelihood Explanation

- Any P2P relay peer connected to the victim node can observe relayed transactions before pool admission.
- Witness stripping requires only reading the transaction structure — no cryptographic capability needed.
- The attacker only needs to submit to the same node faster than the legitimate user, trivially achievable for a co-located or well-connected peer.
- No fee is paid for rejected transactions, making the attack free to repeat indefinitely.
- The existing test suite confirms the behavior is deterministic and reproducible.

## Recommendation

1. **Verify queue**: Change the unique index key from `ProposalShortId` to `witness_hash` (or a composite of `tx_hash` + `witness_hash`), so a stripped-witness variant does not occupy the slot for the legitimate transaction.
2. **`check_txid_collision`**: When a tx with the same `tx_hash` but a different `witness_hash` is already in the pool, apply a replacement policy (prefer the version with a non-empty/valid witness) rather than unconditionally rejecting the newcomer.
3. **Comment correction**: Fix the misleading comment in `pre_check` at `process.rs` L280 that incorrectly claims `tx_hash` covers witnesses.

## Proof of Concept

The existing integration test at `test/src/specs/tx_pool/collision.rs` already proves the deduplication behavior. To prove the attack direction (stripped version submitted first):

```rust
let tx_user = node.new_transaction_spend_tip_cellbase(); // has valid witness
let tx_attacker = tx_user.as_advanced_builder().witness(Bytes::default()).build();
assert_eq!(tx_user.hash(), tx_attacker.hash());

// Attacker submits stripped version first
node.submit_transaction(&tx_attacker); // enters verify queue

// User's legitimate tx is rejected
let result = node.rpc_client().send_transaction_result(tx_user.data().into());
assert!(result.err().unwrap().to_string().contains("PoolRejectedDuplicatedTransaction"));

// tx_attacker fails script verification and is evicted; attacker repeats
```

This is a direct inversion of the existing `TransactionHashCollisionDifferentWitnessHashes` test and requires no additional infrastructure to reproduce. [10](#0-9)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L37-38)
```rust
    #[multi_index(hashed_unique)]
    id: ProposalShortId,
```

**File:** tx-pool/src/component/verify_queue.rs (L109-111)
```rust
    pub fn contains_key(&self, id: &ProposalShortId) -> bool {
        self.inner.get_by_id(id).is_some()
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L204-228)
```rust
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
```

**File:** tx-pool/src/process.rs (L280-282)
```rust
                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;
```

**File:** tx-pool/src/process.rs (L349-351)
```rust
        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
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

**File:** verification/src/transaction_verifier.rs (L94-102)
```rust
    pub fn verify(&self) -> Result<(), Error> {
        self.version.verify()?;
        self.size.verify()?;
        self.empty.verify()?;
        self.duplicate_deps.verify()?;
        self.outputs_data_verifier.verify()?;
        self.script_hash_type.verify()?;
        Ok(())
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
