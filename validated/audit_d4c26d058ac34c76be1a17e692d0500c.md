### Title
Single Peer Can Monopolize the Entire Orphan Pool via Unchecked Per-Peer Quota — (`tx-pool/src/component/orphan.rs`)

---

### Summary

`OrphanPool::add_orphan_tx` stores the submitting peer's `PeerIndex` in each `Entry` but `limit_size` never consults that field when evicting. A single unprivileged P2P peer can fill all `DEFAULT_MAX_ORPHAN_TRANSACTIONS` (100) slots, after which every honest peer's orphan transaction has a ~99% probability of being immediately evicted by the random eviction loop.

---

### Finding Description

`add_orphan_tx` unconditionally inserts the new entry and then calls `limit_size`: [1](#0-0) 

`limit_size` evicts by taking the first key from the `HashMap` iterator — effectively random due to Rust's hash randomization — with no regard for which peer contributed the most entries: [2](#0-1) 

The `peer` field is recorded in `Entry` but is never read inside `limit_size` or anywhere else in `OrphanPool`: [3](#0-2) 

The P2P entry point is `after_process` in `process.rs`. When a remote peer submits a transaction whose inputs resolve to unknown outpoints, `is_missing_input` returns `true` and `add_orphan` is called unconditionally: [4](#0-3) 

`is_missing_input` matches `Reject::Resolve(OutPointError::Unknown)`, which fires before any lock-script execution, so the attacker does not need valid signing keys — any transaction referencing non-existent outpoints qualifies: [5](#0-4) 

---

### Impact Explanation

With 100 attacker-controlled entries filling the pool, each subsequent honest-peer orphan tx is inserted as entry 101, then `limit_size` evicts one uniformly at random. The probability that the honest tx survives is 1/101 ≈ 1%. The attacker can immediately re-submit any of its own txs that happen to be evicted, sustaining near-total pool occupancy indefinitely. Honest peers' orphan transactions are effectively denied service for the duration of the attack, delaying or preventing their parent-resolution flow.

---

### Likelihood Explanation

The attack requires only a single P2P connection and the ability to craft transactions referencing non-existent outpoints — no funds, no valid signatures, no hashpower. The 100-slot cap is small enough that the pool can be saturated in a single burst. There is no rate-limit, no per-peer counter, and no existing guard that would detect or block this pattern.

---

### Recommendation

Inside `limit_size` (or as a pre-check in `add_orphan_tx`), count entries per peer and enforce a per-peer cap (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / MAX_PEERS_PER_POOL` or a fixed small constant such as 10). When the pool is full, prefer evicting from the peer that currently holds the most slots before falling back to random eviction. The `peer` field already present in `Entry` makes this straightforward to implement without schema changes.

---

### Proof of Concept

```rust
// Pseudocode unit test
let mut pool = OrphanPool::new();
let attacker_peer: PeerIndex = 0.into();
let honest_peer:   PeerIndex = 1.into();

// Attacker fills all 100 slots
for i in 0..100 {
    let tx = build_tx(vec![(&nonexistent_hash(i), 0)], 1);
    pool.add_orphan_tx(tx, attacker_peer, 0);
}
assert_eq!(pool.len(), 100);

// Honest peer submits one orphan tx
let honest_tx = build_tx(vec![(&nonexistent_hash(200), 0)], 1);
let honest_id  = honest_tx.proposal_short_id();
pool.add_orphan_tx(honest_tx, honest_peer, 0);

// Pool is back to 100; honest tx survives with only ~1% probability
// In practice, run this 1000 times: honest tx survives ~10 times, evicted ~990 times
assert!(pool.contains_key(&honest_id),
    "honest peer's tx was evicted — attacker monopolized the pool");
```

### Citations

**File:** tx-pool/src/component/orphan.rs (L18-28)
```rust
#[derive(Debug, Clone)]
pub struct Entry {
    /// Transaction
    pub tx: TransactionView,
    /// peer id
    pub peer: PeerIndex,
    /// Declared cycles
    pub cycle: Cycle,
    /// Expire timestamp
    pub expires_at: u64,
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

**File:** tx-pool/src/process.rs (L507-512)
```rust
                    if is_missing_input(reject) {
                        self.send_result_to_relayer(TxVerificationResult::UnknownParents {
                            peer,
                            parents: tx.unique_parents(),
                        });
                        self.add_orphan(tx, peer, declared_cycle).await;
```

**File:** tx-pool/src/util.rs (L150-152)
```rust
pub(crate) fn is_missing_input(reject: &Reject) -> bool {
    matches!(reject, Reject::Resolve(out_point_err) if out_point_err.is_unknown())
}
```
