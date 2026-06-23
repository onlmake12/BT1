### Title
Unbounded `pending_get_block_proposals` DashMap Allows Any Peer to Exhaust Node Memory via Fake Proposal IDs — (`File: sync/src/types/mod.rs`)

### Summary
Any connected P2P peer can send repeated `GetBlockProposal` relay messages containing fabricated `ProposalShortId` values that do not exist in the local tx-pool. Each such ID is unconditionally inserted into the `pending_get_block_proposals` `DashMap` in `SyncState` with no size cap, allowing an attacker to grow this map without bound and exhaust node memory.

### Finding Description

`SyncState` in `sync/src/types/mod.rs` holds a `DashMap` for caching incoming block-proposal requests from peers:

```rust
pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
``` [1](#0-0) 

When a peer sends a `GetBlockProposal` relay message, `GetBlockProposalProcess::execute()` validates only that the count does not exceed `max_block_proposals_limit * max_uncles_num` and that there are no duplicates within a single message. Any proposal IDs that are absent from the local tx-pool are then inserted into `pending_get_block_proposals` via `insert_get_block_proposals()`:

```rust
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
``` [2](#0-1) 

There is no size limit, no per-peer quota, and no eviction policy on this map. The call site in `GetBlockProposalProcess::execute()` performs no further guard before writing: [3](#0-2) 

The map is only drained by `drain_get_block_proposals()`, which is called from `prune_tx_proposal_request()` on a periodic timer. Between timer ticks, the map accumulates all entries written by all peers with no bound. [4](#0-3) 

The per-message count limit is `max_block_proposals_limit * max_uncles_num`. On mainnet this is `1500 * 2 = 3000` unique `ProposalShortId` values (each 10 bytes) per message. An attacker sending a stream of such messages with distinct fabricated IDs can grow the map at ~30 KB per message with no upper bound.

### Impact Explanation

An attacker who maintains a single peer connection can continuously send `GetBlockProposal` messages containing up to 3000 unique fake proposal IDs per message. Because the IDs are not in the local tx-pool, they are all inserted into `pending_get_block_proposals`. The map grows without bound until the node runs out of memory and crashes (OOM), causing a full denial-of-service. The periodic drain only provides temporary relief; the attacker can immediately refill the map after each drain cycle.

### Likelihood Explanation

Any unprivileged peer that completes the CKB P2P handshake can send `GetBlockProposal` relay messages. No stake, fee, or special privilege is required. The only per-message validation is a count check and a within-message duplicate check: [5](#0-4) 

A single attacker node with a persistent connection can sustain the attack indefinitely at low cost, making the likelihood realistic for a motivated adversary.

### Recommendation

- **Short term**: Add a hard cap on the total number of entries in `pending_get_block_proposals` (e.g., `max_block_proposals_limit * max_uncles_num * MAX_PEERS`). When the cap is reached, reject or drop new insertions from the offending peer and consider banning it.
- **Short term**: Track per-peer insertion counts and apply a per-peer quota to prevent a single peer from monopolizing the map.
- **Long term**: Introduce a size-aware eviction policy (e.g., LRU or FIFO with a fixed capacity) for `pending_get_block_proposals`, consistent with how the orphan tx pool uses `DEFAULT_MAX_ORPHAN_TRANSACTIONS` and `limit_size()`. [6](#0-5) 

### Proof of Concept

1. Attacker establishes a peer connection to a CKB full node.
2. Attacker constructs `GetBlockProposal` relay messages, each containing 3000 unique, randomly generated `ProposalShortId` values (10 bytes each) that do not correspond to any real transaction.
3. Attacker sends these messages in a tight loop.
4. On the victim node, `GetBlockProposalProcess::execute()` passes the count check (`3000 <= 1500 * 2`), finds none of the IDs in the tx-pool, and calls `insert_get_block_proposals()` for all 3000 IDs per message.
5. `pending_get_block_proposals` grows by ~3000 entries per message. After ~33,000 messages the map holds ~100 million entries, consuming gigabytes of memory.
6. The periodic `drain_get_block_proposals()` call provides only a brief pause; the attacker immediately resumes filling the map.
7. The node process is killed by the OS OOM killer, completing the denial-of-service. [7](#0-6)

### Citations

**File:** sync/src/types/mod.rs (L1318-1341)
```rust
pub struct SyncState {
    /* Status irrelevant to peers */
    shared_best_header: RwLock<HeaderIndexView>,
    tx_filter: Mutex<TtlFilter<Byte32>>,

    // The priority is ordering by timestamp (reversed), means do not ask the tx before this timestamp (timeout).
    unknown_tx_hashes: Mutex<KeyedPriorityQueue<Byte32, UnknownTxHashPriority>>,

    /* Status relevant to peers */
    peers: Peers,

    /* Cached items which we had received but not completely process */
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
    pending_get_headers: RwLock<LruCache<(PeerIndex, Byte32), Instant>>,
    pending_compact_blocks: tokio::sync::Mutex<PendingCompactBlockMap>,

    /* In-flight items for which we request to peers, but not got the responses yet */
    inflight_proposals: DashMap<packed::ProposalShortId, BlockNumber>,
    inflight_blocks: RwLock<InflightBlocks>,

    /* cached for sending bulk */
    tx_relay_receiver: Receiver<TxVerificationResult>,
    min_chain_work: U256,
}
```

**File:** sync/src/types/mod.rs (L1586-1601)
```rust
    pub fn drain_get_block_proposals(
        &self,
    ) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
        let ret = self.pending_get_block_proposals.clone();
        self.pending_get_block_proposals.clear();
        ret
    }

    pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
        for id in ids.into_iter() {
            self.pending_get_block_proposals
                .entry(id)
                .or_default()
                .insert(pi);
        }
    }
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L35-52)
```rust
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
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L73-77)
```rust
        // Cache request, try process on timer
        self.relayer
            .shared()
            .state()
            .insert_get_block_proposals(self.peer, not_exist_proposals);
```

**File:** tx-pool/src/component/orphan.rs (L96-131)
```rust
    fn limit_size(&mut self) -> Vec<Byte32> {
        let now = ckb_systemtime::unix_time().as_secs();
        let expires: Vec<_> = self
            .entries
            .iter()
            .filter_map(|(id, entry)| {
                if entry.expires_at <= now {
                    Some(id)
                } else {
                    None
                }
            })
            .cloned()
            .collect();

        let mut evicted_txs = vec![];

        for id in expires {
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        while self.len() > DEFAULT_MAX_ORPHAN_TRANSACTIONS {
            // Evict a random orphan:
            let id = self.entries.keys().next().cloned().expect("bound checked");
            if let Some(entry) = self.remove_orphan_tx(&id) {
                evicted_txs.push(entry.tx.hash());
            }
        }

        if !evicted_txs.is_empty() {
            trace!("OrphanTxPool full, evicted {} tx", evicted_txs.len());
            self.shrink_to_fit();
        }
        evicted_txs
```
