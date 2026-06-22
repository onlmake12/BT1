The code evidence is clear. Let me confirm the key facts before rendering a verdict.

### Title
Unbounded `GetBlocksProof` Request Amplification in `LightClientProtocol` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

`LightClientProtocol` contains no rate limiter of any kind. A single unprivileged peer can continuously send `GetBlocksProof` messages each carrying up to `GET_BLOCKS_PROOF_LIMIT = 1000` valid on-chain block hashes, forcing the server to execute up to 3 000 synchronous RocksDB reads plus an `mmr.gen_proof(1000 positions)` call per message with zero throttling. The light-client protocol is **enabled by default** in the production configuration. Both sibling protocols — `Relayer` (30 req/s keyed limiter) and `HolePunching` (30 req/s keyed limiter) — carry explicit rate limiters; `LightClientProtocol` is the only request-serving protocol that does not.

---

### Finding Description

**Entrypoint:** Any peer that speaks the `/ckb/lightclient` protocol (protocol ID 120) can send a `LightClientMessage::GetBlocksProof` message.

**Dispatch path:**

`LightClientProtocol::received` → `try_process` → `GetBlocksProofProcess::execute` [1](#0-0) 

`try_process` dispatches directly to the handler with no rate-limit check: [2](#0-1) 

The `LightClientProtocol` struct carries only `shared: Shared` — no `rate_limiter` field exists anywhere in the crate: [3](#0-2) 

**Work performed per message in `GetBlocksProofProcess::execute`:**

1. Size guard: rejects if `block_hashes().len() > GET_BLOCKS_PROOF_LIMIT` (1 000). A message at exactly the limit passes.
2. Duplicate guard: O(N) hash-set scan.
3. For each of up to 1 000 found hashes: `snapshot.get_block_header()` + `snapshot.get_block_uncles()` + `snapshot.get_block_extension()` = **3 000 synchronous RocksDB reads**.
4. `reply_proof` → `mmr.gen_proof(items_positions)` with up to 1 000 positions — O(N log N) in MMR size. [4](#0-3) [5](#0-4) 

**Contrast with rate-limited protocols:**

`Relayer` enforces 30 req/s per `(PeerIndex, message_type)` before any handler runs: [6](#0-5) 

`HolePunching` enforces the same 30 req/s keyed limiter at the top of `received`: [7](#0-6) 

**Default deployment:** The production `ckb.toml` includes `LightClient` in `support_protocols` by default, so every standard CKB full node is exposed: [8](#0-7) 

The constant confirming the maximum per-message work: [9](#0-8) 

---

### Impact Explanation

Each `GetBlocksProof` message at the 1 000-hash limit causes:
- 3 000 synchronous RocksDB point-reads (header + uncles + extension per block) against the same RocksDB instance used by the main chain processing loop.
- One `mmr.gen_proof(1000 positions)` traversal of the MMR tree.

These are blocking I/O operations executed inside the async tokio handler. Sustained flooding from even a single peer saturates the shared RocksDB I/O bandwidth and the tokio worker threads, directly competing with block validation reads and writes. With `max_peers = 125`, multiple coordinated peers multiply the effect linearly. [10](#0-9) 

---

### Likelihood Explanation

- No PoW, no stake, no privileged role required — any peer that completes the TCP handshake and speaks the light-client protocol can send these messages.
- The attacker needs only a list of 1 000 valid on-chain block hashes (trivially obtained from any block explorer or by syncing the chain).
- The `last_hash` must be on the main chain, but this is also trivially satisfied by using the current tip hash.
- The attack is locally reproducible in a test environment with a single connection.

---

### Recommendation

Add a per-`(PeerIndex, message_type)` `governor::RateLimiter` to `LightClientProtocol`, mirroring the pattern already used in `Relayer` and `HolePunching`. A quota of 1–5 req/s per peer per message type is sufficient for legitimate light-client use while eliminating the amplification attack surface. Additionally, consider moving the synchronous RocksDB reads in `GetBlocksProofProcess::execute` to a `spawn_blocking` task to avoid blocking the async executor.

---

### Proof of Concept

```
1. Sync a CKB node to obtain 1 000 valid main-chain block hashes H_1..H_1000.
2. Connect to the target node on /ckb/lightclient (protocol 120).
3. In a tight loop, send:
     LightClientMessage::GetBlocksProof {
         last_hash: <current tip hash>,
         block_hashes: [H_1, ..., H_1000]
     }
4. Measure RocksDB read IOPS on the target node and concurrent block-validation
   throughput (e.g., via ckb metrics or rocksdb statistics).
5. Assert: block-validation throughput degrades measurably while the flood is active.
```

The absence of any rate-limit check between `received` and `GetBlocksProofProcess::execute` means step 3 will be processed at the full rate the network and the server's async queue allow, with no server-side throttle.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-29)
```rust
pub struct LightClientProtocol {
    /// Sync shared state.
    pub shared: Shared,
}
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-95)
```rust
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
```

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }

    /// Get shared state
    pub fn shared(&self) -> &Arc<SyncShared> {
        &self.shared
    }

    async fn try_process(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        message: packed::RelayMessageUnionReader<'_>,
    ) -> Status {
        // CompactBlock will be verified by POW, it's OK to skip rate limit checking.
        let should_check_rate =
            !matches!(message, packed::RelayMessageUnionReader::CompactBlock(_));

        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** network/src/protocols/hole_punching/mod.rs (L95-107)
```rust
        if self
            .rate_limiter
            .check_key(&(session_id, msg.item_id()))
            .is_err()
        {
            debug!(
                "process {} from {}; result is {}",
                item_name,
                session_id,
                status::StatusCode::TooManyRequests.with_context(msg.item_name())
            );
            return;
        }
```

**File:** resource/ckb.toml (L94-95)
```text
max_peers = 125
max_outbound_peers = 8
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```
