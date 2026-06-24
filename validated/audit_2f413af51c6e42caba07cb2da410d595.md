All six code claims check out against the actual source. Let me verify the constants and the guard logic precisely before rendering a verdict.

**Verified facts:**

- `push_peer` (L1291-1293): unconditional `self.peers.push(peer_index)` — no dedup, no cap. [1](#0-0) 
- `add_ask_for_txs` acquires the mutex at L1484 and holds it for the entire function body, including the nested loop at L1516-1523. [2](#0-1) 
- The guard at L1507-1509 checks only `unknown_tx_hashes.len()` (distinct key count), not any `peers` vector length. [3](#0-2) 
- `MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`, `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = MAX_RELAY_TXS_NUM_PER_BATCH = 32,767`. [4](#0-3) 
- `tx_filter` only contains hashes of received/processed transactions; fabricated hashes pass through every time. [5](#0-4) 
- The only per-message check is the count cap `> MAX_RELAY_TXS_NUM_PER_BATCH`; no per-peer frequency limit exists. [6](#0-5) 

The attack path is fully reachable: peer A repeatedly re-announces the same 25,000 fabricated hashes K times, growing each entry's `peers` vector to length K while `unknown_tx_hashes.len()` stays at 25,000 (guard never fires). Peer B then announces 25,000 different fabricated hashes, pushing the key count to 50,000 and triggering the guard. The nested loop then executes O(50,000 × K) iterations under the mutex. The existing integration test `TooManyUnknownTransactions` only covers the single-peer unique-hash path and does not protect against this vector. [7](#0-6) 

---

Audit Report

## Title
Unbounded `peers` Vector Growth in `UnknownTxHashPriority::push_peer` Enables CPU Exhaustion via Nested Iteration — (File: sync/src/types/mod.rs)

## Summary
`push_peer` appends a `PeerIndex` to an internal `Vec<PeerIndex>` with no deduplication and no size cap. A malicious peer can repeatedly re-announce the same fabricated transaction hashes via `RelayTransactionHashes`, inflating each entry's `peers` vector without bound while the key-count guard never fires. A second peer then triggers the guard, causing a nested O(entries × peers_per_entry) loop to execute while holding the `unknown_tx_hashes` mutex, blocking all relay operations on the victim node.

## Finding Description
**Root cause — `push_peer` has no deduplication or size limit:**

`push_peer` at `sync/src/types/mod.rs` L1291–1293 unconditionally appends:
```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    self.peers.push(peer_index);
}
```
It is called from `add_ask_for_txs` at L1491–1494 whenever a tx hash already exists in the map as an occupied entry.

**The size guard is bypassed by repeated announcements of the same hashes:**

The guard at L1507–1509 checks only `unknown_tx_hashes.len()` — the number of distinct keys — not the length of any `peers` vector:
```rust
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50 000
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER  // peers × 32 767
```
Re-announcing the same 25,000 hashes does not add new keys, so `unknown_tx_hashes.len()` stays at 25,000 and the guard never fires. The `peers` vector for each entry grows by 1 on every re-announcement.

**The `tx_filter` pre-filter does not block the attack:**

`transaction_hashes_process.rs` L38–47 filters out hashes already present in `tx_filter` (the set of *received/processed* transactions). Fabricated hashes that have never been received are never added to `tx_filter`, so they pass the filter on every re-announcement and reach `add_ask_for_txs` each time.

**Expensive nested loop triggered under the mutex:**

The mutex is acquired at L1484 and held for the entire function. When the guard fires (peer B pushes the total key count to 50,000), the code at L1516–1523 executes while still holding the lock:
```rust
for (_hash, priority) in unknown_tx_hashes.iter() {   // O(50 000)
    for peer in priority.peers.iter() {                // O(K per entry)
        if *peer == peer_index { peer_unknown_counter += 1; }
    }
}
```
Total work is O(entries × max_peers_per_entry). With pre-inflated `peers` vectors of length K across 25,000 entries, this is O(25,000 × K) — unbounded in K. All other callers that acquire `unknown_tx_hashes` (`pop_ask_for_txs`, `mark_as_known_txs`) are blocked for the duration.

**No per-peer rate limit exists on `RelayTransactionHashes`:**

The only check in `transaction_hashes_process.rs` is a per-message count cap (`> MAX_RELAY_TXS_NUM_PER_BATCH = 32,767`). There is no limit on how frequently a peer may send this message type.

## Impact Explanation
The attack stalls the `unknown_tx_hashes` mutex on the victim node, blocking transaction relay operations (`pop_ask_for_txs`, `mark_as_known_txs`) for all connected peers during the lock-hold period. Applied cheaply and repeatedly to multiple CKB nodes simultaneously, this constitutes **CKB network congestion with few costs**, matching the **High (10001–15000 points)** impact class.

## Likelihood Explanation
- Requires only two standard peer connections; no keys, stake, or privileged role.
- `RelayTransactionHashes` is accepted from any connected peer by default.
- No per-peer rate limit exists on this message type.
- Each attack message is small (~800 KB for 25,000 hashes × 32 bytes) and carries no on-chain cost.
- The attack is repeatable: after the mutex is released, the attacker can re-inflate and re-trigger continuously.
- Effective against any CKB node accepting inbound relay connections (the default configuration).

## Recommendation
1. **Deduplicate in `push_peer`:** Replace `Vec<PeerIndex>` with `HashSet<PeerIndex>`, or check for membership before appending:
```rust
pub fn push_peer(&mut self, peer_index: PeerIndex) {
    if !self.peers.contains(&peer_index) {
        self.peers.push(peer_index);
    }
}
```
2. **Cap the `peers` vector:** Enforce a hard maximum equal to the maximum number of connected peers so the vector cannot grow beyond a known bound regardless of deduplication logic.
3. **Eliminate the O(entries × peers) scan:** Maintain an explicit `HashMap<PeerIndex, usize>` per-peer counter that is incremented/decremented as entries are added or removed, avoiding the nested loop entirely.

## Proof of Concept
1. Connect two peers A and B to the victim CKB node using the standard Relay v3 protocol.
2. From peer A, send K = 10,000 repeated `RelayTransactionHashes` messages each containing the same 25,000 fabricated (non-existent) tx hashes. Each call to `add_ask_for_txs` finds existing entries and calls `push_peer(A)` 25,000 times, growing each entry's `peers` vector by 1. The guard never fires because `unknown_tx_hashes.len()` remains 25,000 (< 50,000).
3. From peer B, send one `RelayTransactionHashes` message with 25,000 *different* fabricated tx hashes. This pushes `unknown_tx_hashes.len()` to 50,000, triggering the guard at L1507.
4. The nested loop at L1516–1523 executes ~250 million iterations (25,000 entries × K peers + 25,000 entries × 1 peer) while holding the `unknown_tx_hashes` mutex, blocking all relay operations on the victim node.
5. **Expected outcome:** `pop_ask_for_txs` and `mark_as_known_txs` are stalled for the duration; legitimate peers cannot get transactions relayed or fetched. The attack is repeatable continuously.

### Citations

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1483-1484)
```rust
    pub fn add_ask_for_txs(&self, peer_index: PeerIndex, tx_hashes: Vec<Byte32>) -> Status {
        let mut unknown_tx_hashes = self.unknown_tx_hashes.lock();
```

**File:** sync/src/types/mod.rs (L1507-1509)
```rust
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```

**File:** sync/src/relayer/transaction_hashes_process.rs (L29-35)
```rust
            if relay_transaction_hashes.tx_hashes().len() > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({}) > MAX_RELAY_TXS_NUM_PER_BATCH({})",
                    relay_transaction_hashes.tx_hashes().len(),
                    MAX_RELAY_TXS_NUM_PER_BATCH,
                ));
            }
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

**File:** test/src/specs/relay/too_many_unknown_transactions.rs (L21-45)
```rust
        // Send `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` transactions with a same input
        let input = gen_spendable(node0, 1)[0].to_owned();
        let tx_template = always_success_transaction(node0, &input);
        let txs = {
            (0..MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER).map(|i| {
                let since = since_from_absolute_timestamp(i as u64);
                tx_template
                    .as_advanced_builder()
                    .set_inputs(vec![CellInput::new(input.out_point.clone(), since)])
                    .build()
            })
        };
        let tx_hashes = txs.map(|tx| tx.hash()).collect::<Vec<_>>();
        assert!(MAX_RELAY_TXS_NUM_PER_BATCH >= tx_hashes.len());
        net.send(
            node0,
            SupportProtocols::RelayV3,
            build_relay_tx_hashes(&tx_hashes),
        );

        let banned = wait_until(60, || node0.rpc_client().get_banned_addresses().len() == 1);
        assert!(
            banned,
            "NetController should be banned cause TooManyUnknownTransactions"
        );
```
