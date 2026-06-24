Audit Report

## Title
Missing Per-Peer Rate Limiter in `LightClientProtocol` Enables Unbounded Proof Computation DoS — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/components/get_transactions_proof.rs`)

## Summary
`LightClientProtocol` contains no rate-limiting mechanism of any kind. Any peer with a valid P2P connection can repeatedly send `GetTransactionsProof` messages containing up to 1000 tx hashes, each triggering up to 1000 RocksDB reads, 1000 CBMT Merkle proof constructions, and one MMR range proof generation, with no throttling and no ban. This can saturate CPU and I/O on the full node, crashing or severely degrading it.

## Finding Description
`LightClientProtocol` is defined with only a `shared: Shared` field — no rate limiter is present. [1](#0-0) 

`LightClientProtocol::received()` parses the message and immediately calls `self.try_process()` with no rate-limit gate of any kind. [2](#0-1) 

`GetTransactionsProofProcess::execute()` enforces only an upper bound of 1000 hashes. A request at exactly 1000 is fully valid and returns `Status::ok()`, so `should_ban()` returns `None` and the peer is never disconnected. [3](#0-2) 

For each of the up to 1000 found tx hashes, `get_transaction_with_info()` is called (a RocksDB point lookup): [4](#0-3) 

For each unique block containing those transactions, `CBMT::build_merkle_proof()` is called: [5](#0-4) 

Finally, `reply_proof()` calls `mmr.gen_proof()` to generate an MMR range proof: [6](#0-5) 

The network layer's `CKBHandler::received()` applies no global rate limiting before dispatching to protocol handlers — it only checks `is_active()`. [7](#0-6) 

By contrast, `Relayer` explicitly constructs a `governor::RateLimiter` keyed by `(peer, message_type)` at 30 req/sec and checks it at the top of `try_process()` before any work is done: [8](#0-7) 

A grep across the entire `util/light-client-protocol-server/` directory confirms zero occurrences of `rate_limit`, `RateLimiter`, or `governor`.

## Impact Explanation
A single malicious peer can send `GetTransactionsProof` messages in a tight loop, each with 1000 valid tx hashes. Each request causes up to 1000 RocksDB point lookups, 1000 CBMT Merkle tree constructions, and one MMR range proof generation. This saturates I/O and CPU on the full node, which can crash the node or halt its ability to serve other peers. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light client protocol enabled. No keys, no hashpower, no privileged access are needed. The attacker needs only 1000 on-chain tx hashes, trivially obtained from any block explorer. The attack is repeatable indefinitely because valid requests never trigger a ban.

## Recommendation
Add a `governor::RateLimiter` keyed by `(PeerIndex, message_item_id)` to `LightClientProtocol`, mirroring the pattern in `Relayer::new()` and the check in `Relayer::try_process()`. Apply the check at the top of `LightClientProtocol::received()` before dispatching to `try_process()`. A quota of 30 req/sec per peer per message type (matching `Relayer`) is a reasonable starting point; a tighter quota may be appropriate given the higher per-request cost of proof generation.

## Proof of Concept
1. Connect a peer to a CKB full node with the light client protocol enabled.
2. Collect 1000 distinct valid mainchain tx hashes from any block explorer.
3. In a tight loop, send `GetTransactionsProof { last_hash: <current tip>, tx_hashes: [1000 valid hashes] }` over the P2P connection.
4. Observe: server CPU and I/O spike; no ban is issued; the peer is never disconnected; response latency for all other light client peers degrades or the node becomes unresponsive.
5. Confirm: no rate-limit path exists anywhere in `LightClientProtocol::received()`, `try_process()`, or the network-layer `CKBHandler::received()`.

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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L37-39)
```rust
        if self.message.tx_hashes().len() > constant::GET_TRANSACTIONS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many transactions");
        }
```

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L67-75)
```rust
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L86-97)
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

**File:** sync/src/relayer/mod.rs (L88-123)
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
