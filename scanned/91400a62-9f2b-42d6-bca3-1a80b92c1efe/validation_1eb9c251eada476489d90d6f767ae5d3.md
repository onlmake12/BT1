Now I have all the code I need. Let me trace the exact logic carefully.

### Title
`unknown_tx_hashes` Queue Grows Unbounded Beyond `MAX_UNKNOWN_TX_HASHES_SIZE` Due to Post-Insertion Check and Missing Disconnect Cleanup — (`sync/src/types/mod.rs`)

---

### Summary

`SyncState::add_ask_for_txs` inserts all peer-supplied hashes into the global `unknown_tx_hashes` `KeyedPriorityQueue` **before** checking the size limit. When the limit is exceeded the offending peer is banned, but the already-inserted entries are never removed — neither at ban time nor on disconnect. An attacker controlling N relay connections can therefore grow the queue to N × 32 767 entries, exhausting heap memory.

---

### Finding Description

The insertion loop in `add_ask_for_txs` unconditionally inserts up to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` (32 767) new entries per call: [1](#0-0) 

Only after the loop completes does the function test whether the queue has grown past `MAX_UNKNOWN_TX_HASHES_SIZE` (50 000): [2](#0-1) 

When the threshold is exceeded and the current peer's contribution reaches `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`, the function returns `StatusCode::TooManyUnknownTransactions`, which causes the peer to be banned. However, the entries that peer just inserted are **not removed**. The disconnect handler that fires when the ban takes effect also performs no cleanup of `unknown_tx_hashes`: [3](#0-2) 

The constants that define the limits are: [4](#0-3) 

The entry point is `TransactionHashesProcess::execute`, which calls `add_ask_for_txs` directly after a single per-message count check (≤ 32 767 hashes) and a `tx_filter` dedup pass: [5](#0-4) 

The relay handler skips all processing during IBD but otherwise accepts `RelayTransactionHashes` from any connected peer: [6](#0-5) 

---

### Impact Explanation

With N attacker-controlled relay connections, each sending one `RelayTransactionHashes` message containing 32 767 distinct hashes:

| Peers | Queue entries | Approx. heap (≈ 100 B/entry) |
|-------|--------------|------------------------------|
| 2 | 65 534 | ~6.5 MB |
| 10 | 327 670 | ~33 MB |
| 128 (`MAX_RELAY_PEERS`) | 4 194 176 | ~420 MB |

Each `UnknownTxHashPriority` value holds an `Instant`, a `Vec<PeerIndex>`, and a `bool`; each key is a 32-byte `Byte32`. The queue is protected by a single `Mutex`, so lock contention also degrades throughput while the attack is in progress. At the upper bound the node can OOM-crash or become unresponsive.

---

### Likelihood Explanation

The attacker only needs to open multiple inbound or outbound relay connections to a post-IBD node and send one maximum-size `RelayTransactionHashes` message per connection. No PoW, no keys, no privileged access. The per-peer rate limiter (30 msg/s) does not prevent a single large batch from being delivered. The existing integration test (`TooManyUnknownTransactions`) confirms the ban path fires, but it tests only a single peer and does not assert that the queue length stays ≤ 50 000 after the ban. [7](#0-6) 

---

### Recommendation

1. **Pre-insertion guard**: before inserting any hashes, check whether `unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE`; if so, reject immediately without inserting.
2. **Cleanup on disconnect/ban**: in `SyncState::disconnected`, iterate `unknown_tx_hashes` and remove entries whose `peers` list becomes empty after removing the disconnected peer index.
3. **Tighten the per-peer limit**: enforce the per-peer cap (`MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`) as a pre-check rather than a post-check.

---

### Proof of Concept

```
// Pseudocode – run against a post-IBD node
for peer_id in 0..10 {
    connect_relay_peer(node);
    send_relay_transaction_hashes(peer_id, unique_hashes(32767));
    // peer gets banned after insertion, but 32767 entries remain
}
// assert unknown_tx_hashes.len() == 327670  (>> 50000)
```

The existing test at `test/src/specs/relay/too_many_unknown_transactions.rs` can be extended: after the ban fires, read `unknown_tx_hashes.len()` and assert it is ≤ `MAX_UNKNOWN_TX_HASHES_SIZE`. That assertion will currently **fail**, confirming the invariant is broken.

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

**File:** sync/src/relayer/mod.rs (L816-818)
```rust
        if self.shared.active_chain().is_initial_block_download() {
            return;
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
