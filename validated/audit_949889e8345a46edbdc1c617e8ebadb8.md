Audit Report

## Title
Unbounded `peers` Vector Growth in `UnknownTxHashPriority::push_peer` Enables Mutex-Held CPU Exhaustion via Nested Iteration — (`File: sync/src/types/mod.rs`)

## Summary
`push_peer` appends to an internal `Vec<PeerIndex>` with no deduplication and no size cap. Because the overflow guard in `add_ask_for_txs` checks only the number of distinct tx-hash keys — not the length of any `peers` vector — an attacker can pre-inflate every entry's `peers` vector to arbitrary length K by re-announcing the same hashes. When a second peer later triggers the guard, a nested O(entries × K) loop executes while holding the `unknown_tx_hashes` mutex, blocking all relay operations on the victim node for the duration.

## Finding Description

**Root cause — `push_peer` is unconditional:**

`push_peer` at `sync/src/types/mod.rs:1291-1293` simply calls `self.peers.push(peer_index)` with no deduplication check and no length cap. [1](#0-0) 

It is called unconditionally from `add_ask_for_txs` for every already-existing entry on every invocation: [2](#0-1) 

**Guard is blind to `peers` vector length:**

The overflow guard at lines 1507-1509 checks only `unknown_tx_hashes.len()` — the count of distinct hash keys — against `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`. Re-announcing the same hashes adds no new keys, so the guard never fires regardless of how large each entry's `peers` vector grows. [3](#0-2) 

**Expensive nested loop under the mutex:**

When the guard *does* fire (triggered by a second peer adding new distinct hashes), the code at lines 1516-1523 iterates over every entry and every element of every `peers` vector while holding the `unknown_tx_hashes` mutex locked since line 1484. Total work is O(entries × peers_per_entry). [4](#0-3) 

**`tx_filter` does not mitigate the attack:**

`transaction_hashes_process.rs` filters out hashes already present in `tx_filter` before calling `add_ask_for_txs`. Fabricated (non-existent) tx hashes are never resolved and never added to `tx_filter`, so they pass the filter on every re-announcement and reach `push_peer` unconditionally. [5](#0-4) 

**Exploit flow:**
1. Peer A sends 25,000 fabricated unique tx hashes → 25,000 entries created, `peers = [A]` each. Guard does not fire (25,000 < 50,000).
2. Peer A re-sends the same 25,000 hashes K times → each entry's `peers` vector grows to length K+1. `unknown_tx_hashes.len()` stays at 25,000; guard never fires.
3. Peer B sends 25,000 different fabricated hashes → total key count reaches 50,000, triggering the guard.
4. The nested loop executes ≈ 25,000 × (K+1) + 25,000 × 1 iterations while holding the mutex. At K = 10,000 this is ~250 million iterations, stalling every concurrent caller of `pop_ask_for_txs` and `mark_as_known_txs`.

## Impact Explanation

Holding the `unknown_tx_hashes` mutex for hundreds of milliseconds blocks all transaction relay operations on the victim node — fetching, announcing, and marking transactions as known — for every connected peer during the lock-hold period. The attack is repeatable: once the mutex is released the attacker can re-inflate and re-trigger. This constitutes a bad design that can cause CKB network congestion (degraded transaction propagation across nodes accepting inbound relay connections) with low cost to the attacker. **Impact: High — vulnerability or bad design which could cause CKB network congestion with few costs.**

## Likelihood Explanation

- Requires only two standard peer connections; no keys, stake, or privileged role.
- `RelayTransactionHashes` is accepted from any connected peer with no per-peer rate limit. [6](#0-5) 
- Fabricated hashes cost nothing on-chain and are never rejected by `tx_filter`.
- Bandwidth cost is ~800 KB per message; 10,000 messages ≈ 8 GB — feasible for a motivated attacker. Smaller K values still cause measurable lock contention.
- The attack is fully repeatable after each mutex release.

## Recommendation

1. **Deduplicate in `push_peer`:** Check for existing membership before appending, or replace `Vec<PeerIndex>` with a `HashSet<PeerIndex>` to make deduplication O(1). [1](#0-0) 

2. **Cap the `peers` vector:** Enforce a hard maximum equal to the maximum number of connected peers so the vector cannot grow beyond a known bound.

3. **Eliminate the nested scan:** Replace the O(entries × peers) loop at lines 1516-1523 with a separate `HashMap<PeerIndex, usize>` counter maintained incrementally as entries are inserted and removed, reducing the guard check to O(1). [4](#0-3) 

## Proof of Concept

**Manual steps (no privileged access required):**

1. Connect peers A and B to the victim node using the standard Relay v3 protocol.
2. From peer A, send repeated `RelayTransactionHashes` messages each containing the same 25,000 fabricated (non-existent) tx hashes. Repeat K = 10,000 times. Each call to `add_ask_for_txs` finds existing entries and calls `push_peer(A)` 25,000 times; `unknown_tx_hashes.len()` remains 25,000 so the guard never fires.
3. From peer B, send one `RelayTransactionHashes` message with 25,000 *different* fabricated tx hashes. This pushes `unknown_tx_hashes.len()` to 50,000, triggering the guard at line 1507.
4. The nested loop at lines 1517-1522 executes ~250 million iterations while holding the `unknown_tx_hashes` mutex, blocking all concurrent relay operations on the victim node.

**Expected outcome:** The victim node's transaction relay is stalled for the duration of the lock hold; legitimate peers cannot get transactions relayed or fetched. The attack repeats continuously. [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1483-1532)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();

        for tx_hash in tx_hashes
            .into_iter()
            .take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)
        {
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
                }
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
            }
        }

        // Check `unknown_tx_hashes`'s length after inserting the arrival `tx_hashes`
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
        }

        Status::ok()
    }
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L25-49)
```rust
    pub fn execute(self) -> Status {
        let state = self.relayer.shared().state();
        {
            let relay_transaction_hashes = self.message;
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
        }

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
