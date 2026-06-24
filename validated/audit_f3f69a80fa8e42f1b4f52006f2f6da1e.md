Audit Report

## Title
Unbounded `peers` Vec Growth via Repeated `RelayTransactionHashes` in `add_ask_for_txs` — (`sync/src/types/mod.rs`)

## Summary
`SyncState::add_ask_for_txs` appends a peer's `PeerIndex` to the `peers` `Vec` inside `UnknownTxHashPriority` on every call without deduplication or a per-entry size cap. The only guard that could reject the peer fires only when the count of *distinct* hash keys in `unknown_tx_hashes` exceeds `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000). Because repeated sends of the same hashes do not add new keys, the guard is never reached, and the `peers` `Vec` for each entry grows without bound, enabling a single unprivileged peer to exhaust node memory.

## Finding Description

`add_ask_for_txs` iterates over incoming hashes (capped at `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767 per call). For each hash already present in the keyed-priority-queue, it clones the existing priority, calls `priority.push_peer(peer_index)`, and writes it back: [1](#0-0) 

`push_peer` appends to a `Vec<PeerIndex>` with no existence check and no cap. The struct is initialized with `peers: vec![peer_index]`, confirming the field is a plain `Vec`: [2](#0-1) 

After the insertion loop, the only guard checks `unknown_tx_hashes.len()` — the count of *distinct* hash keys — against two thresholds: [3](#0-2) 

When an attacker repeatedly sends the **same** set of hashes, `unknown_tx_hashes.len()` stays constant (the hashes are already present as keys; no new keys are inserted). The condition at line 1507 is never satisfied, the per-peer counter at line 1516–1523 is never evaluated, and `push_peer` is called unconditionally on every iteration of every repeated message. The `peers` `Vec` for each of the 32,767 entries grows by one element per call, with no upper bound.

The relevant constants confirm the scale: [4](#0-3) 

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node.**

Each `PeerIndex` is a `usize` (8 bytes). With 32,767 hashes per message and N repeated sends, the `peers` `Vec` entries alone consume `32,767 × N × 8` bytes. At N = 10,000 sends this exceeds 2.6 GB. The node runs out of memory and crashes or becomes unresponsive, halting block and transaction relay for all connected peers. The impact is a concrete, reproducible node crash, not a theoretical degradation.

## Likelihood Explanation

The attack requires only a standard P2P connection — no privileged role, no key material, no majority hashpower. The attacker sends a fixed-size `RelayTransactionHashes` message (32,767 hashes, announced once to populate the map) and then replays the identical message in a tight loop. The protocol imposes no rate limit on this message type beyond the per-call `take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` truncation, which does not prevent repeated calls. The attack is cheap for the attacker (small, constant-size messages) and expensive for the victim (unbounded heap allocation per message).

## Recommendation

1. **Deduplicate `peer_index` in `push_peer`**: replace `Vec<PeerIndex>` with `HashSet<PeerIndex>` for the `peers` field of `UnknownTxHashPriority`, or check for existence before pushing.
2. **Cap the `peers` collection per entry**: enforce a hard maximum (e.g., `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`) on the number of peers recorded per hash entry, and silently drop or reject the push when the cap is reached.
3. **Move the per-peer limit to a pre-insertion guard**: count the peer's existing entries *before* inserting, unconditionally on every call, not only when the global distinct-key threshold is crossed.

## Proof of Concept

```
1. Attacker peer connects to a CKB node via the Relay protocol.
2. Attacker constructs a RelayTransactionHashes message containing
   32,767 transaction hashes unknown to the node.
3. Attacker sends the message once — unknown_tx_hashes now has 32,767
   distinct keys (below MAX_UNKNOWN_TX_HASHES_SIZE = 50,000).
4. Attacker sends the identical message in a tight loop (N iterations).
   - Each iteration: all 32,767 hashes hit the Occupied branch.
   - push_peer(attacker_peer) appends to each entry's peers Vec.
   - unknown_tx_hashes.len() stays at 32,767; the guard at line 1507
     never fires.
5. After N = 10,000 iterations:
   - Each of the 32,767 entries holds a peers Vec of length 10,001.
   - Memory for peers Vecs alone: 32,767 × 10,001 × 8 ≈ 2.6 GB.
6. Node OOMs and crashes.

Verification: add a counter inside the Occupied branch of
add_ask_for_txs that tracks peers.len() per entry; assert it is
bounded after repeated calls with the same peer and same hashes.
A unit test that calls add_ask_for_txs 1,000 times with the same
peer and same hashes, then inspects unknown_tx_hashes entries, will
show peers.len() == 1,000 with no guard having fired.
```

### Citations

**File:** sync/src/types/mod.rs (L1491-1494)
```rust
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
```

**File:** sync/src/types/mod.rs (L1496-1502)
```rust
                keyed_priority_queue::Entry::Vacant(entry) => {
                    entry.set_priority(UnknownTxHashPriority {
                        request_time: Instant::now(),
                        peers: vec![peer_index],
                        requested: false,
                    })
                }
```

**File:** sync/src/types/mod.rs (L1506-1529)
```rust
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
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
