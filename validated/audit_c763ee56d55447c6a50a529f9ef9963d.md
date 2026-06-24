Audit Report

## Title
Missing Deduplication of `tx_hashes` Enables Amplified DB Reads with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary

The `GetTransactionsProofProcess::execute()` function accepts up to 1000 `tx_hashes` but performs no deduplication before processing. An attacker sending 1000 copies of the same confirmed transaction hash triggers 2000 DB reads (1000 `get_transaction_info` + 1000 `get_transaction_with_info`) and a `CBMT::build_merkle_proof` call with 1000 duplicate indices per request. Unlike the `Relayer` protocol, `LightClientProtocol` has no per-peer rate limiter, and valid requests return `OK` (200), which never triggers a ban. This allows indefinite repetition by any unprivileged P2P peer.

## Finding Description

**Root cause — no deduplication before DB iteration:**

In `get_transactions_proof.rs` lines 54–74, the code partitions all `tx_hashes` (including duplicates) and then iterates the `found` list calling `get_transaction_with_info` for each entry individually. With 1000 copies of the same hash, this produces 1000 `get_transaction_info` calls (partition) and 1000 `get_transaction_with_info` calls (found loop) — 2000 DB reads per request.

The `txs_in_blocks` HashMap groups by `block_hash`, so all 1000 duplicate entries accumulate in a single Vec under one key. `CBMT::build_merkle_proof` is then called once for that block but receives 1000 duplicate indices, performing O(N log N) work with N=1000.

**No rate limiting in `LightClientProtocol`:**

`LightClientProtocol::try_process()` (lib.rs lines 96–125) dispatches directly to handlers with no rate-limiting check. By contrast, `Relayer::try_process()` (sync/src/relayer/mod.rs lines 116–123) checks a `governor`-based per-peer rate limiter before any handler is invoked. `LightClientProtocol` has no such field or check.

**No ban for valid requests:**

`Status::should_ban()` (status.rs lines 95–102) only bans 4xx status codes. A well-formed request with ≤1000 hashes and a valid `last_hash` returns `StatusCode::OK` (200) — no ban is applied regardless of how many times it is sent.

**Only guard present:**

```rust
if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
}
```
This caps the count at 1000 but does nothing to prevent 1000 identical hashes.

## Impact Explanation

Each maximum-cost request causes 2000 synchronous DB reads and O(N log N) CBMT work. With no rate limiting and no ban for valid requests, a single attacker connection can sustain this indefinitely, saturating the node's storage I/O and CPU. This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (a light-client-enabled full node under sustained attack cannot serve legitimate peers) and **High: easily crash a CKB node** (storage I/O saturation can exhaust file descriptors or cause the RocksDB layer to stall under sustained amplified read pressure).

## Likelihood Explanation

- Precondition: any confirmed transaction on the main chain (always true on a running node).
- Attacker capability: any unprivileged P2P peer that can connect to a light-client-enabled node.
- No proof-of-work, no key, no privileged role required.
- Requests can be sent in a tight loop; each returns `OK` with no cooldown enforced.
- The light client protocol is a supported, documented feature (`SupportProtocols::LightClient`).

## Recommendation

1. **Deduplicate `tx_hashes` before processing**: convert to a `HashSet` immediately after the length check, then proceed with the deduplicated set.
2. **Add a per-peer rate limiter to `LightClientProtocol`**: mirror the `governor`-based `rate_limiter` already present in `Relayer` and check it at the top of `try_process()` keyed by `(peer, message.item_id())`.
3. **Consider lowering `GET_TRANSACTIONS_PROOF_LIMIT`** or adding a per-request cost budget proportional to the number of unique blocks touched.

## Proof of Concept

```
1. Start a CKB node with light client protocol enabled.
2. Obtain any confirmed tx_hash H and the current tip_hash T from the node.
3. Construct:
     GetTransactionsProof {
         last_hash: T,
         tx_hashes: [H; 1000]   // 1000 identical hashes
     }
4. Send this message repeatedly over a single P2P connection.
5. Observe on the server:
   - 1000 get_transaction_info DB reads (partition loop, lines 59–64)
   - 1000 get_transaction_with_info DB reads (found loop, lines 68–74)
   - CBMT::build_merkle_proof called with 1000 duplicate indices (lines 86–97)
   - Response: SendTransactionsProof with OK status — no ban applied
   - No rate limiting prevents the next request from being processed immediately
```