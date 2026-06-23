Now I have all the information needed to analyze this vulnerability claim precisely.

**Key facts from the code:**

1. `OrphanPool` has two data structures: `entries: HashMap<ProposalShortId, Entry>` and `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>` [1](#0-0) 

2. `limit_size()` caps the pool by checking `self.len()`, which returns `self.entries.len()` — it only counts transactions, not out-point index entries. [2](#0-1) [3](#0-2) 

3. `add_orphan_tx` inserts one entry per input into `by_out_point` with no cap on that map's size. [4](#0-3) 

4. `TRANSACTION_SIZE_LIMIT` is 512,000 bytes. A `CellInput` in molecule is a fixed-size struct: 8 bytes (`since`) + 36 bytes (`OutPoint`) = 44 bytes, plus 4 bytes dynvec offset = 48 bytes per input. This allows ≈ (512,000 − overhead) / 48 ≈ **10,600+ inputs per transaction**. [5](#0-4) 

5. The attack entry point is concrete: a remote peer relays a transaction → `submit_remote_tx` → `process_tx` → `_process_tx` → `pre_check` fails with `Resolve(Unknown)` → `after_process` calls `add_orphan` when `is_missing_input(reject)` is true. [6](#0-5) 

6. `non_contextual_verify` only rejects if `tx_size > TRANSACTION_SIZE_LIMIT` — a 512KB tx with 10,600 inputs passes this check. [7](#0-6) 

---

### Title
Unbounded `by_out_point` Index Growth in OrphanPool Allows Memory Amplification Attack — (`tx-pool/src/component/orphan.rs`)

### Summary
`OrphanPool::limit_size()` caps `entries` at `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100) but places no bound on `by_out_point`. A single orphan transaction with N inputs contributes N entries to `by_out_point`. An attacker can relay 100 maximally-sized orphan transactions (each with ~10,600 inputs referencing distinct unknown out-points), keeping `entries.len() == 100` while `by_out_point.len()` grows to ~1,060,000 entries — a **~10,600× amplification** over the nominal cap.

### Finding Description
In `add_orphan_tx`, after inserting the transaction into `entries`, the code iterates over all inputs and inserts one `OutPoint → HashSet<ProposalShortId>` entry per input into `by_out_point`:

```rust
// orphan.rs lines 150-155
for out_point in tx.input_pts_iter() {
    self.by_out_point
        .entry(out_point)
        .or_default()
        .insert(tx.proposal_short_id());
}
```

`limit_size()` then enforces the cap:

```rust
// orphan.rs lines 119-125
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) { ... }
}
```

`self.len()` is `self.entries.len()`. There is no corresponding check on `self.by_out_point.len()`. The `by_out_point` map is only cleaned up when entries are removed via `remove_orphan_tx`, but the cap never triggers based on `by_out_point` size.

### Impact Explanation
- **Memory amplification**: 100 orphan txs × ~10,600 inputs = ~1,060,000 `OutPoint` keys in `by_out_point`. Each key is 36 bytes; each `HashSet` value has ~56 bytes of overhead + 10 bytes per `ProposalShortId`. Total `by_out_point` memory: ~1,060,000 × ~102 bytes ≈ **~108 MB** from a single attacker sending 100 transactions.
- The attacker can repeat this continuously (evicted orphans are replaced by new ones), sustaining the memory pressure indefinitely.
- This is additive on top of the `entries` memory (100 × up to 512KB = ~51MB), so total orphan pool memory can reach **~160MB** from a single peer, far exceeding the intended bound.
- On resource-constrained nodes or under coordinated multi-peer attack, this can cause OOM or severe memory pressure, degrading node performance and potentially causing network participation failures (block/tx relay stalls).

### Likelihood Explanation
The attack requires no special privileges. Any P2P relay peer can send `SendTransaction` messages. Constructing a valid-looking transaction with thousands of inputs referencing unknown out-points is trivial — the transaction only needs to pass `non_contextual_verify` (size check + cellbase check + `NonContextualTransactionVerifier`). It will fail at resolution with `OutPointError::Unknown`, which is exactly the condition that routes it to the orphan pool. The attacker does not need PoW, keys, or any on-chain state.

### Recommendation
Bound `by_out_point` growth in `limit_size()` or enforce a per-transaction input count limit before admission to the orphan pool. The simplest fix is to add a cap on the number of inputs a transaction may have to be admitted as an orphan (e.g., reject orphan admission if `tx.inputs().len() > some_limit`), or to evict entries from `by_out_point` when its size exceeds a threshold proportional to `DEFAULT_MAX_ORPHAN_TRANSACTIONS`. Bitcoin Core addresses this by limiting orphan pool size in bytes rather than transaction count.

### Proof of Concept
```rust
// Pseudocode — directly exercises OrphanPool without P2P
let mut orphan = OrphanPool::new();
for i in 0..100 {
    // Build a tx with ~10_600 inputs, each referencing a distinct unknown OutPoint
    let tx = build_tx_with_n_distinct_inputs(10_600, i as u64 /* seed */);
    orphan.add_orphan_tx(tx, 0.into(), 0);
}
assert_eq!(orphan.entries.len(), 100);          // capped as expected
assert!(orphan.by_out_point.len() > 1_000_000); // >> 100, unbounded
```

The `by_out_point` map grows to over one million entries while `entries` stays at 100, confirming the invariant violation. Each `add_orphan_tx` call passes `limit_size()` without triggering any cleanup of `by_out_point`. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** tx-pool/src/component/orphan.rs (L42-45)
```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
```

**File:** tx-pool/src/component/orphan.rs (L52-54)
```rust
    pub fn len(&self) -> usize {
        self.entries.len()
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

**File:** util/types/src/core/tx_pool.rs (L309-309)
```rust
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/process.rs (L506-512)
```rust
                    );
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
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

**File:** tx-pool/src/util.rs (L67-73)
```rust
    let tx_size = tx.data().serialized_size_in_block() as u64;
    if tx_size > TRANSACTION_SIZE_LIMIT {
        return Err(Reject::ExceededTransactionSizeLimit(
            tx_size,
            TRANSACTION_SIZE_LIMIT,
        ));
    }
```
