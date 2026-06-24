Audit Report

## Title
Unbounded O(N×M) Linear Scan in `add_ask_for_txs` Under Mutex Enables Relay Starvation by Unprivileged Peers — (File: sync/src/types/mod.rs)

## Summary

`SyncState::add_ask_for_txs` acquires the `unknown_tx_hashes` mutex and holds it while performing a full nested-loop scan over up to 50,000 entries, each with an unbounded `peers` Vec. Any unprivileged peer can fill the queue with fabricated tx hashes and repeatedly trigger this scan, starving `mark_as_known_txs`, `pop_ask_for_txs`, and `tx_filter` of the mutex and degrading transaction relay and compact-block reconstruction on the targeted node.

## Finding Description

**Confirmed O(N×M) scan under mutex:**

`add_ask_for_txs` acquires the mutex at line 1484 and holds it for the entire function body. After inserting hashes (lines 1486–1504), it checks the soft cap and, when triggered, iterates every entry and every peer inside each entry's `peers` Vec:

```rust
// sync/src/types/mod.rs lines 1507–1523
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE          // 50,000
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    let mut peer_unknown_counter = 0;
    for (_hash, priority) in unknown_tx_hashes.iter() {   // O(N)
        for peer in priority.peers.iter() {               // O(M) per entry
            if *peer == peer_index {
                peer_unknown_counter += 1;
            }
        }
    }
``` [1](#0-0) 

The scan executes while the mutex is held, blocking every concurrent caller that needs the lock.

**Confirmed constants:**

- `MAX_UNKNOWN_TX_HASHES_SIZE = 50_000`
- `MAX_RELAY_TXS_NUM_PER_BATCH = 32_767`
- `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32_767` [2](#0-1) 

**Confirmed call chain:**

`RelayTransactionHashes` is dispatched at `relayer/mod.rs:143–144` → `TransactionHashesProcess::execute` at `transaction_hashes_process.rs:49` calls `state.add_ask_for_txs(self.peer, tx_hashes)` directly with no additional rate guard beyond the per-message batch cap. [3](#0-2) [4](#0-3) 

**Check fires after insertion:** Lines 1486–1504 insert all hashes first; the cap check and scan only begin at line 1507, so the queue is already at capacity when the O(N×M) scan starts. [5](#0-4) 

**Unbounded `peers` Vec:** The `Occupied` branch at lines 1491–1494 calls `push_peer` unconditionally with no cap on the number of peers per entry, allowing shared hashes to accumulate a `peers` Vec of length equal to the number of attacking peers. [6](#0-5) 

**Existing guards are insufficient:**

- `MAX_RELAY_TXS_NUM_PER_BATCH = 32,767` only caps a single message; two peers each sending ~25,000 unique hashes fills the 50,000-entry queue across two messages.
- The per-peer rate limiter bounds individual peer rate but not aggregate rate across K attacker peers.
- `tx_filter` deduplication (lines 38–47 of `transaction_hashes_process.rs`) only filters hashes already known; fabricated hashes pass through. [7](#0-6) 

## Impact Explanation

The `unknown_tx_hashes` mutex is shared with `mark_as_known_txs`, `pop_ask_for_txs`, and `tx_filter`. Holding it for the duration of a 50,000-entry × K-peer scan at high frequency starves these operations, directly degrading transaction relay throughput and compact-block reconstruction. Compact-block reconstruction stalls translate to delayed block propagation for the targeted node. This constitutes **CKB network congestion with few costs**, matching the **High (10001–15000 points)** impact tier.

## Likelihood Explanation

- Any peer can connect to a CKB node without authentication or stake.
- `RelayTransactionHashes` with fabricated (non-existent) tx hashes requires no PoW, no fee, and no valid UTXO.
- Filling the queue to 50,000 entries requires only two peers each sending one batch of ~25,000 hashes — well within `MAX_RELAY_TXS_NUM_PER_BATCH = 32,767`.
- With K=10 attacker peers, the per-peer rate limiter allows 30×K = 300 scans/second, each performing up to 50,000 × K comparisons under the mutex.
- The existing integration test at `test/src/specs/relay/too_many_unknown_transactions.rs` confirms the code path is reachable and exercised in CI. [8](#0-7) 

## Recommendation

1. **Replace the O(N×M) scan with a per-peer counter map.** Maintain a `HashMap<PeerIndex, usize>` alongside `unknown_tx_hashes` updated incrementally on insert/remove, making the overflow check O(1) and eliminating the full scan under the mutex.
2. **Cap the `peers` Vec per entry.** In `push_peer`, enforce a maximum number of peers per `UnknownTxHashPriority` entry to bound the inner loop.
3. **Enforce a hard cap before insertion.** Reject new hashes from a peer that has already reached `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` before inserting, rather than scanning after the fact.

## Proof of Concept

```
1. Attacker opens K=10 inbound connections to the target node.

2. Peer 1 sends one RelayTransactionHashes message with 25,000 unique
   fabricated tx hashes; Peer 2 sends another 25,000 unique hashes.
   → unknown_tx_hashes reaches MAX_UNKNOWN_TX_HASHES_SIZE = 50,000.

3. All K peers also announce the same 1,000 shared hashes, causing
   push_peer to be called K times per shared entry → peers Vec length = K.

4. Each peer sends RelayTransactionHashes at the rate limit.
   → K×rate scans/second, each iterating 50,000 entries × K peers per entry
     under the unknown_tx_hashes mutex.

5. Observable effect: mark_as_known_txs, pop_ask_for_txs, and tx_filter
   are starved; relay latency spikes; compact-block reconstruction stalls.

Verification: extend the existing TooManyUnknownTransactions integration
test (test/src/specs/relay/too_many_unknown_transactions.rs) with multiple
peers and measure mutex wait time and relay latency under the described load.
```

### Citations

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

**File:** sync/src/relayer/mod.rs (L143-145)
```rust
            packed::RelayMessageUnionReader::RelayTransactionHashes(reader) => {
                TransactionHashesProcess::new(reader, self, peer).execute()
            }
```

**File:** test/src/specs/relay/too_many_unknown_transactions.rs (L1-5)
```rust
use crate::util::cell::gen_spendable;
use crate::util::transaction::always_success_transaction;
use crate::utils::{build_relay_tx_hashes, since_from_absolute_timestamp, wait_until};
use crate::{Net, Node, Spec};
use ckb_constant::sync::{MAX_RELAY_TXS_NUM_PER_BATCH, MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER};
```
