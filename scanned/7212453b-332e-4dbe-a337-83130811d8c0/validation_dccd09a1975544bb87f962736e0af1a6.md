Audit Report

## Title
Global Orphan Pool Saturation via No Per-Peer Limit Enables Targeted Eviction of Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary

The `OrphanPool` enforces a single global cap of 100 entries with no per-peer sub-accounting. Eviction in `limit_size()` selects entries by HashMap iteration order, ignoring the stored `peer` field entirely. A single malicious P2P peer can send all 100 orphan transactions in one `RelayTransactions` batch message (consuming only one rate-limit token), permanently saturating the pool and causing all legitimate orphan transactions from honest peers to be continuously evicted and silently dropped from automatic promotion.

## Finding Description

**Root cause — `tx-pool/src/component/orphan.rs`:**

The `OrphanPool` struct stores all entries in a single global `HashMap<ProposalShortId, Entry>` with no per-peer sub-accounting: [1](#0-0) 

The global cap is a compile-time constant of 100: [2](#0-1) 

When the pool exceeds capacity, `limit_size()` evicts entries by calling `self.entries.keys().next()` — HashMap iteration order, effectively random — with no reference to the `peer` field stored in each `Entry`: [3](#0-2) 

`add_orphan_tx` inserts the entry and immediately calls `limit_size()`: [4](#0-3) 

**Attack path:**

1. Attacker connects as a P2P peer via the relay protocol.
2. Attacker sends a single `RelayTransactions` batch message containing 100 transactions, each referencing a fabricated non-existent `OutPoint`. The relay rate limiter is keyed by `(peer, message_type)` and allows 30 requests/second — but a single `RelayTransactions` message can carry all 100 transactions, consuming only one rate-limit token: [5](#0-4) 
3. Each transaction passes `non_contextual_verify`, fails resolution with `is_missing_input`, and is admitted to the orphan pool via `add_orphan` in `TxPoolService`: [6](#0-5) 
4. The pool is now at capacity (100 entries, all attacker-controlled). Any subsequent legitimate orphan triggers `limit_size()`, which randomly evicts one entry. The attacker re-sends a replacement orphan to maintain saturation.
5. When the legitimate parent transaction arrives and `process_orphan_tx` is called, `find_orphan_by_previous` returns nothing for the evicted child: [7](#0-6) 
6. The child transaction is silently absent from the orphan pool and is not promoted to pending. The original sender must re-relay it.

**Why existing mitigations are insufficient:**

- The relay rate limiter (30 req/sec per `(peer, message_type)`) does not prevent the attack because `RelayTransactions` is a batch message — all 100 orphans can be delivered in a single message.
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` tracks unknown *hash announcements* via `RelayTransactionHashes`, a completely separate code path from full orphan transaction submissions via `RelayTransactions`: [8](#0-7) 

## Impact Explanation

This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**. A single peer can permanently monopolize the entire orphan pool with one batch message at negligible cost. Under sustained attack, all orphan transactions from honest peers are continuously evicted before their parents arrive, causing systematic transaction propagation failures across the network. The orphan pool is a critical relay mechanism; its permanent saturation degrades the network's ability to propagate transactions that arrive out of order, which is a normal condition during network propagation.

## Likelihood Explanation

The attack requires only a single P2P connection and one `RelayTransactions` batch message containing 100 minimal transactions with fabricated parent hashes. No funds, no privileged access, and no special timing are required. The attacker can sustain the attack indefinitely by sending one replacement message per eviction event. The 100-slot global limit makes saturation trivially achievable in a single round-trip.

## Recommendation

1. **Add per-peer orphan limits**: Track orphan count per `PeerIndex` in `OrphanPool` and reject new entries from a peer that already holds a disproportionate share (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).
2. **Prefer evicting from the most-represented peer**: Replace the random eviction in `limit_size()` with a policy that evicts from the peer holding the most orphan slots, protecting honest peers from attacker-controlled saturation.
3. **Increase the global limit or make it configurable**: The current limit of 100 is very small; even modest honest traffic can be disrupted.

## Proof of Concept

Connect as a P2P peer and send a single `RelayTransactions` message containing 100 transactions, each a minimal valid transaction whose single input references a fabricated `OutPoint` (random 32-byte tx hash, index 0). Each transaction passes `non_contextual_verify` and is admitted to the orphan pool. After this one message, the pool is full. Any subsequent legitimate orphan from an honest peer triggers random eviction. Re-send one replacement orphan per eviction to maintain saturation. When the honest peer's parent transaction arrives, `find_by_previous` returns an empty list for the evicted child, and the child is silently lost from the pool.

```rust
// Pseudocode for attacker:
let mut batch = vec![];
for _ in 0..100 {
    let fake_parent = random_byte32();
    let orphan_tx = build_tx_with_input(fake_parent, 0);
    batch.push((orphan_tx, /*declared_cycles=*/1u64));
}
send_relay_transactions(peer_connection, batch); // single message, one rate-limit token

// Maintain saturation:
loop {
    let fake_parent = random_byte32();
    let orphan_tx = build_tx_with_input(fake_parent, 0);
    send_relay_transactions(peer_connection, vec![(orphan_tx, 1)]);
}
```

The relevant constant and eviction logic are confirmed at: [2](#0-1) [9](#0-8)

### Citations

**File:** tx-pool/src/component/orphan.rs (L16-16)
```rust
pub(crate) const DEFAULT_MAX_ORPHAN_TRANSACTIONS: usize = 100;
```

**File:** tx-pool/src/component/orphan.rs (L41-45)
```rust
#[derive(Default, Debug, Clone)]
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

**File:** tx-pool/src/component/orphan.rs (L157-158)
```rust
        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
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

**File:** tx-pool/src/process.rs (L595-596)
```rust
        while let Some(previous) = orphan_queue.pop_front() {
            let orphans = self.find_orphan_by_previous(&previous).await;
```

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
