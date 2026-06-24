All cited code references check out against the actual codebase. Let me verify the key claims:

All code references in the report are accurate and verified against the actual codebase. Here is the validated output:

---

Audit Report

## Title
Witness-Stripping Front-Run Enables Persistent Transaction DoS via `proposal_short_id` Deduplication — (`File: tx-pool/src/component/verify_queue.rs`, `tx-pool/src/util.rs`)

## Summary
`VerifyQueue` and `check_txid_collision` both deduplicate transactions using `ProposalShortId`, which is derived solely from `tx_hash` and excludes witnesses. An unprivileged P2P relay peer can strip witnesses from an observed transaction and submit the stripped version first, causing the victim node to permanently reject the legitimate transaction as `PoolRejectedDuplicatedTransaction`. The attack is free, requires no cryptographic capability, and is trivially repeatable.

## Finding Description

**Root cause — `VerifyQueue` keyed on `ProposalShortId`:**

`VerifyEntry` declares its unique index on `ProposalShortId`: [1](#0-0) 

`add_tx` inserts and deduplicates using `tx.proposal_short_id()`: [2](#0-1) 

`contains_key` checks by `ProposalShortId`: [3](#0-2) 

**Primary blocking path in `resumeble_process_tx`:**

Before a transaction can enter the verify queue, `resumeble_process_tx` calls `verify_queue_contains`, which checks by `proposal_short_id`. If the stripped-witness tx already occupies the slot, the legitimate tx is immediately rejected: [4](#0-3) 

`verify_queue_contains` itself delegates to `contains_key(&tx.proposal_short_id())`: [5](#0-4) 

**Secondary block in `check_txid_collision`:**

Even after the verify queue is cleared, `pre_check` calls `check_txid_collision`, which also deduplicates by `proposal_short_id`: [6](#0-5) 

The comment directly above this call is factually incorrect — two transactions with identical `RawTransaction` content but different witnesses share the same `tx_hash` and `proposal_short_id`: [7](#0-6) 

**Stripped-witness tx passes `non_contextual_verify`:**

`NonContextualTransactionVerifier.verify()` checks version, size, empty inputs/outputs, duplicate deps, outputs data, and script hash types — it does **not** check witnesses: [8](#0-7) 

A transaction with `witnesses = []` passes all non-contextual checks and enters the verify queue normally.

**Exploit flow:**
1. Attacker (a P2P relay peer) observes `tx_user` with a valid secp256k1 witness in the relay stream.
2. Attacker constructs `tx_attacker = tx_user` with `witnesses = []`. `tx_attacker.hash() == tx_user.hash()`, `tx_attacker.proposal_short_id() == tx_user.proposal_short_id()`.
3. Attacker submits `tx_attacker` to the victim node. It passes `non_contextual_verify` and enters the verify queue under the shared `ProposalShortId`.
4. User submits `tx_user`. `verify_queue_contains` returns `true` → `Err(Reject::Duplicated(...))`. User's tx is rejected.
5. `tx_attacker` fails script verification (secp256k1 rejects empty witness) and is evicted from the queue.
6. Attacker immediately repeats steps 2–3. The user's transaction is permanently blocked on this node.

**Confirmed by existing integration test:**

The test `TransactionHashCollisionDifferentWitnessHashes` explicitly constructs two transactions with the same `tx_hash` but different `witness_hash` and confirms the second is rejected as `PoolRejectedDuplicatedTransaction`: [9](#0-8) 

The helper function confirms `tx_hash` and `proposal_short_id` are identical while `witness_hash` differs: [10](#0-9) 

## Impact Explanation

This is a **persistent, targeted transaction DoS** with negligible attacker cost, matching the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. The user's cells are effectively frozen on the targeted node for as long as the attacker repeats the front-run. For time-sensitive transactions (e.g., DAO withdrawal windows, `since`-locked transactions near expiry), this can cause permanent loss of a time window. If the attacker peers with multiple nodes simultaneously, the DoS extends across the user's reachable network. No fee is paid for rejected transactions, making the attack free to repeat indefinitely.

## Likelihood Explanation

- Any P2P relay peer connected to the victim node can observe relayed transactions before pool admission.
- Witness stripping requires only reading the transaction structure — no cryptographic capability needed.
- The attacker only needs to submit to the same node faster than the legitimate user, trivially achievable for a co-located or well-connected peer that observes the transaction in the relay stream.
- No fee is paid for rejected transactions, making the attack free to repeat indefinitely.
- The existing test suite confirms the behavior is deterministic and reproducible.

## Recommendation

1. **Verify queue**: Change the unique index key from `ProposalShortId` to `witness_hash` (or a composite of `tx_hash` + `witness_hash`) in `VerifyEntry`, so a stripped-witness variant does not occupy the slot for the legitimate transaction.
2. **`check_txid_collision`**: When a tx with the same `tx_hash` but a different `witness_hash` is already in the pool, apply a replacement policy (prefer the version with a non-empty/valid witness) rather than unconditionally rejecting the newcomer.
3. **Comment correction**: Fix the misleading comment in `process.rs` at L280 that incorrectly claims `tx_hash` covers witnesses.

## Proof of Concept

The existing integration test at `test/src/specs/tx_pool/collision.rs` already proves the deduplication behavior. To prove the attack direction (stripped version submitted first), invert the existing test:

```rust
let tx_user = node.new_transaction_spend_tip_cellbase(); // has valid witness
let tx_attacker = tx_user.as_advanced_builder().witness(Bytes::default()).build();
assert_eq!(tx_user.hash(), tx_attacker.hash());
assert_eq!(tx_user.proposal_short_id(), tx_attacker.proposal_short_id());

// Attacker submits stripped version first
node.submit_transaction(&tx_attacker); // enters verify queue under shared ProposalShortId

// User's legitimate tx is rejected
let result = node.rpc_client().send_transaction_result(tx_user.data().into());
assert!(result.err().unwrap().to_string().contains("PoolRejectedDuplicatedTransaction"));

// tx_attacker fails script verification and is evicted; attacker repeats indefinitely
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

**File:** tx-pool/src/component/verify_queue.rs (L204-209)
```rust
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
```

**File:** tx-pool/src/process.rs (L237-240)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }
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
