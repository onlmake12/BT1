### Title
Unbounded Request Rate on `GetTransactionsProof` Handler Enables CPU/IO Exhaustion — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

### Summary

The `LightClientProtocol` handler has no per-peer rate limiter. Any connected light-client peer can continuously send `GetTransactionsProof` messages at the protocol maximum of 1000 tx hashes per message, each in a distinct block, forcing the server to perform up to 1000 RocksDB block reads, 1000 CBMT Merkle proof computations, 1000 uncle reads, and one MMR `gen_proof` call over 1000 positions — with no throttling between requests.

### Finding Description

`GetTransactionsProofProcess::execute` enforces only two guards:

- Empty list rejection [1](#0-0) 
- Count > 1000 rejection [2](#0-1) 

For a valid 1000-tx request where each tx is in a different block, the handler iterates over every unique block and calls `snapshot.get_block()`, `CBMT::build_merkle_proof()` (hashing all transactions in the block), and `snapshot.get_block_uncles()` per block: [3](#0-2) 

It then calls `reply_proof`, which invokes `mmr.gen_proof(items_positions)` over all 1000 MMR leaf positions: [4](#0-3) 

The `LightClientProtocol` struct contains only `shared: Shared` — no rate limiter field exists: [5](#0-4) 

The `received` → `try_process` dispatch path applies zero rate-limiting checks before invoking the handler: [6](#0-5) 

This is in direct contrast to the `Relayer` protocol, which gates every non-PoW message through a per-peer, per-message-type rate limiter (30 req/s cap) before any processing: [7](#0-6) 

The `constant.rs` file confirms the limit is 1000 for all three heavy proof handlers: [8](#0-7) 

### Impact Explanation

A single unprivileged peer can saturate the server's RocksDB I/O and CPU by pipelining maximum-size `GetTransactionsProof` requests. Each request is O(N) in DB reads and O(N log H) in MMR proof generation (N=1000, H=chain height). Without rate limiting, the server processes these as fast as the network delivers them, degrading responsiveness for all other peers. Impact is local node performance degradation (Note, 0–500 points).

### Likelihood Explanation

Any peer that has synced enough chain data to know 1000 committed tx hashes (all public on-chain data) can execute this. No special privilege, key, or hashpower is required. The attacker only needs a single P2P connection to the light-client protocol endpoint.

### Recommendation

Add a per-peer rate limiter to `LightClientProtocol` mirroring the pattern in `Relayer`:
- Add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field to `LightClientProtocol`.
- In `try_process`, check `rate_limiter.check_key(&(peer_index, message.item_id()))` before dispatching, returning `StatusCode::TooManyRequests` on failure.
- Apply a conservative quota (e.g., 5–10 heavy proof requests per second per peer) given the per-request cost.

### Proof of Concept

1. Connect a peer to the server's light-client protocol port.
2. Collect 1000 committed tx hashes from distinct blocks (public chain data).
3. In a tight loop, send `GetTransactionsProof { last_hash: <tip>, tx_hashes: [1000 hashes] }`.
4. Measure server CPU, RocksDB read latency, and response latency to other peers — both will degrade proportionally to request rate.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-35)
```rust
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-126)
```rust
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

**File:** util/light-client-protocol-server/src/lib.rs (L55-93)
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
