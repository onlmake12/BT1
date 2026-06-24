Audit Report

## Title
Unbounded DB/CPU Load via `GetTransactionsProof` with No Rate Limiting in `LightClientProtocol` — (File: `util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
`LightClientProtocol` contains no per-peer or per-message-type rate limiter. Any unauthenticated peer can send a continuous stream of well-formed `GetTransactionsProof` messages, each containing up to 1000 transaction hashes spread across up to 1000 distinct blocks, forcing the server to perform thousands of RocksDB reads, CBMT merkle proof computations, and MMR proof generation per message with no throttle. The attacker's cost is a single TCP connection and a few hundred bytes per message; the server's cost is thousands of I/O and CPU operations per message.

## Finding Description
`GET_TRANSACTIONS_PROOF_LIMIT` is set to 1000 in `util/light-client-protocol-server/src/constant.rs` L7. [1](#0-0) 

`GetTransactionsProofProcess::execute` enforces only an empty-check and this count check before proceeding with all DB work. [2](#0-1) 

For each unique block containing a found transaction, the handler calls `snapshot.get_block()` (full block load from RocksDB), computes a CBMT merkle proof over all transactions in that block, and loads block uncles and extensions. [3](#0-2) 

`reply_proof` then calls `mmr.gen_proof(items_positions)` with up to 1000 MMR positions — an O(positions × log(chain_height)) DB traversal. [4](#0-3) 

`LightClientProtocol` contains only `shared: Shared` — no rate limiter field exists anywhere in the crate (confirmed by grep for `governor`, `RateLimiter`, `rate_limit` returning zero matches). [5](#0-4) 

The `received` handler dispatches directly to `try_process` with zero throttling; the only ban path is for malformed (unparseable) messages, not for well-formed expensive ones. [6](#0-5) 

By contrast, `Relayer` carries an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field keyed by `(peer, message_type)` at 30 req/sec, checked before any processing. [7](#0-6) 

## Impact Explanation
A single attacker peer can saturate the CKB node's RocksDB I/O and CPU by sending a continuous stream of `GetTransactionsProof` messages. Each message triggers up to: 1000 `get_transaction_info` reads, 1000 `get_transaction_with_info` reads, 1000 `get_block` full-block reads, 1000 CBMT proof builds, 1000 `get_block_uncles` + `get_block_extension` reads, and 1 `mmr.gen_proof(1000 positions)` call. This can stall block processing and sync on the same node, constituting a **High** impact: "Vulnerabilities which could easily crash a CKB node" and "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation
`SupportProtocols::LightClient` is a production protocol enabled on full nodes. Any peer that can reach the node's P2P port can open a light client session and send `GetTransactionsProof` messages without authentication or prior state. The 5-minute ban applies only to parse failures, not to well-formed expensive requests. The attack is trivially scriptable with a standard CKB P2P client library.

## Recommendation
Add a per-peer, per-message-type rate limiter to `LightClientProtocol` mirroring the pattern in `Relayer`: add a `rate_limiter: RateLimiter<(PeerIndex, u32)>` field, initialize it in `LightClientProtocol::new`, and check it at the top of `try_process` before dispatching to any handler. [8](#0-7)  Additionally, consider reducing `GET_TRANSACTIONS_PROOF_LIMIT` or adding a secondary limit on the number of distinct blocks that may be referenced in a single request. [1](#0-0) 

## Proof of Concept
1. Connect to a CKB full node with the light client protocol enabled.
2. Collect 1000 confirmed transaction hashes, each from a different block.
3. In a tight loop, send `GetTransactionsProof` messages with `last_hash` set to the current tip and `tx_hashes` set to all 1000 hashes.
4. Observe that each message causes ~3000+ RocksDB reads and significant CPU usage on the server, while the attacker sends ~300 bytes per message.
5. Measure server CPU/IO saturation vs. attacker bandwidth cost to confirm the asymmetric amplification and node stall.

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L7-7)
```rust
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L33-39)
```rust
        if self.message.tx_hashes().is_empty() {
            return StatusCode::MalformedProtocolMessage.with_context("no transaction");
        }

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

**File:** sync/src/relayer/mod.rs (L63-123)
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
