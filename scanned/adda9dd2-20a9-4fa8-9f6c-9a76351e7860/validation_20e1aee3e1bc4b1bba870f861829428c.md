### Title
O(n×m) Nested Loop in `add_ask_for_txs` Triggered by Malicious Peer Flooding `RelayTransactionHashes` - (File: sync/src/types/mod.rs)

### Summary

When the `unknown_tx_hashes` queue is full, every call to `add_ask_for_txs` executes a nested loop that iterates over every entry in the queue and, for each entry, iterates over every peer that announced that hash. A malicious peer can keep the queue saturated by continuously sending `RelayTransactionHashes` P2P messages, causing repeated O(n×m) CPU work on the relayer's message-processing thread with no per-call cost to the attacker beyond sending cheap P2P messages.

### Finding Description

In `sync/src/types/mod.rs`, the function `add_ask_for_txs` is invoked by the relayer each time a peer sends a `RelayTransactionHashes` message. After inserting the announced hashes, the function checks whether the global `unknown_tx_hashes` queue has reached its capacity limit: [1](#0-0) 

When the capacity condition is met, the code counts how many entries in the entire queue belong to the calling peer via a nested loop:

```rust
for (_hash, priority) in unknown_tx_hashes.iter() {   // O(n) — all queue entries
    for peer in priority.peers.iter() {                // O(m) — all peers per entry
        if *peer == peer_index {
            peer_unknown_counter += 1;
        }
    }
}
```

The outer loop visits every entry in `unknown_tx_hashes` (bounded by `MAX_UNKNOWN_TX_HASHES_SIZE`). The inner loop visits every `PeerIndex` stored in each entry's `peers` vector. Because multiple peers can announce the same hash (via `push_peer` on the `Occupied` branch at line 1492–1494), the `peers` vector per entry is unbounded in the number of distinct peers. The total work per call is O(total_entries × avg_peers_per_entry). [2](#0-1) 

The attacker-controlled entry path is:

1. Attacker connects as a normal P2P peer (no privilege required).
2. Attacker sends `RelayTransactionHashes` messages containing `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` fresh (never-seen) tx hashes per message, rapidly filling the queue.
3. Once the queue is at capacity, every subsequent `RelayTransactionHashes` message from any peer (including the attacker) triggers the full nested scan.
4. The attacker repeats step 2 continuously; each message is cheap to produce (just 32-byte hashes) but forces an O(n×m) scan on the node's relayer thread.

The call site in the relayer: [3](#0-2) 

The constants governing queue size are defined in: [4](#0-3) 

### Impact Explanation

**Impact: Medium.** The relayer message-processing loop is a shared, synchronous resource. Sustained O(n×m) scans on every incoming `RelayTransactionHashes` message delay processing of all other relay messages (compact blocks, transaction propagation), degrading block and transaction relay throughput. Under sustained attack this can cause the node to fall behind the chain tip, miss compact block reconstructions, and degrade mempool admission — all without crashing the node. This matches the "service unavailability or severe degradation under realistic attacker input" criterion.

### Likelihood Explanation

**Likelihood: Medium.** Any unprivileged peer can send `RelayTransactionHashes` messages. The attacker only needs to maintain a single connection and send messages at a moderate rate. The queue fills quickly because `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` hashes can be announced per message. No special knowledge, keys, or majority hashpower is required.

### Recommendation

Replace the linear scan with an O(1) per-peer counter maintained as a side-map (e.g., `HashMap<PeerIndex, usize>`) that is incremented on insert and decremented on eviction. This eliminates the nested loop entirely. Alternatively, deduplicate `priority.peers` on insertion so the inner vector cannot grow beyond the number of connected peers, bounding the inner loop.

### Proof of Concept

1. Connect to a CKB node as a relay peer supporting `RelayV3`.
2. Continuously send `RelayTransactionHashes` messages each containing `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` distinct, never-seen tx hashes (random 32-byte values suffice since the node cannot verify existence before queuing).
3. Once the queue reaches `MAX_UNKNOWN_TX_HASHES_SIZE`, observe via node metrics or timing that each subsequent relay message takes significantly longer to process.
4. Confirm that block relay latency increases and the node begins to lag behind the chain tip.

The existing integration test at `test/src/specs/relay/too_many_unknown_transactions.rs` demonstrates the queue-filling path; the nested scan at lines 1517–1523 executes on every message once that state is reached. [5](#0-4)

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

**File:** sync/src/relayer/transaction_hashes_process.rs (L1-99)
```rust
use crate::relayer::{MAX_RELAY_TXS_NUM_PER_BATCH, Relayer};
use crate::{Status, StatusCode};
use ckb_network::PeerIndex;
use ckb_types::{packed, prelude::*};

pub struct TransactionHashesProcess<'a> {
    message: packed::RelayTransactionHashesReader<'a>,
    relayer: &'a Relayer,
    peer: PeerIndex,
}

impl<'a> TransactionHashesProcess<'a> {
    pub fn new(
        message: packed::RelayTransactionHashesReader<'a>,
        relayer: &'a Relayer,
        peer: PeerIndex,
    ) -> Self {
        TransactionHashesProcess {
            message,
            relayer,
            peer,
        }
    }

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
}


```

**File:** util/constant/src/sync.rs (L1-30)
```rust
use std::time::Duration;

/// The default init download block interval is 24 hours
/// If the time of the local highest block is within this range, exit the ibd state
pub const MAX_TIP_AGE: u64 = 24 * 60 * 60 * 1000;

/// Default max get header response length, if it is greater than this value, the message will be ignored
pub const MAX_HEADERS_LEN: usize = 2_000;

// The default number of download blocks that can be requested at one time
/* About Download Scheduler */

/// ckb2021 edition new limit
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
/// The point at which the scheduler adjusts the number of tasks, by default one adjustment per 512 blocks.
pub const CHECK_POINT_WINDOW: u64 = (MAX_BLOCKS_IN_TRANSIT_PER_PEER * 4) as u64;

/// Inspect the headers downloading every 2 minutes
pub const HEADERS_DOWNLOAD_INSPECT_WINDOW: u64 = 2 * 60 * 1000;
/// Global Average Speed
//      Expect 300 KiB/second
//          = 1600 headers/second (300*1024/192)
//          = 96000 headers/minute (1600*60)
//          = 11.11 days-in-blockchain/minute-in-reality (96000*10/60/60/24)
//      => Sync 1 year headers in blockchain will be in 32.85 minutes (365/11.11) in reality
pub const HEADERS_DOWNLOAD_HEADERS_PER_SECOND: u64 = 1600;
/// Acceptable Lowest Instantaneous Speed: 75.0 KiB/second (300/4)
pub const HEADERS_DOWNLOAD_TOLERABLE_BIAS_FOR_SINGLE_SAMPLE: u64 = 4;
```

**File:** test/src/specs/relay/too_many_unknown_transactions.rs (L1-46)
```rust
use crate::util::cell::gen_spendable;
use crate::util::transaction::always_success_transaction;
use crate::utils::{build_relay_tx_hashes, since_from_absolute_timestamp, wait_until};
use crate::{Net, Node, Spec};
use ckb_constant::sync::{MAX_RELAY_TXS_NUM_PER_BATCH, MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER};
use ckb_network::SupportProtocols;
use ckb_types::packed::CellInput;

pub struct TooManyUnknownTransactions;

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
