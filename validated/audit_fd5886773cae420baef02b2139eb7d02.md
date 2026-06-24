All cited code is confirmed accurate. Let me verify one more detail about whether the light client protocol is optional/default-enabled.

Audit Report

## Title
Unbounded Per-Request DB/CPU Amplification with No Rate Limiting in `GetTransactionsProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
Any unauthenticated P2P peer can send a single `GetTransactionsProof` message containing up to 1000 transaction hashes, each from a distinct block, forcing the server to perform up to 1000 `get_block` reads, 1000 `CBMT::build_merkle_proof` computations, 1000 `get_block_uncles`/`get_block_extension` reads, and one `mmr.gen_proof(1000 positions)` call — all with no per-peer rate limiting and no ban for well-formed requests. This is repeatable indefinitely from a single connection, enabling sustained CPU and RocksDB I/O exhaustion sufficient to crash or render unresponsive a CKB node running the light client protocol server.

## Finding Description

**Guard is count-only, no deduplication**: `GetTransactionsProofProcess::execute` checks only for empty input and count exceeding `GET_TRANSACTIONS_PROOF_LIMIT = 1000`. There is no `HashSet` deduplication of tx hashes. [1](#0-0) 

By contrast, `GetBlocksProofProcess::execute` explicitly rejects duplicate block hashes via a `HashSet` and returns `MalformedProtocolMessage` (which triggers a ban): [2](#0-1) 

**HashMap grouping only deduplicates within a block**: The `txs_in_blocks` HashMap groups transactions by `block_hash`, so multiple txs in the *same* block share one `get_block` call. However, if all 1000 tx hashes are from 1000 *different* blocks, the map has 1000 entries and the loop executes 1000 times: [3](#0-2) 

Per iteration: `get_block` (full block deserialization), `CBMT::build_merkle_proof` over all block transactions, `get_block_uncles`, `get_block_extension`. Then `reply_proof` calls `mmr.gen_proof(1000 positions)`: [4](#0-3) [5](#0-4) 

**No rate limiting in `LightClientProtocol`**: The struct holds only `shared: Shared` — no rate limiter field. The `received` handler calls `try_process` directly with no throttle: [6](#0-5) [7](#0-6) 

This contrasts with `Relayer`, which has a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and checks it on every message before dispatch: [8](#0-7) 

**No ban for valid requests**: `should_ban()` only returns `Some` for 4xx status codes. A well-formed request with 1000 valid tx hashes returns `Status::ok()` (200) — the peer is never disconnected or penalized: [9](#0-8) 

**LightClient is enabled by default**: The default `ckb.toml` includes `"LightClient"` in `support_protocols`, and the launcher unconditionally registers `LightClientProtocol` when this flag is present: [10](#0-9) [11](#0-10) 

## Impact Explanation

A single max-size request causes up to 1000 full-block RocksDB reads, 1000 CBMT Merkle proof computations, 1000 uncle/extension reads, and one 1000-position MMR proof generation — all synchronously on the node's async handler. With no rate limiting and no ban, an attacker sustains this indefinitely from a single connection, exhausting I/O bandwidth and CPU. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, as sustained saturation of RocksDB reads and CPU-bound proof computations will render the node unresponsive to legitimate peers and can cause it to crash under prolonged attack.

## Likelihood Explanation

The light client protocol server is enabled by default in the standard CKB configuration and is accessible to any peer that connects and speaks the protocol — no authentication, no PoW, no stake required. The attacker only needs 1000 real confirmed transaction hashes from distinct blocks, trivially obtained from any public block explorer or by syncing the chain. The attack is repeatable with no consequence to the attacker: the peer is never banned for sending well-formed requests.

## Recommendation

1. **Add a rate limiter** to `LightClientProtocol` keyed by `(PeerIndex, message_type)`, mirroring the `RateLimiter<(PeerIndex, u32)>` pattern in `Relayer`.
2. **Add deduplication** of `tx_hashes` in `GetTransactionsProofProcess::execute` using a `HashSet`, mirroring the check in `GetBlocksProofProcess::execute`, and return `StatusCode::MalformedProtocolMessage` (which triggers a ban) on duplicates.
3. **Cap the number of distinct blocks** referenced per request (e.g., 32–64) independently of the tx count limit, to bound the per-request DB amplification factor.

## Proof of Concept

1. Sync a CKB node with the default configuration (LightClient protocol enabled).
2. Identify 1000 confirmed transactions each in a distinct block (trivial from chain data or a block explorer).
3. Connect as a P2P peer speaking the light client protocol and send a `GetTransactionsProof` message with all 1000 tx hashes and a valid `last_hash`.
4. Measure wall-clock time and RocksDB read count for this request vs. a single-tx baseline — expect ~1000× amplification.
5. Repeat in a tight loop from the same peer — observe sustained CPU/IO saturation with no ban or throttle applied, eventually causing the node to become unresponsive or crash.

### Citations

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-39)
```rust
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L66-75)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L62-70)
```rust
        let mut uniq = HashSet::new();
        if !block_hashes
            .iter()
            .chain([last_block_hash].iter())
            .all(|hash| uniq.insert(hash))
        {
            return StatusCode::MalformedProtocolMessage
                .with_context("duplicate block hash exists");
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

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/launcher/src/lib.rs (L467-476)
```rust
        if support_protocols.contains(&SupportProtocol::LightClient) {
            let light_client = LightClientProtocol::new(shared.clone());
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::LightClient,
                Box::new(light_client),
                Arc::clone(&network_state),
            ));
        } else {
            flags.remove(Flags::LIGHT_CLIENT);
        }
```
