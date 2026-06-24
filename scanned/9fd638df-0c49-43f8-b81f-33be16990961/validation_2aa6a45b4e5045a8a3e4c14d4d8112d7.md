Audit Report

## Title
Costless P2P Peer Can Permanently Saturate the OrphanPool via Fabricated-Input Transactions - (`tx-pool/src/component/orphan.rs`)

## Summary
The `OrphanPool` is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` and evicts entries via arbitrary HashMap iteration order with no per-peer accounting. An unprivileged P2P peer can fill the pool entirely by announcing 100 transaction hashes, waiting for the node to request them, then delivering structurally-valid transactions whose inputs reference non-existent UTXOs. Because `OutPointError::Unknown` is not a malformed-tx error, no ban is triggered, and the attack can be sustained indefinitely at negligible cost.

## Finding Description

**Root cause — no per-peer limit and arbitrary eviction:**

`DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is the hard cap. [1](#0-0) 

When the pool is full, `limit_size()` evicts by calling `self.entries.keys().next()` on a `HashMap`, which gives no fairness guarantee across peers: [2](#0-1) 

`add_orphan_tx` performs no per-peer accounting before inserting: [3](#0-2) 

**Exploit path:**

1. Attacker sends `RelayTransactionHashes` with 100 unique hashes. The node requests them. The filter in `TransactionsProcess::execute` is satisfied because the node itself requested the transactions: [4](#0-3) 

2. Attacker delivers 100 transactions with fabricated `OutPoint`s (random 32-byte tx hashes, index 0). Each is submitted via `submit_remote_tx`: [5](#0-4) 

3. Each tx passes `non_contextual_verify` (structural checks only — no UTXO existence check): [6](#0-5) 

4. The verify worker calls `_process_tx` → `pre_check` → `resolve_tx`, which fails with `OutPointError::Unknown`. `is_missing_input` matches exactly this error: [7](#0-6) 

5. `after_process` detects `is_missing_input` and calls `add_orphan`. Critically, `OutPointError::Unknown` is **not** a malformed-tx error, so `ban_malformed` is never called: [8](#0-7) 

   This is confirmed by the test suite: `Reject::Resolve(OutPointError::Unknown(...))` → `is_malformed_tx()` returns `false`. [9](#0-8) 

6. After 100 insertions the pool is full. Any subsequent legitimate orphan is randomly evicted. When its parent arrives and `process_orphan_tx` runs, `find_orphan_by_previous` returns empty and the legitimate transaction is silently dropped: [10](#0-9) 

**Why existing checks fail:** The `tx_filter` deduplication only prevents re-processing the same hash. The attacker uses 100 distinct hashes per wave. The `ORPHAN_TX_EXPIRE_TIME = 100 * MAX_BLOCK_INTERVAL` is long enough that attacker entries persist across many blocks. There is no rate limit, no per-peer quota, and no economic cost enforced before orphan admission.

## Impact Explanation

This matches the allowed CKB bounty impact: **High — a vulnerability or bad design that can cause CKB network congestion with few costs.** A single unprivileged peer can permanently render the orphan pool unusable on any targeted node. Transactions that depend on in-flight parents (a normal condition during high-throughput periods or chain reorganizations) are silently dropped and never confirmed unless the user detects the failure and resubmits — which the attacker can defeat by sustaining the flood. Applied at scale across multiple nodes, this degrades network-wide transaction propagation.

## Likelihood Explanation

The attack requires only a standard P2P connection — no funds, no privileged access, no hashpower. The attacker crafts 100 structurally-valid transactions with random fabricated `OutPoint`s, announces their hashes, and delivers them on request. Total bandwidth is negligible. The attack is indefinitely repeatable by rotating transaction hashes (each wave uses 100 new unique hashes). No banning is triggered because `OutPointError::Unknown` is explicitly not a malformed-tx condition.

## Recommendation

1. **Per-peer orphan limit**: Track orphan count per `PeerIndex` in `OrphanPool`. Reject or evict the contributing peer's oldest entry first when the pool is full, rather than evicting randomly across all peers.
2. **Prioritized eviction**: When the pool is full, prefer evicting entries from the peer with the highest orphan count, making the attack self-limiting.
3. **Increase cap with aggressive expiry**: `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` is very small. Combine a larger cap with a short expiry window (e.g., 10 blocks) so attacker entries age out quickly.
4. **Announce-before-relay validation**: Before admitting a relayed transaction to the orphan pool, verify that at least one of its missing inputs was recently announced on the network (i.e., is plausibly in-flight), rather than accepting any missing-input transaction unconditionally.

## Proof of Concept

```
Attacker (P2P peer) → CKB node:

1. Send RelayTransactionHashes { hashes: [H1, H2, ..., H100] }
   (H_i = distinct random 32-byte values)

2. Node responds: GetRelayTransactions { tx_hashes: [H1..H100] }
   (filter passes: node itself requested these hashes)

3. Attacker responds with RelayTransactions containing 100 transactions:
   Each tx_i:
     - inputs:  [{ previous_output: { tx_hash: <random 32 bytes>, index: 0 } }]
     - outputs: [{ capacity: 100 CKB, lock: always_success }]
     - witnesses: [<valid witness>]
   (Structurally valid; inputs reference non-existent UTXOs)

4. Each tx_i:
   - passes non_contextual_verify (no UTXO existence check)
   - enters verify_queue via enqueue_verify_queue
   - verify worker: pre_check → resolve_tx → OutPointError::Unknown
   - after_process: is_missing_input == true → add_orphan (no ban)
   - add_orphan_tx inserts into OrphanPool with no per-peer guard

5. After 100 insertions: OrphanPool.len() == DEFAULT_MAX_ORPHAN_TRANSACTIONS
   Any legitimate orphan tx is randomly evicted on insertion.

6. When the legitimate orphan's parent arrives:
   process_orphan_tx → find_orphan_by_previous → returns empty
   Legitimate transaction is silently dropped.

7. Attacker sustains attack by repeating step 1 with 100 new unique hashes.
   No ban is ever triggered.
```

### Citations

**File:** tx-pool/src/component/orphan.rs (L15-16)
```rust
pub(crate) const ORPHAN_TX_EXPIRE_TIME: u64 = 100 * MAX_BLOCK_INTERVAL;
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L119-125)
```rust
        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
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

**File:** sync/src/relayer/transactions_process.rs (L49-56)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
                .collect()
```

**File:** sync/src/relayer/transactions_process.rs (L85-93)
```rust
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
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

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/process.rs (L591-596)
```rust
    pub(crate) async fn process_orphan_tx(&self, tx: &TransactionView) {
        let mut orphan_queue: VecDeque<TransactionView> = VecDeque::new();
        orphan_queue.push_back(tx.clone());

        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** util/types/src/core/tests/tx_pool.rs (L29-36)
```rust
        OutPointError::Dead(Default::default()),
        OutPointError::Unknown(Default::default()),
        OutPointError::OutOfOrder(Default::default()),
        OutPointError::InvalidDepGroup(Default::default()),
        OutPointError::InvalidHeader(Default::default()),
    ] {
        let reject = Reject::Resolve(error);
        assert!(!reject.is_malformed_tx());
```
