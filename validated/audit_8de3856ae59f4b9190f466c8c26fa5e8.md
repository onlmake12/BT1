Audit Report

## Title
Unbounded Per-Message I/O Work with No Per-Peer Rate Limit in `GetBlocksProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
The light-client protocol server accepts `GetBlocksProof` messages from any peer and performs up to 4 × 1000 synchronous RocksDB reads plus one `mmr.gen_proof(1000 positions)` call per message. Because structurally valid messages always return `StatusCode::OK` (200), the ban logic is never triggered, and no per-peer rate limit or message-frequency guard exists anywhere in the server. A single unprivileged peer can sustain a continuous flood of such messages at negligible cost, saturating the node's I/O and CPU.

## Finding Description
`GET_BLOCKS_PROOF_LIMIT = 1000` in `util/light-client-protocol-server/src/constant.rs` (L5) caps work per message but places no bound on message frequency. Inside `GetBlocksProofProcess::execute()` (`get_blocks_proof.rs` L81–95), for each of up to 1000 found hashes the handler calls `snapshot.is_main_chain`, `snapshot.get_block_header`, `snapshot.get_block_uncles`, and `snapshot.get_block_extension` — four synchronous DB reads per hash. After the loop, `reply_proof` in `lib.rs` (L210) calls `mmr.gen_proof(items_positions)` with up to 1000 positions.

`Status::should_ban()` (`status.rs` L95–102) only returns `Some(ban_time)` for HTTP-class 4xx codes. A well-formed request with valid hashes returns `StatusCode::OK` (200), so the dispatch loop in `lib.rs` (L81–86) never calls `nc.ban_peer`. The `LightClientProtocol` struct (`lib.rs` L26–29) holds only a `Shared`; there is no per-peer counter, token bucket, timestamp guard, or inflight-request cap. A grep for `rate_limit`, `throttle`, `quota`, `message_count`, `flood`, or `per_peer` across the entire `util/light-client-protocol-server/` tree returns zero matches.

The same structural pattern and the same `GET_*_LIMIT = 1000` constant apply to `GetTransactionsProofProcess` (`get_transactions_proof.rs` L37) and `GetLastStateProofProcess` (`get_last_state_proof.rs` L201–204).

## Impact Explanation
A single attacker peer can sustain a continuous stream of valid `GetBlocksProof` messages, each forcing 4000 synchronous RocksDB reads and a 1000-position MMR proof generation on the server. This saturates the node's RocksDB I/O bandwidth and CPU, starving both the chain-sync pipeline and legitimate light-client peers. This matches the allowed High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker's cost is sending ~32 KB messages; the server's cost per message is orders of magnitude higher.

## Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light-client protocol enabled. No proof-of-work, no key, no privileged role is needed. The 1000 valid main-chain block hashes required per message are trivially obtained by syncing a few headers first. The existing unit test at `util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs` L78 explicitly asserts `nc.not_banned(peer_index)` for a valid request, confirming the absence of any ban or throttle on the happy path. The attack is fully repeatable and requires no victim mistake.

## Recommendation
1. **Per-peer message-rate limit**: Track the timestamp of the last `GetBlocksProof` message per peer and enforce a minimum inter-message interval (e.g., 1 request/second). Return a 4xx status (triggering a ban) or silently drop messages that exceed the limit.
2. **Inflight request cap**: Allow at most one outstanding `GetBlocksProof` per peer; drop or ban if a second arrives before the first response is sent.
3. **Reduce the per-message ceiling**: Lower `GET_BLOCKS_PROOF_LIMIT` from 1000 to a smaller value (e.g., 100–200) to reduce per-message cost while still serving legitimate clients.
4. **Apply the same fix to `GetTransactionsProof` and `GetLastStateProof`**, which share the identical structural pattern and the same `GET_*_LIMIT = 1000` constant.

## Proof of Concept
```
1. Connect to a CKB node with the light-client protocol enabled.
2. Sync enough headers to collect 1000 valid main-chain block hashes H[0..999].
3. In a tight loop:
     msg = GetBlocksProof { last_hash: tip_hash, block_hashes: H[0..999] }
     send(msg)
4. Observe: server processes each message (4 × 1000 DB reads + mmr.gen_proof(1000)),
   returns SendBlocksProofV1, issues no ban.
5. After K iterations the server's RocksDB I/O queue and async executor are saturated;
   legitimate peers receive no responses.
```

The unit test at `util/light-client-protocol-server/src/tests/components/get_blocks_proof.rs` L75–78 already provides a minimal reproducible harness: it sends a valid `GetBlocksProof` message and asserts `nc.not_banned(peer_index)`, confirming the complete absence of any throttle or ban on the happy path. Extending this test to send the same message in a loop and measuring server response latency for a concurrent legitimate peer would reproduce the starvation effect.