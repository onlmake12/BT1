Audit Report

## Title
Unbounded Memory Growth in `pending_get_block_proposals` via Unauthenticated Peer Messages — (`sync/src/types/mod.rs`, `sync/src/relayer/get_block_proposal_process.rs`)

## Summary
Any peer that completes the handshake can send unlimited `GetBlockProposal` relay messages containing random proposal IDs absent from the local tx-pool. Each message inserts up to ~3,000 entries into the node-global `pending_get_block_proposals` DashMap with no size cap, no per-peer quota, and no deduplication across messages. The map grows unboundedly until the periodic drain, which clones the entire structure in O(n) memory, enabling a sustained memory-exhaustion denial-of-service against the relay subsystem.

## Finding Description
`SyncState` initializes `pending_get_block_proposals` as an uncapped `DashMap::new()` at `sync/src/types/mod.rs:1021` with no size bound enforced anywhere in the codebase — no `MAX_PENDING_GET_BLOCK` constant exists.

`insert_get_block_proposals` at `sync/src/types/mod.rs:1594-1601` inserts every supplied ID unconditionally via `entry(id).or_default().insert(pi)` with no cap check.

`GetBlockProposalProcess::execute` at `sync/src/relayer/get_block_proposal_process.rs:35-77` applies only a single per-message length check (`max_block_proposals_limit * max_uncles_num`, defaulting to ~3,000), then passes all IDs absent from the tx-pool directly to `insert_get_block_proposals`. There is no global map size check and no per-peer quota.

`drain_get_block_proposals` at `sync/src/types/mod.rs:1586-1592` clones the entire DashMap before clearing it — an O(n) heap allocation that grows proportionally to the number of accumulated entries.

By contrast, `unknown_tx_hashes` has explicit soft limits (`MAX_UNKNOWN_TX_HASHES_SIZE = 50,000`, `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER = 32,767`) at `util/constant/src/sync.rs:69-72` that `pending_get_block_proposals` entirely lacks.

## Impact Explanation
A single attacker peer sending one message per second at ~3,000 IDs/message inserts 180,000 entries/minute. Multiple peers multiply this linearly. Between timer ticks the map grows without bound; at millions of entries `DashMap::clone()` inside `drain_get_block_proposals` triggers a massive heap allocation that can exhaust node memory or stall the async timer task, blocking proposal-relay processing for all peers. This matches **High (10,001–15,000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
No privilege is required — `GetBlockProposal` is an ordinary relay-protocol message reachable by any peer after handshake. The attacker only needs to generate random 10-byte IDs; no PoW, valid transaction, or fee is needed. There is no per-peer rate limit and no global map size cap, making the attack trivially repeatable and sustainable across every drain cycle.

## Recommendation
1. **Cap the map size**: Add a `MAX_PENDING_GET_BLOCK_PROPOSALS` constant and enforce it in `insert_get_block_proposals`, rejecting or evicting entries when the cap is reached.
2. **Per-peer quota**: Track per-peer contribution counts analogous to `MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER` and reject messages from peers exceeding their quota.
3. **Avoid full clone on drain**: Replace the clone-then-clear pattern in `drain_get_block_proposals` with `std::mem::take` or an atomic swap to eliminate the O(n) allocation.

## Proof of Concept
```
Attacker peer loop:
  for i in 0..∞:
    ids = [random_10_bytes() for _ in range(3000)]  # all absent from victim tx-pool
    send GetBlockProposal { proposals: ids } to victim

Victim node (GetBlockProposalProcess::execute):
  message_len check passes (3000 ≤ limit ~3000)
  fetch_txs(ids) → all missing → not_exist_proposals = ids (all 3000)
  insert_get_block_proposals(peer, not_exist_proposals)
  # pending_get_block_proposals grows by 3000 entries per message, unbounded

After N messages:
  pending_get_block_proposals.len() == N * 3000
  drain_get_block_proposals() allocates clone of N*3000 entries → OOM or async stall
```
A unit test can verify this by calling `insert_get_block_proposals` in a loop with distinct random IDs and asserting the map length grows without bound, then measuring allocation cost of `drain_get_block_proposals` at large N.