The code confirms all three root causes claimed in the report. Let me verify the key details:

Audit Report

## Title
`unknown_tx_hashes` Queue Grows Unbounded Beyond `MAX_UNKNOWN_TX_HASHES_SIZE` Due to Post-Insertion Size Check and Missing Disconnect Cleanup — (File: `sync/src/types/mod.rs`)

## Summary

`SyncState::add_ask_for_txs` inserts all supplied hashes into `unknown_tx_hashes` before evaluating the size limit. When the limit is exceeded and the peer is banned, the already-inserted entries are never removed. The `disconnected` handler also performs no cleanup of `unknown_tx_hashes`. An attacker with N relay connections can grow the queue to N × 32,767 entries, exhausting heap memory and crashing the node.

## Finding Description

**Post-insertion size check:** The insertion loop at `sync/src/types/mod.rs` lines 1486–1504 unconditionally inserts all supplied hashes (up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` = 32,767) into `unknown_tx_hashes` before any size guard runs. [1](#0-0) 

Only after the loop does the function test whether the queue has exceeded `MAX_UNKNOWN_TX_HASHES_SIZE` (50,000). When the threshold is exceeded and the current peer's contribution reaches `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, the function returns `StatusCode::TooManyUnknownTransactions`, banning the peer — but the entries that peer just inserted are **not removed**. [2](#0-1) 

**No disconnect cleanup:** The `disconnected` handler only removes inflight blocks and calls `peers().disconnected(pi)`. It never touches `unknown_tx_hashes`. [3](#0-2) 

**Constants:** [4](#0-3) 

**Entry point:** `TransactionHashesProcess::execute` performs only a per-message count check (≤ 32,767) and a `tx_filter` dedup pass before calling `add_ask_for_txs` directly, with no pre-insertion queue size guard. [5](#0-4) 

**Attack sequence:**
- Peer 1 sends 32,767 unique hashes → inserted (32,767 entries, below 50,000 → no ban, no cleanup).
- Peer 2 sends 32,767 unique hashes → inserted (65,534 entries, ≥ 50,000 → peer 2 banned, but 65,534 entries remain).
- Peer N: queue = N × 32,767 entries, all retained indefinitely.

The existing integration test confirms the ban path fires but tests only a single peer and does not assert that the queue length stays ≤ 50,000 after the ban. [6](#0-5) 

## Impact Explanation

This maps to **High: Vulnerabilities which could easily crash a CKB node**. With a realistic number of relay peers, the queue can hold millions of entries. At roughly 80–100 bytes per entry (32-byte `Byte32` key + `Instant` + `Vec<PeerIndex>` + `bool` + allocator overhead), heap exhaustion and an OOM crash are the concrete outcome. The queue is protected by a single `Mutex`, so lock contention also degrades throughput during the attack.

## Likelihood Explanation

The attacker needs only to open multiple inbound relay connections to a post-IBD node and send one maximum-size `RelayTransactionHashes` message per connection. No proof-of-work, no keys, and no privileged access are required. The per-peer rate limiter does not prevent a single large batch. Banned peers can be replaced with new IP addresses, making the attack repeatable.

## Recommendation

1. **Pre-insertion guard**: in `add_ask_for_txs`, check `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE` before the insertion loop; if exceeded, reject immediately without inserting.
2. **Cleanup on disconnect/ban**: in `SyncState::disconnected`, iterate `unknown_tx_hashes` and remove entries whose `peers` list becomes empty after removing the disconnected `PeerIndex`.
3. **Tighten per-peer cap**: enforce `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` as a pre-check (count existing entries for this peer before inserting) rather than a post-check.

## Proof of Concept

```rust
// Extend test/src/specs/relay/too_many_unknown_transactions.rs:
// After the first peer is banned, connect additional peers and send
// 32767 unique hashes each. Then assert queue length <= MAX_UNKNOWN_TX_HASHES_SIZE.
// This assertion FAILS, confirming the invariant is broken.

for peer_id in 0..10 {
    connect_relay_peer(node);
    send_relay_transaction_hashes(peer_id, unique_hashes(32767));
    // peer gets banned after insertion, but 32767 entries remain
}
// state.unknown_tx_hashes().len() == 327670  (>> 50000)
assert!(state.unknown_tx_hashes().len() <= MAX_UNKNOWN_TX_HASHES_SIZE); // FAILS
```

### Citations

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

**File:** sync/src/types/mod.rs (L1607-1616)
```rust
    pub fn disconnected(&self, pi: PeerIndex) {
        let removed_inflight_blocks_count = self.write_inflight_blocks().remove_by_peer(pi);
        if removed_inflight_blocks_count > 0 {
            debug!(
                "disconnected {}, remove {} inflight blocks",
                pi, removed_inflight_blocks_count
            )
        }
        self.peers().disconnected(pi);
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

**File:** test/src/specs/relay/too_many_unknown_transactions.rs (L11-46)
```rust
impl Spec for TooManyUnknownTransactions {
    fn run(&self, nodes: &mut Vec<Node>) {
        let node0 = &nodes[0];
        let mut net = Net::new(
            self.name(),
            node0.consensus(),
            vec![SupportProtocols::Sync, SupportProtocols::RelayV3],
        );
        net.connect(node0);

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
    }
```
