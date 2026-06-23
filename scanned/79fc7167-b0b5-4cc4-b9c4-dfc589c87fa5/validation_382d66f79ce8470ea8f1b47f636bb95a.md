The code confirms this is a real, concrete vulnerability. Here is the full analysis:

**Key findings from the code:**

1. `LightClientProtocol` struct has **no `rate_limiter` field** — unlike `Relayer` which explicitly has one: [1](#0-0) 

2. The `received()` handler dispatches directly to `try_process()` with **zero rate-limit check**: [2](#0-1) 

3. `GET_TRANSACTIONS_PROOF_LIMIT` is 1000, and a request at exactly that limit is **valid** (not banned): [3](#0-2) [4](#0-3) 

4. For each of the 1000 found tx_hashes, `get_transaction_with_info()` is called; then per unique block, `CBMT::build_merkle_proof()` is called; then `reply_proof()` calls `mmr.gen_proof()`: [5](#0-4) [6](#0-5) [7](#0-6) 

5. By contrast, `Relayer` has an explicit rate limiter (30 req/sec per peer+message-type) applied before any processing: [8](#0-7) 

---

### Title
Missing Per-Peer Rate Limiter in `LightClientProtocol` Enables Unbounded Proof Computation DoS — (`util/light-client-protocol-server/src/lib.rs`, `src/components/get_transactions_proof.rs`)

### Summary
`LightClientProtocol` has no rate limiter. Any unprivileged P2P peer can repeatedly send `GetTransactionsProof` messages with up to 1000 valid tx_hashes, triggering up to 1000 `get_transaction_with_info()` DB reads, up to 1000 `CBMT::build_merkle_proof()` calls, and one `mmr.gen_proof()` call per request, with no throttling whatsoever.

### Finding Description
`LightClientProtocol::received()` parses the message and immediately calls `try_process()` with no rate-limit gate. The `GetTransactionsProofProcess::execute()` enforces only an upper bound of 1000 hashes — a request at exactly that limit is fully valid and returns `Status::ok()`, so `should_ban()` returns `None` and the peer is never disconnected. There is no per-peer, per-message-type, or global request budget. The `Relayer` protocol in the same codebase demonstrates the correct pattern: a `governor::RateLimiter` keyed by `(peer, message_type)` is checked before any processing.

### Impact Explanation
A single malicious peer can flood the server with back-to-back `GetTransactionsProof` requests, each causing up to 1000 RocksDB point lookups, 1000 CBMT Merkle tree constructions, and one MMR range proof generation. This saturates I/O and CPU on the full node, degrading or halting its ability to serve other light clients and potentially impacting overall node responsiveness.

### Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light client protocol enabled. No keys, no hashpower, no privileged access. The attacker needs only knowledge of 1000 on-chain tx hashes (trivially obtained from any block explorer). The attack is repeatable indefinitely.

### Recommendation
Add a `governor::RateLimiter` keyed by `(PeerIndex, message_item_id)` to `LightClientProtocol`, mirroring the pattern in `Relayer::new()` and the check in `Relayer::try_process()`. Apply the check at the top of `LightClientProtocol::received()` before dispatching to `try_process()`.

### Proof of Concept
1. Connect a peer to a CKB full node with light client protocol enabled.
2. Collect 1000 distinct valid mainchain tx hashes (from any block explorer).
3. Repeatedly send `GetTransactionsProof { last_hash: <tip>, tx_hashes: [1000 valid hashes] }` in a tight loop.
4. Observe: server CPU and I/O spike; no ban is issued; the peer is never disconnected; response latency for all other light client peers degrades.
5. Confirm: no rate-limit path exists in `LightClientProtocol::received()` or `try_process()`.

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

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
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

**File:** util/light-client-protocol-server/src/components/get_transactions_proof.rs (L82-97)
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
