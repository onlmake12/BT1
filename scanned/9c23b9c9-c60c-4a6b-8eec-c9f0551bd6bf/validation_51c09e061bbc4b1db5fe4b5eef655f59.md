Looking at the code carefully across the relevant files:

### Title
Unbounded Per-Request DB/CPU Amplification in `GetTransactionsProofProcess::execute` with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

---

### Summary

Any unprivileged P2P peer can send a single `GetTransactionsProof` message containing up to 1000 distinct transaction hashes, each confirmed in a different block. The server performs up to 1000 `get_block` reads, 1000 `CBMT::build_merkle_proof` computations, 1000 `get_block_uncles`/`get_block_extension` reads, and one `mmr.gen_proof(1000 positions)` call — all synchronously, with no per-peer rate limiting and no ban for valid requests. This can be repeated indefinitely.

---

### Finding Description

**Entrypoint**: Any peer connecting over P2P and speaking the light client protocol can send a `GetTransactionsProof` message. The handler is `GetTransactionsProofProcess::execute`.

**Count check only, no deduplication**: The only guard is a count check against `GET_TRANSACTIONS_PROOF_LIMIT = 1000`. [1](#0-0) 

There is no deduplication of tx hashes. Compare with `GetBlocksProofProcess::execute`, which explicitly rejects duplicate block hashes via a `HashSet`: [2](#0-1) 

**HashMap grouping only deduplicates within a block**: The `txs_in_blocks` HashMap groups transactions by `block_hash`, so multiple txs in the *same* block share one `get_block` call. But if all 1000 tx hashes are from 1000 *different* blocks, the HashMap has 1000 entries and the loop executes 1000 times: [3](#0-2) 

Per iteration: `get_block`, `CBMT::build_merkle_proof` over all block transactions, `get_block_uncles`, `get_block_extension`. Then `reply_proof` calls `mmr.gen_proof(1000 positions)`: [4](#0-3) 

**No rate limiting in `LightClientProtocol`**: The `LightClientProtocol` struct holds only `shared: Shared` — no rate limiter field. The `received` handler calls `try_process` directly with no throttle: [5](#0-4) [6](#0-5) 

This contrasts with `Relayer`, which has a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and checks it on every message before dispatch. [7](#0-6) 

**No ban for valid requests**: `StatusCode::MalformedProtocolMessage` (which triggers a ban) is only returned for empty or oversized requests. A well-formed request with 1000 valid tx hashes returns `Status::ok()` — the peer is never disconnected. [8](#0-7) 

---

### Impact Explanation

A single request causes up to:
- 1000 × `get_block` (full block deserialization from RocksDB)
- 1000 × `CBMT::build_merkle_proof` (iterates all transactions in each block)
- 1000 × `get_block_uncles` + `get_block_extension`
- 1 × `mmr.gen_proof(1000 positions)`

With no rate limiting and no ban, an attacker sustains this indefinitely from a single connection, exhausting I/O bandwidth and CPU on the full node serving the light client protocol.

---

### Likelihood Explanation

The light client protocol server is accessible to any peer that connects and speaks the protocol — no authentication, no PoW, no stake. The attacker only needs to know 1000 real confirmed transaction hashes (trivially obtained from any block explorer or by syncing the chain). The attack is repeatable with no consequence to the attacker.

---

### Recommendation

1. **Add a rate limiter** to `LightClientProtocol` keyed by `(PeerIndex, message_type)`, mirroring the pattern in `Relayer`.
2. **Add deduplication** of `tx_hashes` in `GetTransactionsProofProcess::execute`, mirroring the `HashSet` check in `GetBlocksProofProcess::execute`.
3. Consider capping the number of *distinct blocks* referenced per request (e.g., 32 or 64) independently of the tx count limit.

---

### Proof of Concept

1. Sync a CKB node with the light client protocol server enabled.
2. Identify 1000 confirmed transactions each in a distinct block (trivial from chain data).
3. Connect as a P2P peer and send a `GetTransactionsProof` message with all 1000 tx hashes and a valid `last_hash`.
4. Measure wall-clock time and RocksDB read count for this request vs. a single-tx baseline.
5. Repeat in a tight loop from the same peer — observe sustained CPU/IO saturation with no ban or throttle applied.

### Citations

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

**File:** sync/src/relayer/mod.rs (L81-123)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}

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
