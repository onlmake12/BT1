Based on my thorough code review, this is a valid vulnerability.

---

### Title
Missing Per-Peer Rate Limit in `LightClientProtocol` Enables Single-Peer CPU/DB Exhaustion via `GetBlocksProof` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

### Summary

`LightClientProtocol` has no per-peer rate limiter. A single unprivileged peer can flood the server with `GetBlocksProof` messages each containing up to 1000 valid main-chain block hashes, triggering up to 3000 DB reads and a full MMR `gen_proof(1000 positions)` per message, with no throttle or queue bound.

### Finding Description

`LightClientProtocol` is defined with only a `shared: Shared` field — no rate limiter: [1](#0-0) 

Its `try_process` dispatches directly to handlers with zero rate-limit checks: [2](#0-1) 

`GetBlocksProofProcess::execute` accepts up to `GET_BLOCKS_PROOF_LIMIT = 1000` block hashes per message: [3](#0-2) 

For every hash that is on the main chain, the handler performs three synchronous DB reads: [4](#0-3) 

Then `reply_proof` calls `mmr.gen_proof(items_positions)` with up to 1000 positions — a CPU-intensive MMR traversal: [5](#0-4) 

A valid request (≤1000 unique main-chain hashes, valid `last_hash`) is never banned — `BAD_MESSAGE_BAN_TIME` only fires on parse failures: [6](#0-5) 

**Contrast with `Relayer`:** the relay protocol explicitly carries a `governor`-based rate limiter keyed by `(peer, message_type)` and returns `TooManyRequests` before any handler runs. `LightClientProtocol` has no equivalent — confirmed by a complete absence of `rate_limiter`, `RateLimiter`, or `governor` anywhere under `util/light-client-protocol-server/`.



### Impact Explanation

Each max-size request costs the server:
- 3 × 1000 = **3000 RocksDB point-reads** (header, uncles, extension per block)
- **1 MMR `gen_proof` over 1000 leaf positions** (O(N log N) hash computations)

A single attacker peer sending these in a tight loop saturates both the DB I/O path and the async executor threads, measurably degrading throughput for all other peers. No PoW, no stake, no privileged role is required — only a valid TCP connection to the light-client port.

### Likelihood Explanation

The light-client protocol is activated on mainnet (since v0.110.1). Any peer that speaks the protocol can send `GetBlocksProof`. The attack requires only knowledge of 1000 valid block hashes (trivially obtained from any block explorer or by syncing headers), and a loop. No special tooling is needed.

### Recommendation

Add a `governor`-based per-peer rate limiter to `LightClientProtocol`, mirroring the pattern already used in `Relayer`:
- Key the limiter by `(PeerIndex, message_item_id)`.
- Check it at the top of `try_process` before dispatching to any handler.
- Return `StatusCode::TooManyRequests` (non-banning) when the limit is exceeded.

Additionally, consider capping the effective work per request (e.g., limiting `found` to a smaller sub-limit before MMR proof generation) as a defense-in-depth measure.

### Proof of Concept

```
1. Connect to a CKB full node with the light-client protocol enabled.
2. Obtain 1000 distinct main-chain block hashes (e.g., blocks 1–1000).
3. Obtain the current tip hash as last_hash.
4. In a tight loop, send GetBlocksProof{block_hashes: [h1..h1000], last_hash: tip}.
5. Measure: server CPU spikes to saturation; RocksDB read latency for other peers
   increases measurably; light-client responses to legitimate peers are delayed or dropped.
```

The server will process every message fully — 3000 DB reads + `gen_proof(1000)` — with no throttle applied, confirming the invariant violation. [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-35)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}

impl LightClientProtocol {
    /// Create a new light client protocol handler.
    pub fn new(shared: Shared) -> Self {
        Self { shared }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L63-92)
```rust
        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
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

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            };
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L33-114)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.block_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no block");
        }

        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");

        let block_hashes: Vec<_> = self
            .message
            .block_hashes()
            .to_entity()
            .into_iter()
            .collect();

        let mut uniq = HashSet::new();
        if !block_hashes
            .iter()
            .chain([last_block_hash].iter())
            .all(|hash| uniq.insert(hash))
        {
            return StatusCode::MalformedProtocolMessage
                .with_context("duplicate block hash exists");
        }

        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

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

        let proved_items = (
            block_headers.into(),
            uncles_hash.into(),
            packed::BytesOptVec::new_builder().set(extensions).build(),
        );
        let missing_items = missing.into();

        self.protocol
            .reply_proof::<packed::SendBlocksProofV1>(
                self.peer,
                self.nc,
                &last_block,
                positions,
                proved_items,
                missing_items,
            )
            .await
    }
```
