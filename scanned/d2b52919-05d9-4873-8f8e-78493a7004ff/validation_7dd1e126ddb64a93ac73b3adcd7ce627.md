Audit Report

## Title
Witness-Stripping Front-Run Enables Persistent Transaction DoS via `proposal_short_id` Deduplication — (`File: tx-pool/src/util.rs`)

## Summary
The CKB tx-pool and verify queue deduplicate transactions using `proposal_short_id`, which is derived from `tx_hash` and excludes witnesses. An attacker who observes a pending transaction in the P2P relay network can strip its witnesses and submit the stripped version to a victim node first, causing the legitimate transaction to be rejected as `PoolRejectedDuplicatedTransaction`. The attacker can repeat this indefinitely at negligible cost, creating a persistent targeted DoS against any specific transaction.

## Finding Description

**Root cause:** `check_txid_collision` in `tx-pool/src/util.rs` uses only `proposal_short_id` (first 10 bytes of `tx_hash`) for deduplication:

```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
``` [1](#0-0) 

`tx_hash` is computed over `RawTransaction` only and explicitly excludes witnesses. Two transactions with identical inputs/outputs but different witnesses share the same `tx_hash` and `proposal_short_id`.

The verify queue uses the same key. `verify_queue_contains` calls `queue.contains_key(&tx.proposal_short_id())`: [2](#0-1) 

`VerifyEntry` uses `ProposalShortId` as its `hashed_unique` index: [3](#0-2) 

`resumeble_process_tx` checks the verify queue before enqueuing, so the block occurs before the tx even reaches the pool: [4](#0-3) 

The comment at line 280 of `process.rs` directly above `check_txid_collision` states *"Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc."* — this is factually incorrect. [5](#0-4) 

**Exploit flow:**
1. User submits `tx_user` (with valid secp256k1 witness) to node A.
2. Node A relays `tx_hash` to peers; attacker requests and receives the full transaction.
3. Attacker constructs `tx_attacker` = same `RawTransaction`, `witnesses = []`. `tx_attacker.hash() == tx_user.hash()`, `tx_attacker.proposal_short_id() == tx_user.proposal_short_id()`.
4. Attacker submits `tx_attacker` to victim node B via RPC or relay. It enters the verify queue keyed by `proposal_short_id`.
5. `tx_user` arrives at victim node B via normal relay. `verify_queue_contains` returns `true` → `Err(Reject::Duplicated(...))`.
6. `tx_attacker` fails script verification (secp256k1 rejects empty witness) and is evicted.
7. Attacker immediately repeats step 3–4. The user's transaction is permanently blocked.

The integration test `cousin_txs_with_same_hash_different_witness_hash` explicitly confirms this behavior: [6](#0-5) [7](#0-6) 

`NonContextualTransactionVerifier` does not check witnesses at all — it only checks version, size, empty inputs/outputs, duplicate deps, outputs data, and script hash type — so the stripped transaction passes the non-contextual check and enters the verify queue: [8](#0-7) 

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

An attacker can block any specific transaction from ever being confirmed with zero fee cost (rejected transactions pay no fees). Applied systematically against many transactions, this degrades the effective throughput of the network. For time-sensitive transactions (DAO withdrawals, `since`-locked cells near expiry), this can cause permanent, irreversible loss of a time window. In the degenerate case of a lock script that does not validate witnesses (e.g., always-success), the stripped transaction passes script verification and is committed to the chain, spending the user's cells without authorization.

## Likelihood Explanation

- Any P2P relay peer of the victim node can observe relayed transactions (full transaction data including witnesses is sent via `RelayTransactions`).
- The attack requires no key material, no special privileges, and no majority hashpower.
- The attacker only needs to submit to the victim node faster than normal relay propagation — trivially achievable for a co-located or well-connected peer.
- The attack is repeatable with negligible cost and no rate limiting on rejected transactions.

## Recommendation

1. `check_txid_collision` should additionally check `witness_hash`: if a transaction with the same `tx_hash` but a different `witness_hash` is already in the pool, apply a replacement policy that prefers the version with valid witnesses (or reject the stripped version outright).
2. `verify_queue_contains` and `VerifyQueue::add_tx` should use `witness_hash` as the deduplication key, so a stripped-witness version cannot block the legitimate version from entering the queue.
3. Correct the misleading comment at `process.rs:280` that claims `tx_hash` covers witnesses.

## Proof of Concept

The existing integration test already demonstrates the core primitive:

```rust
fn cousin_txs_with_same_hash_different_witness_hash(node: &Node) -> (TransactionView, TransactionView) {
    let tx1 = node.new_transaction_spend_tip_cellbase();
    let tx2 = tx1.as_advanced_builder().witness(Bytes::default()).build();
    assert_eq!(tx1.hash(), tx2.hash());
    assert_ne!(tx1.witness_hash(), tx2.witness_hash());
    (tx1, tx2)
}
```

To reproduce the attack direction: submit `tx2` (stripped) first, then submit `tx1` (legitimate). `tx1` will be rejected with `PoolRejectedDuplicatedTransaction`. After `tx2` is evicted due to script verification failure, resubmit `tx2` before `tx1` — the block repeats indefinitely. [6](#0-5)

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

**File:** tx-pool/src/component/verify_queue.rs (L34-53)
```rust
#[derive(MultiIndexMap, Clone)]
struct VerifyEntry {
    /// The transaction id
    #[multi_index(hashed_unique)]
    id: ProposalShortId,
    /// The unix timestamp when entering the Txpool, unit: Millisecond
    /// This field is used to sort the txs in the queue
    /// We may add more other sort keys in the future
    #[multi_index(ordered_non_unique)]
    added_time: u64,

    /// whether the tx is a large cycle tx
    #[multi_index(hashed_non_unique)]
    is_large_cycle: bool,
    /// whether the tx is a proposal tx
    is_proposal_tx: bool,

    /// other sort key
    inner: Entry,
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

**File:** test/src/specs/tx_pool/collision.rs (L214-224)
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
