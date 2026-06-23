### Title
Unbounded Growth of `pending_get_block_proposals` via Unauthenticated Peer `GetBlockProposal` Messages — (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

---

### Summary

Any connected peer can send unlimited `GetBlockProposal` relay messages containing proposal IDs that do not exist in the local tx-pool. Each such message causes entries to be inserted into the node-global `pending_get_block_proposals` DashMap with no size cap. The map grows unboundedly in memory until the periodic timer drains it by cloning the entire structure. A sustained attacker can exhaust node memory or make the drain operation prohibitively expensive, causing a denial-of-service.

---

### Finding Description

`SyncState` holds a global, unbounded `DashMap`:

```rust
pending_get_block_proposals: DashMap::new(),
``` [1](#0-0) 

The insertion function has no size guard:

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

`GetBlockProposalProcess::execute` calls this with every proposal ID that is absent from the tx-pool, after only checking that a single message does not exceed `max_block_proposals_limit * max_uncles_num` (≈ 3 000 IDs per message):

```rust
// Cache request, try process on timer
self.relayer
    .shared()
    .state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
``` [3](#0-2) 

There is no per-peer rate limit, no total-map size cap, and no deduplication across messages (different proposal IDs each time bypass the DashMap key deduplication). The map is only consumed by `drain_get_block_proposals`, which **clones the entire DashMap** before clearing it:

```rust
pub fn drain_get_block_proposals(
    &self,
) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
    let ret = self.pending_get_block_proposals.clone();
    self.pending_get_block_proposals.clear();
    ret
}
``` [4](#0-3) 

The contrast is stark: `unknown_tx_hashes` has explicit soft limits (`MAX_UNKNOWN_TX_HASHES_SIZE = 50 000`, `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32 767`), but `pending_get_block_proposals` has none. [5](#0-4) 

---

### Impact Explanation

An attacker who controls one or more connected peers can:

1. Continuously send `GetBlockProposal` relay messages, each carrying up to ≈ 3 000 fresh random `ProposalShortId` values (10 bytes each) that are guaranteed not to be in the tx-pool.
2. Each message inserts up to 3 000 new entries into `pending_get_block_proposals` with zero cost beyond the network round-trip.
3. Between timer ticks the map grows without bound. At millions of entries the `DashMap::clone()` inside `drain_get_block_proposals` becomes an O(n) memory allocation that can exhaust heap or stall the async timer task, blocking proposal-relay processing for all peers.
4. Sustained flooding keeps the map large across every drain cycle, producing a persistent memory-exhaustion / liveness DOS on the relay subsystem.

---

### Likelihood Explanation

- **No privilege required**: `GetBlockProposal` is an ordinary relay-protocol message; any peer that completes the handshake can send it.
- **Low cost per entry**: The attacker only needs to generate random 10-byte IDs; no PoW, no valid transaction, no fee.
- **No per-message rate limit**: The code path contains only a per-message length check, not a per-peer or global frequency limit.
- **Amplification**: A single peer sending one message per second at 3 000 IDs/message inserts 180 000 entries/minute. Multiple peers multiply this linearly.

---

### Recommendation

1. **Cap the map size**: Enforce a hard upper bound (e.g., `MAX_PENDING_GET_BLOCK_PROPOSALS`) in `insert_get_block_proposals`, evicting oldest entries or rejecting new ones when the cap is reached.
2. **Per-peer rate limiting**: Track how many proposal IDs each peer has contributed and reject messages from peers that exceed a per-peer quota, analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER`.
3. **Avoid full clone on drain**: Replace the clone-then-clear pattern with `std::mem::take` or a swap to avoid the O(n) allocation.

---

### Proof of Concept

```
Attacker peer loop:
  for i in 0..∞:
    ids = [random_10_bytes() for _ in range(3000)]   # all absent from victim tx-pool
    send GetBlockProposal { proposals: ids } to victim

Victim node:
  GetBlockProposalProcess::execute():
    fetch_txs(ids) → all missing
    not_exist_proposals = ids   # all 3000 inserted
    insert_get_block_proposals(peer, not_exist_proposals)
    # pending_get_block_proposals grows by 3000 entries per message

After N messages:
  pending_get_block_proposals.len() == N * 3000  (no cap)
  drain_get_block_proposals() clones N*3000 entries → OOM or stall
``` [6](#0-5) [2](#0-1)

### Citations

**File:** sync/src/types/mod.rs (L1021-1021)
```rust
            pending_get_block_proposals: DashMap::new(),
```

**File:** sync/src/types/mod.rs (L1586-1592)
```rust
    pub fn drain_get_block_proposals(
        &self,
    ) -> DashMap<packed::ProposalShortId, HashSet<PeerIndex>> {
        let ret = self.pending_get_block_proposals.clone();
        self.pending_get_block_proposals.clear();
        ret
    }
```

**File:** sync/src/types/mod.rs (L1594-1600)
```rust
    pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
        for id in ids.into_iter() {
            self.pending_get_block_proposals
                .entry(id)
                .or_default()
                .insert(pi);
        }
```

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

**File:** util/constant/src/sync.rs (L69-72)
```rust
/// The soft limit to the number of unknown transactions
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
/// The soft limit to the number of unknown transactions per peer
pub const MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER: usize = MAX_RELAY_TXS_NUM_PER_BATCH;
```
