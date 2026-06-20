### Title
Missing Per-Peer Rate Limiting in LightClientProtocol Allows Handler Monopolization — (`util/light-client-protocol-server/src/lib.rs`, `src/components/get_blocks_proof.rs`, `src/components/get_transactions_proof.rs`)

---

### Summary

`LightClientProtocol` has no rate limiter of any kind. A single unprivileged peer can flood the handler with back-to-back maximum-size `GetBlocksProof` and `GetTransactionsProof` messages (up to 1 000 items each), each triggering hundreds of DB reads plus synchronous MMR proof generation, starving all other light-client peers of handler time. The `Relayer` and `HolePunching` protocols both carry an explicit `RateLimiter<(PeerIndex, u32)>` and reject excess messages; `LightClientProtocol` carries none.

---

### Finding Description

**No rate limiter in `LightClientProtocol`**

The struct holds only `shared: Shared` and no rate-limiter field. [1](#0-0) 

`received()` parses the message and immediately calls `try_process()` — no quota check, no per-peer token bucket, no per-message-type guard. [2](#0-1) 

A `grep` for `rate_limiter` across the entire `util/light-client-protocol-server/` tree returns zero matches, confirming the absence is complete.

**Contrast: `Relayer` has an explicit rate limiter**

`Relayer::new()` creates a `RateLimiter<(PeerIndex, u32)>` capped at 30 req/s per (peer, message-type) and rejects excess messages before any processing occurs. [3](#0-2) 

`HolePunching` does the same with both a per-(session, message-type) limiter and a per-(from, to) forward limiter. [4](#0-3) 

**Cost of a single max-size request**

`GetBlocksProofProcess::execute()` accepts up to `GET_BLOCKS_PROOF_LIMIT = 1 000` block hashes. [5](#0-4) 

For each found hash it performs three synchronous DB reads (header, uncles, extension), then calls `reply_proof`, which runs a synchronous `mmr.get_root()` and `mmr.gen_proof(items_positions)` over up to 1 000 leaf positions — all inside an `async fn` without yielding. [6](#0-5) 

`GetTransactionsProofProcess::execute()` is similarly bounded at 1 000 tx hashes, but additionally fetches full blocks and builds CBMT Merkle proofs per block before the same MMR path. [7](#0-6) 

`GetLastStateProofProcess::execute()` performs binary-search DB walks across the entire chain height for each sampled difficulty point, bounded at `GET_LAST_STATE_PROOF_LIMIT = 1 000`. [8](#0-7) 

**Sequential handler model**

`CKBHandler::received()` calls `self.handler.received(...).await` — one message at a time per protocol handler instance. All peers sharing the `LightClient` protocol share a single handler task queue. [9](#0-8) 

A peer that continuously sends max-size requests therefore occupies the handler for the full duration of each DB+MMR computation before the next peer's message is dequeued.

---

### Impact Explanation

A single unprivileged peer can cause sustained, measurable latency degradation for every other peer connected to the same node's light-client endpoint. Because the four message types share one handler and none carries a per-type or aggregate quota, alternating `GetBlocksProof` / `GetTransactionsProof` / `GetLastStateProof` messages at maximum item count bypasses any hypothetical per-type limit that might be added later. The main-chain sync and relay handlers are unaffected (separate protocol tasks), so consensus is not at risk, but the light-client service is effectively a single-threaded resource that can be monopolized at zero cost beyond a TCP connection.

---

### Likelihood Explanation

The attack requires only a valid TCP connection to a node that has enabled the light-client server. No PoW, no stake, no privileged role. The attacker needs to know valid block/tx hashes (trivially obtained from any public explorer or by syncing a few blocks), or can use the `last_hash` mismatch path to trigger the cheaper `reply_tip_state` branch in a tight loop. The absence of any rate-limiting code is confirmed by source inspection.

---

### Recommendation

Add a `RateLimiter<(PeerIndex, u32)>` to `LightClientProtocol` (mirroring `Relayer`) and check it at the top of `try_process()` before dispatching to any component. A conservative quota of 5–10 req/s per (peer, message-type) would bound aggregate work while still serving legitimate light clients. Additionally, consider offloading the synchronous MMR proof generation to a blocking thread pool (`tokio::task::spawn_blocking`) to avoid holding the async executor during CPU-intensive proof construction.

---

### Proof of Concept

```
1. Connect to a CKB node with LightClient protocol enabled.
2. Obtain any 1 000 valid main-chain block hashes (e.g., blocks 1–1000).
3. In a tight loop, send:
     GetBlocksProof { last_hash: tip, block_hashes: [h1..h1000] }
     GetTransactionsProof { last_hash: tip, tx_hashes: [t1..t1000] }
   alternating, as fast as the TCP window allows.
4. From a second peer, send a single GetLastState message and measure
   the response latency — it will be queued behind all in-flight
   handler invocations from peer 1.
5. Observe that peer 2's response time grows linearly with the flood
   rate from peer 1, with no server-side rejection or throttling.
```

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

**File:** util/light-client-protocol-server/src/lib.rs (L195-218)
```rust
        let (parent_chain_root, proof) = if last_block.is_genesis() {
            (Default::default(), Default::default())
        } else {
            let snapshot = self.shared.snapshot();
            let mmr = snapshot.chain_root_mmr(last_block.number() - 1);
            let parent_chain_root = match mmr.get_root() {
                Ok(root) => root,
                Err(err) => {
                    let errmsg = format!("failed to generate a root since {err:?}");
                    return StatusCode::InternalError.with_context(errmsg);
                }
            };
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
            (parent_chain_root, proof)
```

**File:** sync/src/relayer/mod.rs (L84-123)
```rust
impl Relayer {
    /// Init relay protocol handle
    ///
    /// This is a runtime relay protocol shared state, and any relay messages will be processed and forwarded by it
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L32-147)
```rust
    pub(crate) async fn execute(self) -> Status {
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendTransactionsProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");

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

        let proved_items = (
            packed::FilteredBlockVec::new_builder()
                .set(filtered_blocks)
                .build(),
            uncles_hash.into(),
            packed::BytesOptVec::new_builder().set(extensions).build(),
        );
        let missing_items = missing.into();

        self.protocol
            .reply_proof::<packed::SendTransactionsProofV1>(
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

**File:** network/src/protocols/mod.rs (L365-384)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
        }

        trace!(
            "[received message]: {}, {}, length={}",
            self.proto_id,
            context.session.id,
            data.len()
        );
        let nc = DefaultCKBProtocolContext {
            proto_id: self.proto_id,
            network_state: Arc::clone(&self.network_state),
            p2p_control: context.control().to_owned().into(),
            async_p2p_control: context.control().to_owned(),
        };
        let peer_index = context.session.id;
        self.handler.received(Arc::new(nc), peer_index, data).await;
    }
```
