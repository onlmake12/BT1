The code evidence is clear. Let me confirm the contrast between `LightClientProtocol` and `Relayer` is complete.

### Title
Missing Per-Peer Rate Limiting in `LightClientProtocol` Enables DB I/O Exhaustion via `GetBlocksProof` Flood — (`util/light-client-protocol-server/src/lib.rs`)

---

### Summary

`LightClientProtocol` processes every inbound light-client message with no per-peer rate limit. A single unprivileged peer can send `GetBlocksProof` messages at maximum network speed, each triggering up to 4 × 1000 RocksDB reads plus an MMR `gen_proof` computation, saturating the node's DB I/O and async executor.

---

### Finding Description

`LightClientProtocol` is defined with only a `shared: Shared` field — no rate-limiter state exists: [1](#0-0) 

Its `received` handler dispatches directly to `try_process` with no throttle check: [2](#0-1) 

`try_process` routes to `GetBlocksProofProcess::execute` with no rate check: [3](#0-2) 

`GetBlocksProofProcess::execute` performs, per message, up to:
- 1 `is_main_chain` check on `last_hash`
- 1000 `is_main_chain` DB lookups (partition loop)
- 1000 `get_block_header` reads
- 1000 `get_block_uncles` reads
- 1000 `get_block_extension` reads
- 1 MMR `gen_proof` computation (in `reply_proof`) [4](#0-3) 

The only guard is a per-message size cap: [5](#0-4) [6](#0-5) 

This caps work *per message* but places no bound on *message rate*.

**Contrast with `Relayer`**, which has an explicit `rate_limiter` field and checks it before every non-PoW message: [7](#0-6) [8](#0-7) 

The `HolePunching` protocol similarly has both a `rate_limiter` and a `forward_rate_limiter`: [9](#0-8) 

`LightClientProtocol` is the only production protocol handler that omits this protection entirely.

---

### Impact Explanation

Each `GetBlocksProof` message with 1000 hashes causes ~4000 synchronous RocksDB point-reads plus an MMR proof generation. A single attacker peer sending these messages in a tight loop can:

1. Saturate RocksDB read bandwidth, stalling block validation and relay for all peers.
2. Monopolize the async executor task that processes light-client messages, delaying other protocol handlers.

The same gap exists for `GetTransactionsProof` (up to 1000 tx lookups + full block reads + CBMT proof per message) and `GetLastStateProof` (binary-search DB reads + MMR root per sampled block). [10](#0-9) [11](#0-10) 

---

### Likelihood Explanation

The attack requires only a persistent TCP connection to the node's P2P port — no credentials, no PoW, no stake. Any node that enables the light-client protocol (the `LightClient` support protocol) is exposed. The attacker controls the exact message rate and can sustain it indefinitely.

---

### Recommendation

Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`, mirroring the `Relayer` pattern: [12](#0-11) 

Check it at the top of `try_process` before dispatching to any component, keyed by `(peer_index, message.item_id())`. A quota of 1–5 requests/second per peer per message type is sufficient for legitimate light-client usage.

---

### Proof of Concept

```
1. Connect to a CKB full node with the LightClient protocol enabled.
2. Obtain any 1000 valid main-chain block hashes (h1..h1000) and the current tip hash.
3. In a tight loop, send:
     GetBlocksProof { block_hashes: [h1..h1000], last_hash: tip }
4. Observe: each message triggers ~4000 RocksDB reads + MMR gen_proof.
   With no rate limit, the node's RocksDB read throughput is fully consumed
   by the single attacker peer, degrading block relay and validation for all
   other peers.
```

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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-75)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = self
            .message
            .tx_hashes()
            .to_entity()
            .into_iter()
            .partition(|tx_hash| {
                snapshot
                    .get_transaction_info(tx_hash)
                    .map(|tx_info| snapshot.is_main_chain(&tx_info.block_hash))
                    .unwrap_or_default()
            });

        let mut txs_in_blocks = HashMap::new();
        for tx_hash in found {
            let (tx, tx_info) = snapshot
                .get_transaction_with_info(&tx_hash)
                .expect("tx exists");
            txs_in_blocks
                .entry(tx_info.block_hash)
                .or_insert_with(Vec::new)
                .push((tx, tx_info.index));
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-205)
```rust
    pub(crate) async fn execute(self) -> Status {
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```
