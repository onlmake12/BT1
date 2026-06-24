Audit Report

## Title
Tx-Pool Admission Denial via ProposalShortId Slot Pre-Occupation — (`tx-pool/src/process.rs`, `tx-pool/src/component/verify_queue.rs`)

## Summary
`ProposalShortId` is derived solely from `tx_hash`, which is computed over `RawTransaction` and excludes witnesses. Two transactions sharing identical inputs, outputs, cell-deps, and header-deps but carrying different witnesses are indistinguishable by `ProposalShortId`. Because the `VerifyQueue` enforces uniqueness on `ProposalShortId`, an unprivileged P2P peer can submit a "cousin" transaction (same `tx_hash`, garbage witnesses) via the relay path, which passes `non_contextual_verify` and occupies the unique slot. Any subsequent submission of the legitimate transaction — including via the local RPC — is immediately rejected with `PoolRejectedDuplicatedTransaction (-1107)`.

## Finding Description

**Root cause — `tx_hash` excludes witnesses**

`ProposalShortId` is the first 10 bytes of `tx_hash`: [1](#0-0) 

`tx_hash` is computed over `RawTransaction` only, which structurally excludes the witnesses field: [2](#0-1) 

The integration test explicitly confirms two transactions with different witnesses share the same `tx_hash` and `proposal_short_id`: [3](#0-2) 

**`VerifyQueue` enforces uniqueness on `ProposalShortId`**

`VerifyEntry` declares `id: ProposalShortId` as `hashed_unique`, making it the sole deduplication key: [4](#0-3) 

`verify_queue_contains` and `orphan_contains` both key on `proposal_short_id()`: [5](#0-4) 

**P2P relay path enqueues without script/witness verification**

`submit_remote_tx` calls `resumeble_process_tx`, which runs only `non_contextual_verify` — checking version, size, cellbase, duplicate deps, but **not witness validity** — then immediately enqueues: [6](#0-5) 

`non_contextual_verify` delegates to `NonContextualTransactionVerifier` and size/cellbase checks only; no witness content check is performed: [7](#0-6) 

**Victim's transaction is rejected as Duplicated**

Both `process_tx` (RPC path) and `resumeble_process_tx` (relay path) check `verify_queue_contains` before proceeding. If the attacker's cousin already occupies the slot, the victim's transaction is immediately rejected: [8](#0-7) 

**Misleading comment compounds the issue**

The comment at line 280 states "Same txid means exactly the same transaction, including inputs, outputs, witnesses" — this is factually incorrect since witnesses are excluded from `tx_hash`: [9](#0-8) 

**Integration test confirms the rejection**

The existing test `TransactionHashCollisionDifferentWitnessHashes` directly demonstrates that the second cousin transaction is rejected with `PoolRejectedDuplicatedTransaction`: [10](#0-9) 

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating multiple low-cost Sybil P2P peers can continuously pre-occupy `ProposalShortId` slots for targeted transactions, preventing them from entering the pool. The `VerifyQueue` accepts up to 256 MB of transactions: [11](#0-10) 

By flooding the queue with cousin transactions (each with garbage witnesses but valid structure), the attacker can: (1) deny specific transactions from entering the pool, and (2) saturate the verify_queue worker, degrading pool throughput for all users. In time-sensitive scenarios involving `since`-locked transactions, this denial has direct financial consequences for victims.

## Likelihood Explanation

- Any connected P2P peer can send `RelayTransactions` messages; no privilege is required.
- The cousin transaction passes all `non_contextual_verify` checks (correct version, size within limits, not cellbase, no duplicate deps).
- The victim's transaction structure (inputs/outputs) is observable once broadcast to the P2P network, or predictable for known cell-spending patterns.
- A single attacker peer is eventually banned after the cousin fails script verification in the verify_queue worker. However, with multiple Sybil peers (low cost on a permissionless P2P network), the attack is sustainable indefinitely.
- The 256 MB verify_queue limit allows the attacker to queue many cousins, extending the denial window significantly beyond a single queue-drain cycle.

## Recommendation

1. **Key the verify_queue and orphan pool on `witness_hash` instead of `ProposalShortId`** for deduplication purposes. `witness_hash` covers the full serialized transaction including witnesses, making cousin transactions distinguishable at admission time.
2. **Alternatively, add a lightweight structural witness check** in `resumeble_process_tx` before enqueuing: verify that the witness count matches the input count (a cheap O(1) check), rejecting transactions with obviously mismatched or empty witnesses before they occupy a slot.
3. **Fix the misleading comment** at `tx-pool/src/process.rs:280` — `tx_hash` does not cover witnesses; only `witness_hash` does.

## Proof of Concept

The existing integration test already demonstrates the collision and rejection. The attack scenario (attacker submits cousin first, victim is denied) is the exact reverse ordering of the test at `test/src/specs/tx_pool/collision.rs:27-36`:

```rust
// Attacker submits cousin (garbage witness, same tx_hash) via P2P relay first
// → passes non_contextual_verify, occupies ProposalShortId slot in verify_queue
node.submit_transaction(&tx2); // tx2 has Bytes::default() witness, same hash as tx1

// Victim submits valid transaction via RPC
// → verify_queue_contains(&tx1) returns true (slot occupied by tx2)
// → immediately rejected
let result = node.rpc_client().send_transaction_result(tx1.data().into());
assert!(result.err().unwrap().to_string().contains("PoolRejectedDuplicatedTransaction"));
```

The cousin construction is confirmed at: [3](#0-2) 

The relay entry path `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` bypasses all script checks: [12](#0-11) [6](#0-5)

### Citations

**File:** util/gen-types/src/extension/shortcut.rs (L27-33)
```rust
impl packed::ProposalShortId {
    /// Creates a new `ProposalShortId` from a transaction hash.
    pub fn from_tx_hash(h: &packed::Byte32) -> Self {
        let mut inner = [0u8; 10];
        inner.copy_from_slice(&h.as_slice()[..10]);
        inner.into()
    }
```

**File:** util/gen-types/src/extension/calc_hash.rs (L135-142)
```rust
impl<'r> packed::TransactionReader<'r> {
    /// Calls [`RawTransactionReader.calc_tx_hash()`] for [`self.raw()`].
    ///
    /// [`RawTransactionReader.calc_tx_hash()`]: struct.RawTransactionReader.html#method.calc_tx_hash
    /// [`self.raw()`]: #method.raw
    pub fn calc_tx_hash(&self) -> packed::Byte32 {
        self.raw().calc_tx_hash()
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
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

**File:** tx-pool/src/process.rs (L237-245)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }

    pub(crate) async fn orphan_contains(&self, tx: &TransactionView) -> bool {
        let orphan = self.orphan.read().await;
        orphan.contains_key(&tx.proposal_short_id())
    }
```

**File:** tx-pool/src/process.rs (L279-283)
```rust

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

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

**File:** tx-pool/src/process.rs (L401-411)
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
