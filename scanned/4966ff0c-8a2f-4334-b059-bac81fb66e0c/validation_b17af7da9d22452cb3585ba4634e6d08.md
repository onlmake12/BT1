### Title
OrphanPool `by_out_point` Index Grows Proportionally to `num_txs × inputs_per_tx`, Bypassing the `DEFAULT_MAX_ORPHAN_TRANSACTIONS` Memory Bound — (`tx-pool/src/component/orphan.rs`)

---

### Summary

The `OrphanPool` enforces a count limit of 100 entries via `DEFAULT_MAX_ORPHAN_TRANSACTIONS`, but the secondary index `by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>` is populated with one entry **per input** of each orphan transaction. The `limit_size()` enforcement only checks `self.entries.len()`, leaving `by_out_point` unbounded relative to input count. An unprivileged remote peer can craft orphan transactions each carrying a large number of inputs, causing `by_out_point` to grow to `num_txs × inputs_per_tx` entries — far beyond what the count cap implies.

---

### Finding Description

In `add_orphan_tx`, after inserting the transaction into `self.entries`, the code iterates over every input and inserts each `OutPoint` into `by_out_point`: [1](#0-0) 

The `limit_size()` function enforces the cap only on `self.entries`: [2](#0-1) 

It never checks or limits `self.by_out_point.len()`. With `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100`: [3](#0-2) 

…the pool allows exactly 100 transactions in `entries`, but `by_out_point` can hold up to `100 × inputs_per_tx` entries. The `shrink_to_fit` call only reclaims capacity after removals; it does not cap the logical size. [4](#0-3) 

---

### Impact Explanation

CKB's block size limit implicitly caps the number of inputs per transaction (a minimal CKB input is ~44 bytes: 32-byte `tx_hash` + 4-byte `index` + 8-byte `since`). With a ~597 KB block size ceiling, a single transaction could carry on the order of thousands of inputs. At 100 orphan transactions × thousands of inputs each, `by_out_point` can accumulate hundreds of thousands of `OutPoint` → `HashSet<ProposalShortId>` entries, consuming tens to hundreds of MB of heap — well beyond what the "100 orphan transactions" limit implies to operators and monitoring systems. This constitutes a memory amplification attack causing elevated memory pressure and potential node instability.

---

### Likelihood Explanation

The attack path is straightforward and requires no privilege:
1. A remote peer sends `RelayTransactions` P2P messages containing crafted transactions whose parent outputs do not exist in the local chain.
2. The node's relay handler calls `add_orphan_tx` for each such transaction.
3. The attacker continuously rotates 100 high-input orphan transactions (replacing expired ones), maintaining sustained `by_out_point` bloat.

No PoW, no keys, no special role required — only the ability to send P2P relay messages.

---

### Recommendation

Bound `by_out_point` growth explicitly. Options include:
- Enforce a cap on the total number of `by_out_point` entries (e.g., `MAX_ORPHAN_TRANSACTIONS × MAX_INPUTS_PER_ORPHAN`) and reject or evict when exceeded.
- Track per-orphan input count and reject transactions whose input count exceeds a configurable threshold before inserting into the pool.
- Include `by_out_point.len()` in the `limit_size()` eviction logic so that memory, not just entry count, is the enforced invariant.

---

### Proof of Concept

```
1. Craft 100 transactions, each with N inputs referencing non-existent OutPoints
   (so they are classified as orphans). N = floor(MAX_BLOCK_BYTES / sizeof(Input)).
2. Send each via RelayTransactions P2P message to the target node.
3. After insertion, assert:
     orphan_pool.by_out_point.len() == 100 * N
   while:
     orphan_pool.entries.len() == 100   ← count limit satisfied
4. Observe that by_out_point.len() >> entries.len(), confirming the
   count limit does not bound memory.
5. Repeat as expired entries are evicted to maintain sustained pressure.
``` [5](#0-4)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L65-68)
```rust
    fn shrink_to_fit(&mut self) {
        shrink_to_fit!(self.entries, SHRINK_THRESHOLD);
        shrink_to_fit!(self.by_out_point, SHRINK_THRESHOLD);
    }
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

**File:** tx-pool/src/component/orphan.rs (L134-158)
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
```
