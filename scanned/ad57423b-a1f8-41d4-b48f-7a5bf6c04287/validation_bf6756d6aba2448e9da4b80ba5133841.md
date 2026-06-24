The code in the repository exactly matches all claims. Let me confirm the key details before rendering judgment.

All cited code is verified:
- `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` at line 16 of `orphan.rs`
- `limit_size` uses `self.entries.keys().next()` (HashMap iteration order, effectively random) at lines 119–125, with the comment literally reading `// Evict a random orphan:`
- `add_orphan_tx` inserts unconditionally then calls `limit_size` at lines 134–159
- No per-peer quota exists anywhere in `OrphanPool`
- `after_process` routes to `add_orphan` on `is_missing_input` at lines 507–512 of `process.rs`
- Evicted txs are sent as `TxVerificationResult::Reject` at lines 563–572 of `process.rs`

The attack is cheap (only bandwidth), requires only a standard P2P connection, and can be applied to any/all nodes simultaneously — fitting "High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

---

Audit Report

## Title
Orphan Pool Random Eviction With No Per-Peer Cap Allows Any P2P Peer to Permanently Displace Legitimate Orphan Transactions - (File: `tx-pool/src/component/orphan.rs`)

## Summary
`OrphanPool` enforces a global cap of 100 entries with no per-peer sub-limit. When the pool is full, `limit_size` evicts by arbitrary `HashMap` iteration order — effectively random. A single unprivileged P2P peer can pre-fill all 100 slots with crafted orphan transactions (inputs referencing non-existent cells), giving any subsequently arriving legitimate orphan a ≈99% probability of immediate eviction. The attacker can sustain this indefinitely at the cost of bandwidth alone, preventing dependent transaction chains from propagating through the targeted node.

## Finding Description
`add_orphan_tx` (lines 134–159 of `tx-pool/src/component/orphan.rs`) inserts the new transaction unconditionally into `self.entries` and then calls `limit_size`. Inside `limit_size` (lines 96–132), after expiry-based removal, the overflow loop picks the eviction victim via `self.entries.keys().next()` — the first key returned by `HashMap` iteration, which is non-deterministic and effectively random. The comment in the source code itself labels this `// Evict a random orphan:`. There is no per-peer counter, no fee-rate ordering, and no priority mechanism anywhere in `OrphanPool`.

The entry path is confirmed in `process.rs` lines 507–512: whenever `_process_tx` returns `is_missing_input`, `after_process` calls `add_orphan`. Evicted entries are forwarded to the relayer as `TxVerificationResult::Reject` (lines 563–572), which clears them from the "known" filter, making the node willing to re-request them — but the attacker simply re-announces the same hashes to refill the evicted slot.

Orphan entries carry an expiry of `100 × MAX_BLOCK_INTERVAL` seconds (line 15), so attacker entries persist for many minutes before natural expiry can help.

Existing guards are insufficient: the only guard is the duplicate check `self.entries.contains_key` (line 140), which prevents re-inserting the *same* transaction but does nothing to limit how many distinct transactions a single peer may insert.

## Impact Explanation
This is a **High** severity finding matching the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* An attacker with only a P2P connection and bandwidth can saturate the orphan pool of any reachable CKB node, preventing dependent transaction chains from being resolved and relayed. Applied simultaneously to multiple nodes (trivially achievable since the cost per node is ~100 small transactions), this disrupts orphan transaction propagation network-wide. Legitimate users whose transactions depend on unconfirmed parents cannot have those transactions relayed through affected nodes, degrading the network's transaction propagation capability at negligible attacker cost.

## Likelihood Explanation
The attack requires only a standard P2P connection — no keys, no hashpower, no privilege. The attacker announces 100 transaction hashes, waits for the victim to request them via the relay protocol, and delivers transactions referencing non-existent cells. These pass `non_contextual_verify` (structural checks only) and fail contextually with `is_missing_input`, landing in the orphan pool. The 100-entry limit is small enough to saturate in a single round-trip. Sustaining the attack requires only periodic re-announcement of the same 100 hashes whenever any slot is freed. The attack is repeatable, cheap, and requires no special knowledge of the victim's state.

## Recommendation
1. **Add a per-peer cap** inside `OrphanPool`: track a `HashMap<PeerIndex, usize>` entry count and reject (or evict from that peer's quota) when a single peer exceeds `DEFAULT_MAX_ORPHAN_TRANSACTIONS / N` entries, where N is a reasonable peer divisor.
2. **Replace random eviction with priority-based eviction**: evict the entry with the lowest declared cycle count (or fee rate) first, so attacker entries with minimal declared cycles are displaced before legitimate high-value ones.
3. **Increase `DEFAULT_MAX_ORPHAN_TRANSACTIONS`** or make it proportional to the number of connected peers to raise the cost of saturation.

## Proof of Concept
1. Attacker connects to a CKB node as a standard P2P peer.
2. Attacker announces 100 distinct transaction hashes via `RelayTransactionHashes`.
3. Victim node requests the transactions via `GetRelayTransactions`.
4. Attacker responds with 100 transactions whose inputs reference cells that do not exist on-chain or in the tx-pool. Each passes `non_contextual_verify`, fails contextual resolution with `is_missing_input`, and is inserted into `OrphanPool` via `add_orphan_tx` (lines 134–159 of `orphan.rs`).
5. Pool is now at capacity (100 entries, all attacker-controlled).
6. A legitimate user submits a transaction whose parent is unconfirmed. It reaches the victim node, passes non-contextual checks, fails `is_missing_input`, and `add_orphan_tx` is called. Pool reaches 101; `limit_size` evicts one entry at random (line 121). With probability 100/101 ≈ 99% the legitimate entry is evicted and sent as `TxVerificationResult::Reject` to the relayer.
7. Attacker periodically re-announces all 100 of its hashes to refill any slot that was evicted, maintaining the pool at 100 attacker entries indefinitely.
8. The legitimate orphan transaction is never resolved; its dependent chain cannot propagate through this node.

A unit test can reproduce this deterministically: insert 100 `Entry` objects from `peer=1`, then insert one from `peer=2`, call `limit_size`, and assert the `peer=2` entry is present in the pool with probability approaching 1/101 across repeated randomized runs — or simply assert that no per-peer limit prevents `peer=1` from holding all 100 slots.