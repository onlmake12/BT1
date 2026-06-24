Audit Report

## Title
Unbounded DB and MMR amplification per `GetBlocksProof` request with no per-peer rate limiting — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

## Summary
`GetBlocksProofProcess::execute` performs up to 3 synchronous RocksDB reads per requested hash (header, uncles, extension) plus O(N × log M) MMR node reads for proof generation, with the only guard being a 1000-hash cap per message. No per-peer rate limit, concurrent-request cap, or ban exists for well-formed valid requests. The LightClient protocol is enabled by default, so any unprivileged peer can repeatedly send maximal requests to saturate the node's I/O and CPU.

## Finding Description
`GET_BLOCKS_PROOF_LIMIT = 1000` is the sole guard at `constant.rs:5`. Once a message passes the size check (`get_blocks_proof.rs:38–40`) and the duplicate check (`get_blocks_proof.rs:62–70`), the handler iterates all `found` hashes and unconditionally calls `get_block_header`, `get_block_uncles`, and `get_block_extension` for each (`get_blocks_proof.rs:81–95`) — 3000 DB reads for a maximal request. `reply_proof` then calls `mmr.get_root()` and `mmr.gen_proof(items_positions)` with up to 1000 positions (`lib.rs:199–216`), traversing O(1000 × log N) MMR nodes. The `received` handler at `lib.rs:55–92` only bans on unparseable messages or when `status.should_ban()` returns `Some`; a valid maximal request returns `Status::ok()` with no ban and no backpressure. The LightClient protocol is included in `default_support_all_protocols()` (`util/app-config/src/configs/network.rs:247`) and in the default `ckb.toml` `support_protocols` list (`resource/ckb.toml:112`), so it is active on all default-configured nodes.

## Impact Explanation
An attacker sending a sustained stream of maximal `GetBlocksProof` messages (each with 1000 valid main-chain hashes) forces the target node to perform thousands of synchronous RocksDB reads and CPU-intensive MMR proof computations per message, with no throttle. This can saturate the node's storage I/O and CPU, starving the Sync and Relay protocol handlers of resources and halting the node's participation in block propagation — matching the allowed impact: **Vulnerabilities which could easily crash a CKB node (High, 10001–15000 points)**.

## Likelihood Explanation
The attacker requires only a standard P2P connection to a default-configured CKB node and knowledge of 1000 main-chain block hashes, which are trivially obtained by syncing block headers. No proof-of-work, key material, or privilege is needed. The attack is fully repeatable and can be parallelized across multiple connections up to `max_peers = 125` (default).

## Recommendation
- Introduce a per-peer sliding-window rate limit on `GetBlocksProof` (and similarly `GetTransactionsProof`) messages, e.g., a token-bucket allowing N requests per second per peer.
- Apply a short ban or exponential backoff when a peer exceeds the rate limit.
- Consider reducing `GET_BLOCKS_PROOF_LIMIT` or requiring the requester to demonstrate a valid light-client sync state before serving large proofs.
- Evaluate whether `mmr.gen_proof` for large position sets should be bounded or deferred to a background task with a concurrency cap.

## Proof of Concept
1. Connect to a default-configured CKB full node (LightClient protocol enabled by default).
2. Sync enough block headers to collect 1000 distinct main-chain block hashes at heights ≤ tip.
3. Construct a valid `GetBlocksProof` message with all 1000 hashes and a valid `last_hash`.
4. Send this message in a tight loop (or 100 concurrent connections each sending the message repeatedly).
5. Observe: each message triggers ~3000 RocksDB reads + O(1000 × log N) MMR node reads; no ban is applied; relay and sync message latency spikes measurably; the node's block-propagation participation degrades or halts under sustained load.