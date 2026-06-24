All code references check out against the actual source. Let me verify one additional detail about rate limiting before concluding.

Audit Report

## Title
Unbounded `peers` Vec Growth via Duplicate Hashes in `RelayTransactionHashes` — (`sync/src/relayer/transaction_hashes_process.rs`)

## Summary
`execute()` accepts up to 32,767 hashes per `RelayTransactionHashes` message without intra-message deduplication. Because `add_ask_for_txs` processes all items and `push_peer` has no length cap, a single message containing 32,767 copies of one unknown hash causes 32,766 unconditional `push_peer` calls on the same `UnknownTxHashPriority` entry. Repeated messages grow the `peers` Vec without bound, enabling heap exhaustion and OOM termination of the target node.

## Finding Description

**Entry point — count check uses `>`, not `>=`.** [1](#0-0) 
A message with exactly 32,767 hashes passes the guard.

**`tx_filter` does not deduplicate within a message.** [2](#0-1) 
The filter only removes hashes already in the verified-tx set. Repeated occurrences of the same never-seen hash all pass through.

**`add_ask_for_txs` processes all 32,767 items.** [3](#0-2) 
`.take(MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER)` equals `.take(32767)`, so nothing is dropped. The first occurrence hits `Vacant` and creates the entry; occurrences 2–32,767 hit `Occupied` and call `push_peer` unconditionally.

**`push_peer` has no bound.** [4](#0-3) 

**Post-insert overflow check is blind to `peers` Vec length.** [5](#0-4) 
The guard checks `unknown_tx_hashes.len()` — the count of *distinct* hashes in the map. With 32,767 identical hashes only one distinct entry exists (`len == 1`), far below `MAX_UNKNOWN_TX_HASHES_SIZE = 50000`. The check never fires.

**Drain rate is one peer per 30-second cycle.** [6](#0-5) 
`next_request_peer()` removes at most one `PeerIndex` per scheduling cycle via `swap_remove(0)`, governed by `RETRY_ASK_TX_TIMEOUT_INCREASE = 30s`. [7](#0-6) 

**Constants confirm the attack parameters.** [8](#0-7) 
`MAX_RELAY_TXS_NUM_PER_BATCH = MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32767`.

## Impact Explanation
Each `PeerIndex` is a small integer (4–8 bytes). One message with 32,767 duplicate hashes inserts 32,767 `PeerIndex` values into a single `Vec` (~256 KiB). The fake hash never enters `tx_filter` unless the corresponding transaction is verified, so the entry is never removed. Subsequent messages each add another 32,767 entries (all 32,767 occurrences now hit `Occupied`). At one message per second a single peer forces ~256 KiB/s of unreclaimable heap growth, leading to OOM termination of the node. This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**. [9](#0-8) 

## Likelihood Explanation
The exploit requires only a standard P2P connection — no proof-of-work, no keys, no privileged role. The attacker needs one hash absent from `tx_filter`, trivially satisfied by any random 32-byte value. The attack is indefinitely repeatable because the fake hash never enters `tx_filter` without transaction verification. No rate limiting on `RelayTransactionHashes` messages was found in the relayer. [10](#0-9) 

## Recommendation

1. **Deduplicate within `execute()`** before passing hashes to `add_ask_for_txs`:
   ```rust
   let tx_hashes: Vec<_> = {
       let mut tx_filter = state.tx_filter();
       tx_filter.remove_expired();
       self.message
           .tx_hashes()
           .iter()
           .map(|x| x.to_entity())
           .filter(|tx_hash| !tx_filter.contains(tx_hash))
           .collect::<HashSet<_>>()
           .into_iter()
           .collect()
   };
   ```

2. **Cap the `peers` Vec in `push_peer`** — e.g., refuse to append beyond a small constant:
   ```rust
   pub fn push_peer(&mut self, peer_index: PeerIndex) {
       if self.peers.len() < MAX_PEERS_PER_UNKNOWN_TX {
           self.peers.push(peer_index);
       }
   }
   ```

Both fixes are needed: deduplication prevents intra-message amplification; the cap prevents cross-message accumulation. [4](#0-3) 

## Proof of Concept

```rust
// 1. Establish a standard P2P connection to the target node.
// 2. Construct a RelayTransactionHashes with 32767 copies of one unknown hash:
let fake_hash = Byte32::new([0xAB; 32]);
let hashes: Vec<Byte32> = std::iter::repeat(fake_hash.clone()).take(32767).collect();
let msg = packed::RelayTransactionHashes::new_builder()
    .tx_hashes(hashes.pack())
    .build();

// 3. Send the message. execute() passes the count check (32767 == MAX, not >).
//    tx_filter does not contain fake_hash, so all 32767 pass the filter.
//    add_ask_for_txs: occurrence 1 → Vacant (peers: vec![peer_index]);
//                    occurrences 2–32767 → Occupied → push_peer() × 32766.

let state = relayer.shared().state();
state.add_ask_for_txs(peer_index, vec![fake_hash.clone(); 32767]);

let guard = state.unknown_tx_hashes();
let priority = guard.get(&fake_hash).unwrap();
assert_eq!(priority.peers.len(), 32767); // Vec grows unboundedly

// 4. Repeat the message every second.
//    Each subsequent message: all 32767 occurrences hit Occupied → 32767 push_peer calls.
//    Net growth: ~32767 PeerIndex values/second (~256 KiB/s) with no upper bound.
//    Drain: 1 entry per 30-second cycle — negligible.
// 5. Node OOMs and terminates.
```

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L25-50)
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
    }
```

**File:** sync/src/types/mod.rs (L1256-1261)
```rust
#[derive(Eq, PartialEq, Clone)]
pub struct UnknownTxHashPriority {
    request_time: Instant,
    peers: Vec<PeerIndex>,
    requested: bool,
}
```

**File:** sync/src/types/mod.rs (L1276-1289)
```rust
    pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
        if self.requested {
            if self.peers.len() > 1 {
                self.request_time = Instant::now();
                self.peers.swap_remove(0);
                self.peers.first().cloned()
            } else {
                None
            }
        } else {
            self.requested = true;
            self.peers.first().cloned()
        }
    }
```

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1486-1504)
```rust
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
```

**File:** sync/src/types/mod.rs (L1507-1529)
```rust
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

**File:** util/constant/src/sync.rs (L57-57)
```rust
pub const RETRY_ASK_TX_TIMEOUT_INCREASE: Duration = Duration::from_secs(30);
```

**File:** util/constant/src/sync.rs (L68-72)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
