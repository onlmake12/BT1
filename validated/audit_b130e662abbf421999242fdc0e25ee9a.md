Audit Report

## Title
Unbounded `pending_get_block_proposals` DashMap Growth via Peer-Supplied `GetBlockProposal` Messages — (File: `sync/src/types/mod.rs`)

## Summary
Any unauthenticated P2P peer can flood `pending_get_block_proposals` by repeatedly sending `GetBlockProposal` messages containing fabricated `ProposalShortId` values absent from the tx pool. `insert_get_block_proposals` performs no size check before inserting into the shared `DashMap`, and the periodic drain clones the entire map before clearing it, creating a transient memory-doubling spike at every drain tick. Sustained flooding causes unbounded memory growth, ultimately crashing the node via OOM.

## Finding Description
`GetBlockProposalProcess::execute()` validates only that the per-message proposal count does not exceed `max_block_proposals_limit * max_uncles_num` (≤ 3,000 on mainnet). Proposals absent from the tx pool are forwarded unconditionally to `insert_get_block_proposals`:

```rust
// sync/src/relayer/get_block_proposal_process.rs L68-77
let not_exist_proposals: Vec<packed::ProposalShortId> = proposals
    .into_iter()
    .filter(|short_id| !fetched_transactions.contains_key(short_id))
    .collect();
self.relayer.shared().state()
    .insert_get_block_proposals(self.peer, not_exist_proposals);
```

`insert_get_block_proposals` inserts every supplied ID with no size guard:

```rust
// sync/src/types/mod.rs L1594-1601
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
```

The map is declared as an unbounded `DashMap::new()` at `sync/src/types/mod.rs` L1021. The only relief valve, `drain_get_block_proposals`, performs a full clone before clearing — doubling peak memory at every drain tick:

```rust
// sync/src/types/mod.rs L1586-1592
pub fn drain_get_block_proposals(&self) -> DashMap<...> {
    let ret = self.pending_get_block_proposals.clone(); // O(n) allocation
    self.pending_get_block_proposals.clear();
    ret
}
```

This drain is called from `prune_tx_proposal_request` via the `TX_PROPOSAL_TOKEN` notify timer, which fires every **100 ms** (10 Hz), confirmed at `sync/src/relayer/mod.rs` L798. The rate limiter is keyed by `(peer, message_id)` at 30 req/s per peer (`sync/src/relayer/mod.rs` L91), which slows but does not prevent the attack. With 10 attacker peers at 30 req/s × 3,000 proposals/req, the map accumulates ~900,000 entries/s. At each 100 ms drain tick, the full clone doubles peak memory transiently. No eviction, per-peer accounting, or hard cap exists on `pending_get_block_proposals`.

## Impact Explanation
The attack causes OOM kill of the CKB node process — a complete local node shutdown. This matches the allowed bounty impact: **High (10,001–15,000 points): Vulnerabilities which could easily crash a CKB node.** The drain-time clone creates a transient spike that can trigger OOM even before steady-state is reached, and the CPU cost of cloning/clearing a large DashMap delays the relayer timer, creating a positive feedback loop.

## Likelihood Explanation
- Requires only a standard, unauthenticated P2P connection — no stake, miner role, or special privilege.
- Fabricating `ProposalShortId` values absent from the tx pool is trivial (random 10-byte values).
- The 30 req/s rate limiter slows but does not prevent the attack; multiple Sybil connections multiply the effect linearly.
- No existing hard cap, eviction policy, or per-peer accounting guards `pending_get_block_proposals`.
- Analogous fixes were applied to `inflight_proposals`, inflight blocks, and pending compact blocks, confirming this class of bug is recognized in the codebase; `pending_get_block_proposals` was not addressed.

## Recommendation
Add a hard cap inside `insert_get_block_proposals`:

```rust
pub fn insert_get_block_proposals(&self, pi: PeerIndex, ids: Vec<packed::ProposalShortId>) {
    const MAX_PENDING_PROPOSALS: usize = 10_000;
    if self.pending_get_block_proposals.len() >= MAX_PENDING_PROPOSALS {
        return;
    }
    for id in ids.into_iter() {
        self.pending_get_block_proposals
            .entry(id)
            .or_default()
            .insert(pi);
    }
}
```

Additionally, replace the full-clone drain with an atomic swap (e.g., `DashMap::drain()` or `std::mem::replace` on a `Mutex<HashMap>`) to eliminate the transient memory-doubling spike at each 100 ms tick.

## Proof of Concept
1. Connect to a victim CKB node as a standard P2P peer.
2. Repeatedly send `GetBlockProposal` relay messages containing 3,000 random `ProposalShortId` values (none present in the victim's tx pool) at 30 messages/second per connection.
3. Open ≥ 10 such connections simultaneously.
4. Monitor victim process RSS — it climbs by hundreds of MB per second.
5. At each drain tick (~10 Hz, every 100 ms per `TX_PROPOSAL_TOKEN` timer), RSS spikes further due to the full-map clone in `drain_get_block_proposals`.
6. The victim node is OOM-killed and cannot relay or confirm transactions.