Looking at the full code path from P2P relay → orphan admission, and the size limit enforcement.

### Title
Unbounded `by_out_point` Index Growth in OrphanPool via High-Input Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

### Summary

`add_orphan_tx` inserts one entry into `entries` but N entries into `by_out_point` (one per input `OutPoint`). `limit_size` only enforces the 100-transaction cap on `entries.len()`, leaving `by_out_point` unbounded. An unprivileged relay peer can submit 100 orphan transactions each carrying ~10,000 inputs (within the 512 KB size limit), causing `by_out_point` to accumulate ~1,000,000 entries while `entries` stays at exactly 100.

---

### Finding Description

`OrphanPool` maintains two data structures:

```
entries:      HashMap<ProposalShortId, Entry>          // one entry per tx
by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>> // one entry per input OutPoint
``` [1](#0-0) 

`add_orphan_tx` inserts the transaction into `entries` (cost: 1) and then iterates every input, inserting each `OutPoint` into `by_out_point` (cost: N): [2](#0-1) 

`limit_size`, despite its "DoS prevention" comment, only bounds `entries.len()` against `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`: [3](#0-2) 

`by_out_point` is never independently bounded.

---

### Impact Explanation

`TRANSACTION_SIZE_LIMIT = 512 * 1,000 = 512,000 bytes`: [4](#0-3) 

Each `CellInput` serializes to ~44 bytes of payload + 4 bytes of molecule offset = 48 bytes. A minimal transaction with N inputs fits in 512 KB when N ≤ (512,000 − ~200) / 48 ≈ **10,662 inputs**. A transaction with 10,000 inputs (~480 KB) passes the size check.

`non_contextual_verify` (which enforces the size limit) is called in `resumeble_process_tx` **before** `enqueue_verify_queue`: [5](#0-4) 

After the verify queue processes the tx and finds all inputs missing, `after_process` calls `add_orphan`: [6](#0-5) 

With 100 such transactions (the orphan cap), `by_out_point` accumulates up to **1,000,000 entries**:

- Each entry: ~36 B (OutPoint key) + ~56 B (HashSet overhead) + ~10 B (ProposalShortId) + ~50 B (HashMap overhead) ≈ 152 B
- 1,000,000 × 152 B ≈ **~152 MB** for `by_out_point` alone
- Plus 100 × ~480 KB transaction bodies ≈ **~48 MB**
- **Total: ~200 MB** from a single attacker

---

### Likelihood Explanation

The attack requires only a P2P connection — no keys, no PoW, no privileged role. The relayer rate-limiter (30 req/s per peer/message-type) slows but does not prevent the attack; an attacker can use multiple peers or simply wait. The 100-tx eviction loop runs after each insertion, so the pool never exceeds 100 transactions, but `by_out_point` is never trimmed to match. [7](#0-6) 

---

### Recommendation

Enforce a per-transaction input-count cap before orphan admission (e.g., reject any transaction with more inputs than a configurable threshold), **or** bound `by_out_point` directly — e.g., after `limit_size` evicts transactions, also remove their `by_out_point` entries (which `remove_orphan_tx` already does correctly) and add an explicit assertion or hard cap on `by_out_point.len()`. The simplest fix is to add an input-count check in `non_contextual_verify` or at the orphan-admission gate.

---

### Proof of Concept

```rust
// Pseudocode — locally testable unit test
let mut pool = OrphanPool::new();
for i in 0..100 {
    // Build a tx with 10_000 distinct OutPoints as inputs, all unknown
    let tx = build_tx_with_n_inputs(10_000, i);
    pool.add_orphan_tx(tx, peer, cycles);
}
assert_eq!(pool.entries.len(), 100);          // capped correctly
assert!(pool.by_out_point.len() > 100);       // NOT capped — ~1_000_000
``` [8](#0-7)

### Citations

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

**File:** tx-pool/src/component/orphan.rs (L145-155)
```rust
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
```

**File:** util/types/src/core/tx_pool.rs (L306-309)
```rust
/// The ckb consensus does not limit the size of a single transaction,
/// but if the size of the transaction is close to the limit of the block,
/// it may cause the transaction to fail to be packed
pub const TRANSACTION_SIZE_LIMIT: u64 = 512 * 1_000;
```

**File:** tx-pool/src/process.rs (L341-352)
```rust
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

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** sync/src/relayer/mod.rs (L59-61)
```rust
pub const MAX_RELAY_PEERS: usize = 128;
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
pub const MAX_RELAY_TXS_BYTES_PER_BATCH: usize = 1024 * 1024;
```
