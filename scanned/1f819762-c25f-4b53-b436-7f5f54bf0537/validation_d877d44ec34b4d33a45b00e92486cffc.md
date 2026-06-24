Audit Report

## Title
Unbounded `pending_get_block_proposals` Cache Enables Memory/CPU DoS via Repeated `GetBlockProposal` Relay Messages — (File: sync/src/types/mod.rs)

## Summary
`SyncState::pending_get_block_proposals` is an unbounded `DashMap` with no size cap or per-peer quota. Any peer that completes the CKB handshake can flood the node with `GetBlockProposal` relay messages containing unique fake `ProposalShortId` values, causing the map to grow without bound. The periodic `prune_tx_proposal_request` timer then performs O(N) work over the entire accumulated map, compounding memory and CPU exhaustion into a node-level DoS.

## Finding Description

**Unbounded map declaration:**

`SyncState` declares the map with no capacity limit: [1](#0-0) 

**Unconditional insertion:**

`insert_get_block_proposals` inserts every supplied ID with no size guard: [2](#0-1) 

**Attacker-controlled entry path:**

In `GetBlockProposalProcess::execute()`, the only guard is a per-message count check against `max_block_proposals_limit * max_uncles_num`. There is no rate limit on how many messages a peer may send, no ban/disconnect for repeated requests, and no cap on total map size: [3](#0-2) 

All proposal IDs absent from the tx pool are unconditionally inserted: [4](#0-3) 

**O(N) drain on timer:**

`drain_get_block_proposals` clones the entire map (O(N) allocation) before clearing it: [5](#0-4) 

`prune_tx_proposal_request` then serialises all N keys into a `Vec` for the tx-pool actor and iterates the full map: [6](#0-5) 

With default consensus values (`max_block_proposals_limit = 1500`, `max_uncles_num = 2`), each message carries up to 3,000 unique IDs. Sending thousands of such messages per second is trivial for an attacker, and every fake ID passes the filter and is inserted permanently until the next timer drain.

## Impact Explanation

**High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**

- **Memory exhaustion**: At 3,000 entries per message and 10,000 messages, the map holds 30 million entries. Each entry is a `ProposalShortId` (10 bytes) plus a `HashSet<PeerIndex>`. The resulting multi-GB heap allocation causes an OOM crash.
- **CPU exhaustion**: When `prune_tx_proposal_request` fires, it clones and iterates all N entries, stalling the async relay task for seconds and blocking all relay processing for every connected peer.
- **Tx-pool saturation**: The `fetch_txs` call sends a massive payload to the tx-pool actor channel, degrading transaction admission for all users.

## Likelihood Explanation

Any peer that completes the standard CKB P2P handshake can trigger this. No authentication, stake, or special role is required. Generating random 10-byte `ProposalShortId` values costs negligible CPU on the attacker side. The per-message cap (3,000 IDs) is the only friction, but sending thousands of messages per second over a single TCP connection is trivial. The attack is cheap, repeatable, and requires no victim mistakes.

## Recommendation

1. **Cap `pending_get_block_proposals`**: Enforce a maximum total size (e.g., `max_block_proposals_limit × max_uncles_num × max_connected_peers`) and drop new insertions when the cap is reached.
2. **Per-peer quota**: Track how many entries each peer has contributed and reject further insertions once a per-peer limit is exceeded; ban the peer on repeated violations.
3. **Replace with a bounded LRU cache**: Use a bounded `LruCache<ProposalShortId, HashSet<PeerIndex>>` so the map self-limits without explicit eviction logic.
4. **Validate against known block hashes**: Only cache proposals that correspond to a block hash the node has recently seen, reducing the attack surface for entirely fake IDs.

## Proof of Concept

```
1. Establish a standard P2P connection to the target CKB node (complete handshake).

2. In a tight loop, send GetBlockProposal relay messages:
   - Each message contains 3,000 unique random ProposalShortId values
     (10 random bytes each, guaranteed absent from any tx pool).
   - The per-message limit check passes: 3,000 ≤ max_block_proposals_limit × max_uncles_num.

3. GetBlockProposalProcess::execute() calls tx_pool.fetch_txs() for each message.
   All 3,000 IDs are absent → all 3,000 are passed to insert_get_block_proposals().
   No rate limit, no ban, no size cap prevents insertion.

4. After N messages, pending_get_block_proposals holds N × 3,000 entries with no eviction.

5. When prune_tx_proposal_request fires (periodic timer):
   - drain_get_block_proposals() clones the entire map: O(N × 3,000) allocation.
   - fetch_txs() is called with N × 3,000 keys, saturating the tx-pool message queue.
   - The for loop iterates all N × 3,000 entries: O(N × 3,000) CPU.

6. At N = 10,000 messages: 30,000,000 map entries → multi-GB heap → OOM crash,
   or multi-second relay stall → effective DoS for all connected peers.

Unit test plan: mock a SyncState, call insert_get_block_proposals() in a loop with
unique random IDs, assert pending_get_block_proposals.len() grows without bound,
then call drain_get_block_proposals() and measure allocation/iteration time.
```

### Citations

**File:** sync/src/types/mod.rs (L1330-1330)
```rust
    pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>,
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

**File:** sync/src/relayer/get_block_proposal_process.rs (L35-44)
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
```

**File:** sync/src/relayer/get_block_proposal_process.rs (L68-77)
```rust
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

**File:** sync/src/relayer/mod.rs (L549-580)
```rust
    async fn prune_tx_proposal_request(&self, nc: &Arc<dyn CKBProtocolContext + Sync>) {
        let get_block_proposals = self.shared().state().drain_get_block_proposals();
        let tx_pool = self.shared.shared().tx_pool_controller();

        let fetch_txs = tx_pool
            .fetch_txs(
                get_block_proposals
                    .iter()
                    .map(|kv_pair| kv_pair.key().clone())
                    .collect(),
            )
            .await;
        if let Err(err) = fetch_txs {
            debug_target!(
                crate::LOG_TARGET_RELAY,
                "relayer prune_tx_proposal_request internal error: {:?}",
                err,
            );
            return;
        }

        let txs = fetch_txs.unwrap();

        let mut peer_txs = HashMap::new();
        for (id, peer_indices) in get_block_proposals.into_iter() {
            if let Some(tx) = txs.get(&id) {
                for peer_index in peer_indices {
                    let tx_set = peer_txs.entry(peer_index).or_insert_with(Vec::new);
                    tx_set.push(tx.clone());
                }
            }
        }
```
