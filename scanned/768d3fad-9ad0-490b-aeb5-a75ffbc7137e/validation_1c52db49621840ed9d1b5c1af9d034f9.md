Audit Report

## Title
Unbounded Memory Growth in `pending_get_block_proposals` DashMap via Repeated `GetBlockProposal` P2P Messages — (File: sync/src/types/mod.rs)

## Summary
The `SyncState` struct holds a `pending_get_block_proposals: DashMap<packed::ProposalShortId, HashSet<PeerIndex>>` that is populated by any connected peer via `GetBlockProposal` relay messages. There is no total-size cap on this map, and no per-peer rate limit on the message type. A single malicious peer can flood the node with messages carrying up to 3000 unique, non-existent `ProposalShortId` values each, growing the DashMap without bound between periodic drain cycles and exhausting heap memory.

## Finding Description
The `pending_get_block_proposals` DashMap is declared without any capacity bound at `sync/src/types/mod.rs:1330`. Entries are inserted by `insert_get_block_proposals` (lines 1594–1601), which iterates over all provided IDs and calls `entry(id).or_default().insert(pi)` with no check on the map's current size.

The entry point is `GetBlockProposalProcess::execute` (`sync/src/relayer/get_block_proposal_process.rs:32–77`). The only guard is a per-message count check at lines 38–44: if the message contains more than `max_block_proposals_limit * max_uncles_num` proposals (typically 1500 × 2 = 3000), the message is rejected. After that check, proposals absent from the tx pool are collected (lines 68–71) and unconditionally inserted into the DashMap (lines 73–77). There is no check on the DashMap's accumulated size before or after insertion.

The map is drained by `drain_get_block_proposals` (lines 1586–1592), which clones and clears the map. This is triggered by the `TX_PROPOSAL_TOKEN` notify timer, set to fire every 100 ms (`sync/src/relayer/mod.rs:798`). Between two consecutive timer ticks, a peer can send an arbitrary number of `GetBlockProposal` messages. The `received` handler (`mod.rs:809–879`) applies no rate limit to this message type — it only bans peers for malformed messages, not for high-frequency valid ones.

Because `ProposalShortId` is 10 bytes and can be set to arbitrary values, an attacker trivially generates unique IDs that will never match the tx pool, ensuring every ID is inserted. With 3000 unique IDs per message and no rate limit, the attacker can insert millions of entries per drain interval, each consuming ~50 bytes of heap (key + `HashSet` allocation), growing the DashMap without bound until the OS OOM-kills the process.

The CKB CHANGELOG confirms this exact class of bug was fixed for `inflight_proposals` (#3093) and `pending_compact_blocks` (#3110) in v0.101.0, but `pending_get_block_proposals` was not addressed.

## Impact Explanation
A single connected peer (inbound or outbound) can exhaust the node's heap memory, causing the process to be killed by the OS OOM killer or become unresponsive. This constitutes a full node crash triggered by an unprivileged external actor, matching the allowed impact: **"Vulnerabilities which could easily crash a CKB node" — High (10001–15000 points)**.

## Likelihood Explanation
The attack requires only a single established P2P connection — no special role, key material, or hashpower. `ProposalShortId` values are arbitrary 10-byte sequences; the attacker does not need to know any real proposal IDs. The per-message limit of 3000 is generous, and there is no rate limit on how many `GetBlockProposal` messages a peer may send per second. The attack is trivially automatable and can be sustained indefinitely from a single host.

## Recommendation
1. **Add a total-size cap** to `pending_get_block_proposals` in `insert_get_block_proposals`: reject insertions once the DashMap exceeds a configurable maximum (e.g., `max_block_proposals_limit * max_uncles_num * MAX_PEERS`).
2. **Add per-peer rate limiting** for `GetBlockProposal` messages in the relayer's `received` handler, consistent with rate-limiting patterns used elsewhere in the protocol.
3. **Ban peers** that repeatedly send proposals absent from the tx pool, using the existing `BAD_MESSAGE_BAN_TIME` pattern already applied to malformed messages.

## Proof of Concept
```
1. Attacker establishes a P2P connection to the victim CKB node (inbound or outbound).
2. Loop indefinitely:
   a. Craft a RelayMessage::GetBlockProposal containing 3000 unique, random
      ProposalShortId values (10 random bytes each).
   b. Send the message over the established session.
3. Each iteration inserts up to 3000 new entries into pending_get_block_proposals.
4. The DashMap is only drained every 100 ms (TX_PROPOSAL_TOKEN timer).
5. At network speed (e.g., 1000 messages/100 ms), ~3,000,000 entries (~150 MB)
   accumulate per drain interval, exhausting heap memory and crashing the node.
```

Code path: `P2P receive` → `Relayer::received` (`mod.rs:809`) → `GetBlockProposalProcess::execute` (`get_block_proposal_process.rs:32`) → `insert_get_block_proposals` (`types/mod.rs:1594`) → unbounded `pending_get_block_proposals` DashMap growth (`types/mod.rs:1330`).