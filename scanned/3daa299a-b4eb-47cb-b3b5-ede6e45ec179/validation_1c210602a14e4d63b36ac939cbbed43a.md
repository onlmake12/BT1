### Title
Orphan Transaction Pool Slot Exhaustion via Zero-Cost Flooding — (`tx-pool/src/component/orphan.rs`)

---

### Summary

The CKB orphan transaction pool (`OrphanPool`) has a hard cap of `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` entries but applies **no fee-rate admission check** when inserting orphan transactions, and evicts entries **randomly** when the pool is full. An unprivileged P2P relay peer can craft 100 syntactically valid transactions referencing non-existent inputs (which cost nothing to produce) and continuously flood the orphan pool, causing legitimate orphan transactions from honest peers to be randomly evicted before their parents arrive.

---

### Finding Description

The `OrphanPool` in `tx-pool/src/component/orphan.rs` stores transactions whose inputs are not yet known. It is bounded by a hard constant:

```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
``` [1](#0-0) 

When a new orphan is inserted via `add_orphan_tx`, it is unconditionally appended to the `entries` HashMap, and then `limit_size` is called:

```rust
while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
    // Evict a random orphan:
    let id = self.entries.keys().next().cloned().expect("bound checked");
    if let Some(entry) = self.remove_orphan_tx(&id) {
        evicted_txs.push(entry.tx.hash());
    }
}
``` [2](#0-1) 

There is **no fee-rate check** at orphan admission. The main pool's `check_tx_fee` (which enforces `min_fee_rate`) is only called during `_process_tx` for transactions entering the main pool. Orphan transactions bypass this check entirely — they are added to the orphan pool before their inputs are resolved, so fee cannot be computed. The `add_orphan_tx` function accepts any syntactically valid transaction with missing inputs:

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
    ...
    self.limit_size()
}
``` [3](#0-2) 

There is also **no per-peer limit** — the `OrphanPool` struct tracks only `entries` and `by_out_point`, with no per-peer accounting:

```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
}
``` [4](#0-3) 

The entry path from a P2P peer is: relay peer sends a `RelayTransactions` message → `process_tx` is called → non-contextual verification passes (format only) → `_process_tx` fails with `Resolve(OutPointError::Unknown)` (missing input) → `after_process` calls `add_orphan`:

```rust
if is_missing_input(reject) {
    self.send_result_to_relayer(TxVerificationResult::UnknownParents { ... });
    self.add_orphan(tx, peer, declared_cycle).await;
}
``` [3](#0-2) [5](#0-4) 

The main pool enforces `min_fee_rate` (default 1,000 shannons/KB in production):

```rust
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
``` [6](#0-5) 

But this check is never applied to orphan pool admission.

---

### Impact Explanation

An attacker controlling a single P2P peer can:

1. Craft 100 syntactically valid transactions with outputs meeting minimum cell capacity but with inputs referencing non-existent out-points (zero actual cost — no CKB ownership required).
2. Relay all 100 to a victim node, filling the orphan pool to capacity.
3. Continuously re-send evicted attacker transactions (since eviction is random, ~50% of evictions will be attacker transactions, which are immediately re-sent).

The result: the orphan pool is permanently dominated by attacker transactions. Legitimate orphan transactions from honest peers are randomly evicted before their parent transactions arrive. When the parent arrives, `find_by_previous` finds no matching orphan, so the child is never automatically promoted to the pending pool. The honest user must re-submit the child transaction, which may again be evicted.

This degrades the relay functionality of the node for all peers, with zero on-chain cost to the attacker.

---

### Likelihood Explanation

The attack requires only a single P2P connection and the ability to craft valid-format transactions (no CKB balance needed). The orphan pool limit is only 100 entries, making it trivially fillable. The random eviction policy means the attacker can maintain pool dominance by re-sending evicted transactions at negligible bandwidth cost. This is directly reachable from any unprivileged relay peer.

---

### Recommendation

Apply one or more of the following mitigations:

1. **Per-peer orphan limit**: Track how many orphan transactions each peer has contributed and cap it (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`). This is the direct analog to the Audius fix (enforcing the minimum per service provider rather than globally).
2. **Fee-rate-aware eviction**: When the pool is full, evict the orphan with the lowest declared fee rate (using `declared_cycle` already stored in `Entry`) rather than a random entry.
3. **Increase pool size or add per-peer accounting**: Track orphan entries by `PeerIndex` and evict the peer with the most entries when the pool is full.

---

### Proof of Concept

```
1. Attacker connects to victim node as a P2P relay peer.
2. Attacker generates 100 transactions:
   - Each has one input referencing a random non-existent OutPoint
   - Each has one output with minimum capacity (61 CKB secp256k1 cell)
   - Zero fee (inputs don't exist, so fee is unchecked at orphan admission)
3. Attacker sends all 100 via RelayTransactions messages.
4. Each transaction passes non-contextual verification (valid format),
   fails _process_tx with Resolve(OutPointError::Unknown),
   and is added to the orphan pool via add_orphan_tx.
5. OrphanPool.len() == 100 == DEFAULT_MAX_ORPHAN_TRANSACTIONS.
6. Honest peer sends a legitimate orphan transaction.
   → It is inserted, then limit_size() randomly evicts one entry.
   → ~50% chance the evicted entry is an attacker transaction.
   → Attacker immediately re-sends the evicted transaction.
7. The honest orphan is evicted with ~50% probability on each cycle.
   With 100 attacker transactions and 1 honest transaction,
   the honest transaction's expected survival time is ~1 new-orphan event.
8. When the honest orphan's parent arrives, find_by_previous() returns
   nothing → child is never promoted → user must re-submit indefinitely.
```

The attack cost is zero on-chain CKB and requires only sustained P2P bandwidth (~100 small transactions per eviction cycle).

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L42-45)
```rust
pub(crate) struct OrphanPool {
    pub(crate) entries: HashMap<ProposalShortId, Entry>,
    pub(crate) by_out_point: HashMap<OutPoint, HashSet<ProposalShortId>>,
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

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** resource/ckb.toml (L212-212)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
```
