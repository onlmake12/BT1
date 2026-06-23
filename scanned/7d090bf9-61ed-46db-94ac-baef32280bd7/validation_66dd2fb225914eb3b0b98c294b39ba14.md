Based on my investigation, here is the complete analysis:

**Key findings confirmed:**

1. `LightClientProtocol::try_process` has **no rate limiter** — the struct only holds `shared: Shared` with no `rate_limiter` field. [1](#0-0) [2](#0-1) 

2. By contrast, `Relayer` explicitly holds a `governor::RateLimiter<(PeerIndex, u32)>` and checks it at the top of `try_process` before dispatching any message. [3](#0-2) [4](#0-3) 

3. `HolePunching` also has a `governor::RateLimiter` keyed by `(PeerIndex, msg_item_id)` checked in `received`. [5](#0-4) [6](#0-5) 

4. The per-message item limits are all 1000: [7](#0-6) 

5. Work done per message at max item count:
   - `GetBlocksProof`: up to 1000 × (`is_main_chain` + `get_block_header` + `get_block_uncles` + `get_block_extension`) DB reads, then `mmr.gen_proof(positions)`. [8](#0-7) 
   - `GetTransactionsProof`: up to 1000 × (`get_transaction_info` + `get_transaction_with_info` + `get_block` + CBMT proof build) DB reads, then MMR proof. [9](#0-8) 
   - `GetLastStateProof`: binary-search DB reads per difficulty entry (up to 1000 entries), then per-block `mmr.get_root()` in `complete_headers`. [10](#0-9) [11](#0-10) 

---

### Title
Missing Per-Peer Rate Limiter in `LightClientProtocol::try_process` Enables Unbounded DB-Read and MMR-Computation Flood — (`util/light-client-protocol-server/src/lib.rs`)

### Summary
`LightClientProtocol::try_process` dispatches `GetBlocksProof`, `GetTransactionsProof`, and `GetLastStateProof` messages with no per-peer or global rate limit. Each message may carry up to 1000 items and triggers proportional DB reads plus MMR proof generation. An unprivileged remote peer can flood the server with rapid successive max-size requests, monopolizing the async task pool's I/O and CPU budget and degrading full-node performance for all other peers.

### Finding Description
`LightClientProtocol` holds only `shared: Shared` — no `rate_limiter` field exists. The `received` handler calls `try_process` directly after message deserialization with no throttle check. In contrast, both `Relayer` and `HolePunching` maintain a `governor::RateLimiter<(PeerIndex, u32)>` and reject excess messages with `StatusCode::TooManyRequests` before any work is done. The three expensive handlers each enforce an item-count ceiling of 1000 (`GET_BLOCKS_PROOF_LIMIT`, `GET_TRANSACTIONS_PROOF_LIMIT`, `GET_LAST_STATE_PROOF_LIMIT`) but impose no frequency ceiling per peer. A single peer can therefore issue back-to-back max-size requests at line rate, each triggering up to ~4000 DB reads (for `GetBlocksProof`) or equivalent MMR root computations (for `GetLastStateProof`'s `complete_headers` loop), with no mechanism to shed load.

### Impact Explanation
Sustained high-throughput light-client requests from one peer saturate the shared `Shared` snapshot read path and the async executor, increasing block-processing and sync latency for all other peers. This is a performance-degradation DoS scoped to a single full node, not a consensus or fund-safety issue.

### Likelihood Explanation
Any peer that negotiates the light-client protocol sub-stream can exploit this without any privilege. The attack requires only a TCP connection and knowledge of the protocol message format, both of which are publicly documented.

### Recommendation
Add a `governor::RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol` (mirroring `Relayer::rate_limiter`) and check it at the top of `try_process`, returning `StatusCode::TooManyRequests` on excess. A quota of 10–30 requests per second per peer per message type is consistent with the existing Relayer and HolePunching limits.

### Proof of Concept
1. Connect a peer to a CKB full node that has the light-client protocol enabled.
2. In a tight loop, send `GetBlocksProof` messages each containing 1000 distinct block hashes and a valid `last_hash`.
3. Observe that each message is processed without rejection, triggering ~4000 DB reads and one MMR proof generation per message.
4. Measure full-node block-processing latency under this load versus baseline; expect measurable degradation.

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L26-36)
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

**File:** sync/src/relayer/mod.rs (L63-82)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

#[derive(Debug, Eq, PartialEq)]
pub enum ReconstructionResult {
    Block(BlockView),
    Missing(Vec<usize>, Vec<usize>),
    Collided,
    Error(Status),
}

/// Relayer protocol handle
pub struct Relayer {
    chain: ChainController,
    pub(crate) shared: Arc<SyncShared>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
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

**File:** network/src/protocols/hole_punching/mod.rs (L31-47)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;

/// Hole Punching Protocol
pub(crate) struct HolePunching {
    network_state: Arc<NetworkState>,
    bind_addr: Option<SocketAddr>,
    // Request timestamp recorded
    inflight_requests: HashMap<PeerId, u64>,
    // Delivered timestamp recorded
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L54-126)
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

        let mut positions = Vec::with_capacity(txs_in_blocks.len());
        let mut filtered_blocks = Vec::with_capacity(txs_in_blocks.len());
        let mut uncles_hash = Vec::with_capacity(txs_in_blocks.len());
        let mut extensions = Vec::with_capacity(txs_in_blocks.len());

        for (block_hash, txs_and_tx_indices) in txs_in_blocks.into_iter() {
            let block = snapshot
                .get_block(&block_hash)
                .expect("block should be in store");
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
                .transactions(txs)
                .proof(
                    packed::MerkleProof::new_builder()
                        .indices(merkle_proof.indices().as_ref())
                        .lemmas(merkle_proof.lemmas().to_owned())
                        .build(),
                )
                .build();

            positions.push(leaf_index_to_pos(block.number()));
            filtered_blocks.push(filtered_block);

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
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
