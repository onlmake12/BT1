All citations verified against the actual source. Here is the validation result:

**Citation [1]** — `LightClientProtocol` struct: confirmed, only `shared: Shared`, no rate limiter field. [1](#0-0) 

**Citation [2]** — `try_process` dispatches directly with no rate-limit check: confirmed. [2](#0-1) 

**Citation [3]** — `should_ban` only fires on 4xx; valid requests return `OK` (200): confirmed. [3](#0-2) 

**Citation [4]** — Both proof limits are 1000: confirmed. [4](#0-3) 

**Citation [5]** — 3 DB reads per hash in `GetBlocksProofProcess`: confirmed (`get_block_header`, `get_block_uncles`, `get_block_extension`). [5](#0-4) 

**Citation [6]** — CBMT proof and `calc_witnesses_root` per block in `GetTransactionsProofProcess`: confirmed. [6](#0-5) 

**Citation [7]** — `Relayer` has `rate_limiter` field: confirmed. [7](#0-6) 

**Citation [8]** — `Relayer::try_process` checks rate limiter at top: confirmed. [8](#0-7) 

**Citation [9]** — `HolePunching` has both `rate_limiter` and `forward_rate_limiter`: confirmed. [9](#0-8) 

All nine citations are accurate. The code behaves exactly as described. The finding is valid.

---

Audit Report

## Title
Missing Per-Peer Rate Limit in Light Client Protocol Server Enables CPU/I/O Exhaustion — (`util/light-client-protocol-server/src/lib.rs`)

## Summary

`LightClientProtocol::try_process` performs no per-peer rate limiting before dispatching to `GetBlocksProofProcess` or `GetTransactionsProofProcess`. An unprivileged remote peer can flood the server with valid max-size (1000-item) proof requests, triggering unbounded sequential DB reads and MMR/CBMT proof generation with no cooldown, ban, or backpressure. This degrades response latency for all other light-client peers sharing the same handler.

## Finding Description

`LightClientProtocol` holds only a `Shared` field — no rate limiter is present:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
}
```

`try_process` dispatches directly to handlers with zero rate-limit check at `lib.rs` L96–125. The banning logic in `received` (L81–91) only fires when `status.should_ban()` returns `Some`, which requires a 4xx status code (`status.rs` L95–102). Valid proof requests return `StatusCode::OK` (200), so no ban or warning is ever triggered for a flooding peer.

Each `GetBlocksProof` with 1000 hashes performs up to 3 DB reads per hash (`get_block_header`, `get_block_uncles`, `get_block_extension`) plus an MMR `gen_proof` call (`get_blocks_proof.rs` L81–95, `lib.rs` L207–217). Each `GetTransactionsProof` with 1000 hashes additionally computes a CBMT merkle proof and `calc_witnesses_root` per block (`get_transactions_proof.rs` L86–106).

By contrast, `Relayer::try_process` checks `self.rate_limiter.check_key(&(peer, message.item_id()))` before any processing and returns `StatusCode::TooManyRequests` on excess (`relayer/mod.rs` L116–123). `HolePunching` similarly carries both `rate_limiter` and `forward_rate_limiter` (`hole_punching/mod.rs` L45–46). The light client protocol has neither.

Because `received` takes `&mut self`, the handler is exclusive — one peer's flood of max-size valid requests occupies the handler sequentially, starving all other light-client peers of responses.

## Impact Explanation

Sustained throughput degradation for all peers sharing the same light-client server. Matches **Low (501–2000 points): Any other important performance improvements for CKB**. The attack does not crash the node or affect consensus, but it does monopolize the light-client handler, making the light-client service effectively unavailable to legitimate peers for the duration of the flood.

## Likelihood Explanation

The attacker needs only a standard P2P connection and the ability to send well-formed `GetBlocksProof` or `GetTransactionsProof` messages. No privilege, key, or hashpower is required. The gap is directly reachable from the public P2P interface. The attack is trivially repeatable and requires no victim mistakes.

## Recommendation

Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer`). At the top of `try_process`, check `rate_limiter.check_key(&(peer_index, message.item_id()))` and return a non-4xx status (to avoid banning legitimate slow clients) on excess — either a new `StatusCode::TooManyRequests` that does not trigger `should_ban`, or silently drop the request. Call `rate_limiter.retain_recent()` in `disconnected` to bound memory growth.

## Proof of Concept

1. Connect two peers A and B to the light-client server.
2. From peer A, send `GetBlocksProof` messages in a tight loop, each containing 1000 valid main-chain block hashes and a valid `last_hash`.
3. From peer B, send a single `GetBlocksProof` request and measure response latency.
4. Assert that peer B's response latency grows proportionally to peer A's flood rate, confirming monopolization of the `&mut self` handler.
5. Confirm that peer A is never banned (no 4xx status is returned for valid requests), allowing the flood to continue indefinitely.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
        }
    }
```

**File:** util/light-client-protocol-server/src/status.rs (L95-102)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        let code = self.code as u16;
        if !(400..500).contains(&code) {
            None
        } else {
            Some(constant::BAD_MESSAGE_BAN_TIME)
        }
    }
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-106)
```rust
            let merkle_proof = CBMT::build_merkle_proof(
                &block
                    .transactions()
                    .iter()
                    .map(|tx| tx.hash())
                    .collect::<Vec<_>>(),
                &txs_and_tx_indices
                    .iter()
                    .map(|(_, index)| *index as u32)
                    .collect::<Vec<_>>(),
            )
            .expect("build proof with verified inputs should be OK");

            let txs: Vec<_> = txs_and_tx_indices
                .into_iter()
                .map(|(tx, _)| tx.data())
                .collect();

            let filtered_block = packed::FilteredBlock::new_builder()
                .header(block.header().data())
                .witnesses_root(block.calc_witnesses_root())
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L45-46)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
```
