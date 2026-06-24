All code claims check out. Let me verify the `transactions_process.rs` filter that gates `RelayTransactions` admission, since it's critical to the attack path.

Audit Report

## Title
Global Orphan Pool Saturation via No Per-Peer Limit Enables Targeted Eviction of Legitimate Orphan Transactions — (`tx-pool/src/component/orphan.rs`)

## Summary

`OrphanPool` enforces a single global cap of 100 entries with no per-peer sub-accounting. Eviction in `limit_size()` selects entries by HashMap iteration order, ignoring the stored `peer` field entirely. A single malicious P2P peer can saturate the pool via a two-message exchange (hash announcement + transaction delivery), permanently monopolizing all 100 slots and causing legitimate orphan transactions from honest peers to be continuously evicted and silently dropped from automatic promotion.

## Finding Description

**Root cause — `tx-pool/src/component/orphan.rs`:**

The global cap is 100: [1](#0-0) 

`OrphanPool` stores all entries in a single global `HashMap` with no per-peer sub-accounting: [2](#0-1) 

When the pool exceeds capacity, `limit_size()` evicts via `self.entries.keys().next()` — HashMap iteration order — with no reference to the `peer` field stored in each `Entry`: [3](#0-2) 

`add_orphan_tx` inserts the entry and immediately calls `limit_size()`: [4](#0-3) 

**Corrected attack path (two messages, not one):**

The report claims a single `RelayTransactions` message suffices. This is inaccurate. `TransactionsProcess::execute()` contains a mandatory filter that rejects any transaction not previously requested by the node: [5](#0-4) 

`requesting_peer()` returns `Some(peer)` only after the node has already sent `GetRelayTransactions` to that peer: [6](#0-5) 

The correct two-step attack is:

1. Attacker sends a single `RelayTransactionHashes` message containing 100 fabricated tx hashes (1 rate-limit token). `TransactionHashesProcess` calls `state.add_ask_for_txs(peer, tx_hashes)`, inserting all 100 into `unknown_tx_hashes`: [7](#0-6) 

2. The node's `ask_for_txs` timer fires (every 100ms) and sends `GetRelayTransactions` back to the attacker for all 100 hashes, setting `requested = true` and establishing the attacker as `requesting_peer`.

3. Attacker sends a single `RelayTransactions` message with 100 orphan transactions whose hashes match the announced ones (1 rate-limit token). The filter passes. Each transaction fails resolution with `is_missing_input` and is admitted via `add_orphan`: [8](#0-7) 

4. Pool is now at capacity (100 entries, all attacker-controlled). Any subsequent legitimate orphan triggers `limit_size()`, which randomly evicts one entry. Attacker repeats the two-message cycle to replace evicted slots.

5. When the legitimate parent arrives and `process_orphan_tx` is called, `find_orphan_by_previous` returns nothing for the evicted child: [9](#0-8) 

The child is silently absent from the orphan pool and is not promoted to pending.

**Why existing mitigations are insufficient:**

- The relay rate limiter (30 req/sec per `(peer, message_type)`) does not prevent the attack because both the `RelayTransactionHashes` and `RelayTransactions` messages are batch messages — all 100 entries are delivered in two messages consuming two rate-limit tokens total: [10](#0-9) 

- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767` is a soft limit on hash announcements, not on orphan pool admission. 100 hashes are well within this limit: [11](#0-10) 

- `add_ask_for_txs` applies `take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` before inserting, but 100 << 32767: [12](#0-11) 

## Impact Explanation

**High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** A single peer can permanently monopolize the entire 100-slot orphan pool with two batch messages at negligible cost. Under sustained attack, all orphan transactions from honest peers are continuously evicted before their parents arrive, causing systematic transaction propagation failures. The orphan pool is a critical relay mechanism; its permanent saturation degrades the network's ability to propagate transactions that arrive out of order, which is a normal condition during network propagation.

## Likelihood Explanation

The attack requires only a single P2P connection and two batch messages. No funds, no privileged access, and no special timing are required beyond waiting ~100ms for the `ask_for_txs` timer to fire. The attacker can sustain the attack indefinitely by repeating the two-message cycle once per eviction event. The 100-slot global limit makes saturation trivially achievable.

## Recommendation

1. **Add per-peer orphan limits**: Track orphan count per `PeerIndex` in `OrphanPool` and reject new entries from a peer that already holds a disproportionate share (e.g., `DEFAULT_MAX_ORPHAN_TRANSACTIONS / max_peers`).
2. **Prefer evicting from the most-represented peer**: Replace the random eviction in `limit_size()` with a policy that evicts from the peer holding the most orphan slots, protecting honest peers from attacker-controlled saturation.
3. **Increase the global limit or make it configurable**: The current limit of 100 is very small; even modest honest traffic can be disrupted.

## Proof of Concept

```
// Step 1: announce 100 fabricated tx hashes
let fake_hashes: Vec<Byte32> = (0..100).map(|_| random_byte32()).collect();
send_relay_transaction_hashes(peer_connection, fake_hashes.clone()); // 1 rate-limit token

// Wait ~100ms for node to send GetRelayTransactions back

// Step 2: send 100 orphan transactions whose hashes match the announced ones
let mut batch = vec![];
for hash in &fake_hashes {
    let fake_parent = random_byte32();
    let orphan_tx = build_tx_with_input_and_hash(fake_parent, 0, hash);
    batch.push((orphan_tx, /*declared_cycles=*/1u64));
}
send_relay_transactions(peer_connection, batch); // 1 rate-limit token

// Pool is now full (100 attacker-controlled entries).
// Maintain saturation:
loop {
    let new_hash = random_byte32();
    send_relay_transaction_hashes(peer_connection, vec![new_hash.clone()]);
    // wait ~100ms
    let orphan_tx = build_tx_with_input_and_hash(random_byte32(), 0, &new_hash);
    send_relay_transactions(peer_connection, vec![(orphan_tx, 1)]);
}
```

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

**File:** tx-pool/src/component/orphan.rs (L157-158)
```rust
        // DoS prevention: do not allow OrphanPool to grow unbounded
        self.limit_size()
```

**File:** sync/src/relayer/transactions_process.rs (L49-55)
```rust
                .filter(|(tx, _)| {
                    !tx_filter.contains(&tx.hash())
                        && unknown_tx_hashes
                            .get_priority(&tx.hash())
                            .map(|priority| priority.requesting_peer() == Some(self.peer))
                            .unwrap_or_default()
                })
```

**File:** sync/src/types/mod.rs (L1295-1301)
```rust
    pub fn requesting_peer(&self) -> Option<PeerIndex> {
        if self.requested {
            self.peers.first().cloned()
        } else {
            None
        }
    }
```

**File:** sync/src/types/mod.rs (L1486-1488)
```rust
        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
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

**File:** util/constant/src/sync.rs (L67-72)
```rust
/// The maximum number transaction hashes inside a `RelayTransactionHashes` message
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
