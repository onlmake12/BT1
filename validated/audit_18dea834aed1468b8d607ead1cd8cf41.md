### Title
Unbounded `pending_get_block_proposals` State Inflation via Unauthenticated `GetBlockProposal` P2P Messages — (File: `sync/src/relayer/get_block_proposal_process.rs`)

---

### Summary

Any unprivileged P2P peer can send repeated `GetBlockProposal` relay messages containing crafted proposal IDs that do not exist in the local tx pool. These IDs are inserted into the shared `pending_get_block_proposals` DashMap without any per-peer or global size cap, allowing a peer to inflate this shared state structure unboundedly between drain intervals. The analog to the original report is exact: a function that updates a global state variable is callable by any unprivileged actor with no access restriction, and the variable can be inflated at will.

---

### Finding Description

**Root cause — missing size cap on `insert_get_block_proposals`:**

`GetBlockProposalProcess::execute()` in `sync/src/relayer/get_block_proposal_process.rs` handles the `GetBlockProposal` relay protocol message from any connected peer. It performs two checks:

1. A per-message count check against `max_block_proposals_limit * max_uncles_num` (≈ 3 000 on mainnet).
2. A deduplication check within a single message.

After those checks, proposals that are **not found in the local tx pool** are collected as `not_exist_proposals` and unconditionally inserted into the shared `pending_get_block_proposals` map:

```rust
// sync/src/relayer/get_block_proposal_process.rs  lines 68-77
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();

// Cache request, try process on timer
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```

`insert_get_block_proposals` in `sync/src/types/mod.rs` performs no size check whatsoever:

```rust
// sync/src/types/mod.rs  lines 1594-1601
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
```

`pending_get_block_proposals` is declared as an unbounded `DashMap`:

```rust
// sync/src/types/mod.rs  line 1330
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
```

The map is only drained periodically by `prune_tx_proposal_request` on a timer. Between drain cycles, a peer can insert an arbitrary number of entries.

**Contrast with the guarded path:**

The analogous `add_ask_for_txs` function (which populates `unknown_tx_hashes`) has explicit global and per-peer size caps and bans the peer when exceeded:

```rust
// sync/src/types/mod.rs  lines 1507-1528
if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
    || unknown_tx_hashes.len()
        >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
{
    ...
    if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
        return StatusCode::TooManyUnknownTransactions.into();
    }
    return Status::ignored();
}
```

No equivalent guard exists for `pending_get_block_proposals`.

**Exploit path:**

1. Attacker connects as a normal P2P peer (no privilege required).
2. Attacker repeatedly sends `GetBlockProposal` messages, each containing up to `max_block_proposals_limit × max_uncles_num` (≈ 3 000) distinct, crafted `ProposalShortId` values that are not present in the victim node's tx pool.
3. Each message passes the per-message count check and the deduplication check.
4. All ≈ 3 000 IDs per message are inserted into `pending_get_block_proposals` with no rejection.
5. The attacker repeats this at the maximum message rate the P2P layer allows, accumulating millions of entries before the next drain cycle.

---

### Impact Explanation

`pending_get_block_proposals` is a process-wide shared DashMap held in `SyncState`, which is `Arc`-shared across all sync and relay protocol handlers. Unbounded growth of this map causes:

- **Memory exhaustion**: Each `ProposalShortId` key is 10 bytes; with `HashSet<PeerIndex>` overhead, each entry costs ~100–200 bytes. At 3 000 proposals per message and thousands of messages per second, the map can consume gigabytes of RAM, crashing the node via OOM.
- **Lock contention / throughput degradation**: The drain in `prune_tx_proposal_request` clones and clears the entire map; a very large map makes this operation expensive, stalling the relayer timer loop.
- **Denial of service**: A single peer with a normal TCP connection can sustain this attack indefinitely, as there is no ban or rate-limit triggered by this code path.

---

### Likelihood Explanation

- **Attacker preconditions**: None beyond establishing a standard P2P connection. No keys, no stake, no special role.
- **Message rate**: The P2P layer imposes a message-size limit but no per-message-type rate limit for `GetBlockProposal`. A single peer can send thousands of messages per second.
- **Detection/mitigation**: No existing check bans or throttles a peer for inflating `pending_get_block_proposals`. The `TooManyUnknownTransactions` ban path covers only `unknown_tx_hashes`, not this map.
- **Reproducibility**: Deterministic — any peer sending crafted proposal IDs not in the pool will trigger the insertion path every time.

---

### Recommendation

Apply the same guard pattern used in `add_ask_for_txs`:

1. Track a per-peer insertion counter for `pending_get_block_proposals`.
2. After insertion, check whether the global map size or the per-peer contribution exceeds a configurable constant (e.g., `MAX_PENDING_GET_BLOCK_PROPOSALS_SIZE`).
3. If exceeded, return a `ProtocolMessageIsMalformed`-equivalent status and ban the peer for a short duration, mirroring the `TooManyUnknownTransactions` path.

---

### Proof of Concept

```
1. Connect to a CKB mainnet/testnet node as a standard P2P peer.
2. In a tight loop, send RelayV3 `GetBlockProposal` messages:
     block_hash = <any valid tip hash>
     proposals  = [random 10-byte ProposalShortId × 3000]  // none exist in pool
3. Each message passes:
     - count check  (3000 ≤ max_block_proposals_limit × max_uncles_num)
     - dedup check  (all IDs are distinct within the message)
4. All 3000 IDs are inserted into pending_get_block_proposals per message.
5. After N messages (N × 3000 entries), node RSS grows proportionally.
6. No ban is issued; the loop continues until the node OOMs or the drain
   cycle catches up (which itself becomes expensive at large map sizes).
```

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** sync/src/relayer/get_block_proposal_process.rs (L32-77)
```rust
    pub async fn execute(self) -> Status {
        let shared = self.relayer.shared();
        let message_len = self.message.proposals().len();
        {
            // The block proposal request is separate from uncles,
            // so here the limit is only used to calculate the maximum value of uncles
            let limit = shared.consensus().max_block_proposals_limit()
                * (shared.consensus().max_uncles_num() as u64);
            if message_len as u64 > limit {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "GetBlockProposal proposals count({message_len}) > consensus max_block_proposals_limit({limit})"
                ));
            }
        }

        let proposals: HashSet<packed::ProposalShortId> =
            self.message.proposals().to_entity().into_iter().collect();

        if proposals.len() != message_len {
            return StatusCode::RequestDuplicate.with_context("Request duplicate proposal");
        }

        let fetched_transactions = {
            let tx_pool = self.relayer.shared.shared().tx_pool_controller();
            let fetch_txs = tx_pool.fetch_txs(proposals.clone()).await;
            if let Err(e) = fetch_txs {
                debug_target!(
                    crate::LOG_TARGET_RELAY,
                    "relayer tx_pool_controller send fetch_txs error: {:?}",
                    e
                );
                return Status::ok();
            }
            fetch_txs.unwrap()
        };
        // Transactions that do not exist on this node
        let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
            .into_iter()
            .filter(|short_id| !fetched_transactions.contains_key(short_id))
            .collect();

        // Cache request, try process on timer
        self.relayer
            .shared()
            .state()
            .insert_get_block_proposals(self.peer, not_exist_proposals);
```

**File:** sync/src/types/mod.rs (L1330-1336)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,
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

**File:** sync/src/types/mod.rs (L1594-1601)
```rust
    pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
        for id in ids.into_iter() {
            self.pending_get_block_proposals
                .entry(id)
                .or_default()
                .insert(pi);
        }
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
