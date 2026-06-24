The code matches all claims. Let me verify the guard logic and the `take` limit interaction:

All code claims verified against the actual repository. Every cited line matches exactly:

- `push_peer` at [1](#0-0)  is unconditional — no deduplication, no cap.
- The `Occupied` branch at [2](#0-1)  calls `push_peer` before any guard is evaluated.
- The guard at [3](#0-2)  checks `unknown_tx_hashes.len()` (distinct key count), not the sum of all `peers` Vec lengths.
- Constants confirmed at [4](#0-3) .
- `tx_filter` is never populated with unknown hashes — no `mark_as_known_tx` or equivalent insert path exists in the relay path, so repeated identical messages pass the filter every time. [5](#0-4) 

---

Audit Report

## Title
Unbounded `peers` Vec Growth via Repeated `RelayTransactionHashes` — (`sync/src/types/mod.rs`)

## Summary
`UnknownTxHashPriority::push_peer` unconditionally appends a `PeerIndex` to the `peers` Vec with no deduplication or length cap. The post-insertion guard in `add_ask_for_txs` checks only the number of distinct hash keys in `unknown_tx_hashes`, not the total size of all `peers` Vecs. A single peer sending the same K < 32767 unknown hashes repeatedly causes unbounded heap growth, leading to OOM and node crash.

## Finding Description
`push_peer` (`sync/src/types/mod.rs` L1291–1293) unconditionally calls `self.peers.push(peer_index)`. In `add_ask_for_txs` (L1483–1532), when a hash is already present in `unknown_tx_hashes`, the `Occupied` branch (L1491–1494) clones the priority, calls `push_peer`, and writes it back — before any guard is checked.

The post-insertion guard (L1507–1509) fires only when `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE (50000)` or `unknown_tx_hashes.len() >= peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER (32767)`. Both conditions measure the number of **distinct hash keys**, which is fixed at K after the first message. With K = 32766 and one connected peer: `32766 >= 50000` is false and `32766 >= 1 × 32767` is false. The guard never fires on any subsequent message.

The `tx_filter` in `execute()` (L38–46 of `transaction_hashes_process.rs`) only filters hashes that have been resolved and inserted into the filter. Unknown hashes are never added to `tx_filter`, so the same 32766 hashes pass the filter on every repeated message and reach `add_ask_for_txs` every time.

Each iteration of the attack appends 32766 `PeerIndex` entries (≈8 bytes each) across the 32766 `peers` Vecs. After M messages: heap growth ≈ 32766 × M × 8 bytes. At M = 10,000: ≈2.6 GB → OOM. The `unknown_tx_hashes` mutex is held for the entire insertion loop, so growing allocation also causes lock contention degrading all relay processing.

## Impact Explanation
A single unprivileged peer can crash a CKB node via OOM. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node"** (10001–15000 points). The crash is deterministic and requires no special privileges, no PoW, and no Sybil attack.

## Likelihood Explanation
The attacker needs only a single standard P2P connection. The `RelayTransactionHashes` message format is part of the normal relay protocol; hashes need not correspond to real transactions. The bypass value (K = 32766) is trivially derived from the public constants. There is no rate-limit on this message type and no mechanism to disconnect a peer that never delivers the requested transactions. The attack is repeatable indefinitely.

## Recommendation
1. **Deduplicate in `push_peer`**: before appending, check `if !self.peers.contains(&peer_index)`, or use a `HashSet` for `peers`.
2. **Cap `peers` per entry**: enforce `if self.peers.len() < MAX_PEERS_PER_HASH` before pushing.
3. **Guard on total peer-list size**: replace or augment the post-insertion check with a bound on the sum of all `priority.peers.len()` values, or maintain a running total updated on each `push_peer` call.
4. **Rate-limit `RelayTransactionHashes` per peer**: apply a token-bucket or message-rate cap at the protocol handler level.

## Proof of Concept
```rust
// Single peer, K=32766 hashes (one below MAX_RELAY_TXS_NUM_PER_BATCH)
let hashes: Vec<Byte32> = (0u32..32766).map(|i| {
    let mut buf = [0u8; 32];
    buf[..4].copy_from_slice(&i.to_le_bytes());
    Byte32::new(buf)
}).collect();

// First message: inserts 32766 Vacant entries, each peers=[peer_index]
// Guard: 32766 >= 50000 → false; 32766 >= 1*32767 → false → Status::ok()
send_relay_tx_hashes(&mut stream, &hashes).await;

loop {
    // Subsequent messages: all Occupied → push_peer called 32766 times
    // unknown_tx_hashes.len() stays 32766 → guard never fires
    // peers Vecs grow by 32766 entries per iteration (~262 KB/iter)
    send_relay_tx_hashes(&mut stream, &hashes).await;
    // After ~10,000 iterations: ~2.6 GB heap → OOM / node crash
}
```
A unit test can confirm the guard bypass by calling `add_ask_for_txs` in a loop with the same hashes from one peer and asserting that `priority.peers.len()` grows without bound while `unknown_tx_hashes.len()` remains constant at 32766.

### Citations

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1491-1494)
```rust
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
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
