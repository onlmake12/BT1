All code claims check out against the actual source. Let me verify the `ORPHAN_TX_EXPIRE_TIME` constant and the `non_contextual_verify` path to confirm attack feasibility.

All code claims are confirmed by the actual source. The `NonContextualTransactionVerifier` does not check signatures — it only verifies version, size, non-empty inputs/outputs, duplicate deps, outputs data, and script hash type. This means an attacker can craft structurally valid transactions with random input `OutPoint`s without any keys, which will fail at `pre_check` → `resolve_tx` with a missing-input error and be routed directly into the orphan pool via `after_process`.

Audit Report

## Title
Orphan Transaction Pool Exhaustion via Unbounded Per-Peer Submissions — (File: `tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces a global cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` with no per-peer accounting. An unprivileged attacker can fill the entire pool with structurally valid but input-missing transactions (no keys or fees required), causing legitimate orphan transactions to be arbitrarily evicted. When an evicted orphan's parent later arrives, `process_orphan_tx()` finds no matching entry and silently skips promotion, breaking the orphan-resolution flow for honest users.

## Finding Description
`OrphanPool` is defined with only two fields and no per-peer counter:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
``` [1](#0-0) 

`add_orphan_tx()` accepts any transaction from any peer and stores the `peer` only in the `Entry` struct for record-keeping, never for quota enforcement: [2](#0-1) 

When the pool is full, `limit_size()` first expires timed-out entries, then evicts via `HashMap::keys().next()` — effectively arbitrary — with no preference for the submitting peer's entries: [3](#0-2) 

The call path in `process.rs` passes `peer` into `add_orphan_tx` but never uses it to enforce a per-peer quota: [4](#0-3) 

The critical enabler is that `non_contextual_verify` — the only gate before a transaction enters the verify queue — does **not** check signatures. It only verifies version, size, non-empty I/O, duplicate deps, outputs data, and script hash type: [5](#0-4) [6](#0-5) 

A transaction with random input `OutPoint`s passes `non_contextual_verify`, enters the verify queue, fails at `pre_check` → `resolve_tx` with a missing-input error, and is routed to `add_orphan` in `after_process`: [7](#0-6) 

No signature, no keys, no fees are required to inject entries into the orphan pool.

## Impact Explanation
This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. An attacker targeting multiple CKB nodes simultaneously can saturate each node's orphan pool (cap: 100) at negligible cost, disrupting the orphan-resolution relay mechanism network-wide. Legitimate child transactions whose parents are in-flight are silently dropped and must be resubmitted, degrading transaction propagation across the network. The `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` means injected entries persist for ~80 minutes before natural expiry, requiring only periodic refills to sustain the attack. [8](#0-7) 

## Likelihood Explanation
The attack requires only a P2P connection or repeated `send_transaction` RPC calls. No keys, fees, or privileged access are needed — only structurally valid transactions with random input outpoints. Submitting 100 such transactions fills the pool entirely. The attacker monitors evictions via `TxVerificationResult::Reject` messages propagated back through `send_result_to_relayer` and resubmits replacements to maintain saturation. The cost per refill is bandwidth only. [9](#0-8) 

## Recommendation
Add a `peer_counts: HashMap<PeerIndex, usize>` field to `OrphanPool`. Increment on `add_orphan_tx` and decrement on `remove_orphan_tx`. In `limit_size()`, when the pool is full after expiry-based eviction, prefer to evict an entry from the peer with the highest count rather than using `HashMap::keys().next()`. Additionally, cap each peer's contribution to `DEFAULT_MAX_ORPHAN_TRANSACTIONS / expected_max_peers` at admission time, rejecting the new entry (rather than evicting an existing one) if the submitting peer is already at its quota. [10](#0-9) 

## Proof of Concept
1. Connect to a CKB node as an unprivileged P2P peer or via the `send_transaction` RPC.
2. Generate 100 transactions with: valid version, one input referencing a random 32-byte `OutPoint` (non-existent on-chain), one output with any lock script, matching output data. No signing required — `non_contextual_verify` does not check scripts.
3. Submit all 100. Each passes `non_contextual_verify`, enters the verify queue, fails `pre_check` with `is_missing_input`, and is inserted into the orphan pool via `add_orphan_tx`. Pool is now at capacity.
4. A legitimate user submits a real orphan transaction (child of an in-flight parent). `add_orphan_tx` inserts it and calls `limit_size()`, which evicts an arbitrary entry — potentially the legitimate one.
5. Attacker observes `TxVerificationResult::Reject` for any evicted junk entry and immediately resubmits a replacement to keep the pool saturated.
6. When the legitimate orphan's parent is confirmed, `process_orphan_tx` calls `find_orphan_by_previous`, finds no matching entry, and silently skips promotion. The child transaction is lost until the original sender resubmits it. [11](#0-10)

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L42-45)
```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L96-132)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
    }
```

**File:** tx-pool/src/component/orphan.rs (L134-159)
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
    }
```

**File:** tx-pool/src/process.rs (L507-513)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
                    } else {
```

**File:** tx-pool/src/process.rs (L557-573)
```rust
    pub(crate) async fn add_orphan(
        &self,
        tx: TransactionView,
        peer: PeerIndex,
        declared_cycle: Cycle,
    ) {
        let evicted_txs = self
            .orphan
            .write()
            .await
            .add_orphan_tx(tx, peer, declared_cycle);
        // for any evicted orphan tx, we should send reject to relayer
        // so that we mark it as `unknown` in filter
        for tx_hash in evicted_txs {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }
    }
```

**File:** tx-pool/src/process.rs (L591-597)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
            for orphan in orphans.into_iter() {
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

**File:** verification/src/transaction_verifier.rs (L71-102)
```rust
pub struct NonContextualTransactionVerifier<'a> {
    pub(crate) version: VersionVerifier<'a>,
    pub(crate) size: SizeVerifier<'a>,
    pub(crate) empty: EmptyVerifier<'a>,
    pub(crate) duplicate_deps: DuplicateDepsVerifier<'a>,
    pub(crate) outputs_data_verifier: OutputsDataVerifier<'a>,
    pub(crate) script_hash_type: ScriptHashTypeVerifier<'a>,
}

impl<'a> NonContextualTransactionVerifier<'a> {
    /// Creates a new NonContextualTransactionVerifier
    pub fn new(tx: &'a TransactionView, consensus: &'a Consensus) -> Self {
        NonContextualTransactionVerifier {
            version: VersionVerifier::new(tx, consensus.tx_version()),
            size: SizeVerifier::new(tx, consensus.max_block_bytes()),
            empty: EmptyVerifier::new(tx),
            duplicate_deps: DuplicateDepsVerifier::new(tx),
            outputs_data_verifier: OutputsDataVerifier::new(tx),
            script_hash_type: ScriptHashTypeVerifier::new(tx),
        }
    }

    /// Perform context-independent verification
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
