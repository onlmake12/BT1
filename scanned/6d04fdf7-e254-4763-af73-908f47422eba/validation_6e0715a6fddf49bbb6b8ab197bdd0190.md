### Title
Unbounded MMR Proof Generation in LightClientProtocol Enables Unauthenticated DoS — (`util/light-client-protocol-server/src/lib.rs`)

### Summary

The `LightClientProtocol` server processes `GetBlocksProof` and `GetTransactionsProof` messages from any unprivileged peer with no rate limiting. Each well-formed request with up to 1,000 valid block hashes triggers `mmr.gen_proof(1000_positions)` — O(positions × log N) RocksDB reads — with no throttle, no per-peer budget, and no ban on valid-but-expensive requests. The protocol is **enabled by default** in the shipped `ckb.toml`.

### Finding Description

**Entry point — `LightClientProtocol::received`** [1](#0-0) 

Every incoming message is dispatched immediately. The struct holds only `shared: Shared` — no rate-limiter field exists. [2](#0-1) 

**Per-request limit is 1,000 items, not a rate limit**

`GET_BLOCKS_PROOF_LIMIT = 1000` and `GET_TRANSACTIONS_PROOF_LIMIT = 1000` cap the number of hashes *per message*, but impose no constraint on how often a peer may send such messages. [3](#0-2) 

The check in `GetBlocksProofProcess::execute` rejects oversized messages but accepts any valid 1,000-hash request unconditionally: [4](#0-3) 

**Expensive work per request — `reply_proof`**

For every accepted request, `reply_proof` calls `snapshot.chain_root_mmr(last_block.number() - 1)` and then `mmr.gen_proof(items_positions)`: [5](#0-4) 

`chain_root_mmr` constructs an MMR backed by RocksDB reads: [6](#0-5) 

`gen_proof` for 1,000 positions on a chain of height N performs O(1,000 × log₂ N) store reads — roughly 23,000 reads at N = 10,000,000.

**LightClient is enabled by default**

The default configuration includes `LightClient` in `support_protocols`: [7](#0-6) [8](#0-7) 

**Contrast with HolePunching, which does have rate limiting**

The `HolePunching` protocol uses `governor::RateLimiter` keyed per peer. No equivalent exists anywhere in `LightClientProtocol`. [9](#0-8) 

**`BAD_MESSAGE_BAN_TIME` does not apply**

The ban is only triggered for *malformed* messages. A well-formed 1,000-hash `GetBlocksProof` with a valid `last_hash` returns `Status::ok()` and is never penalised. [10](#0-9) 

### Impact Explanation

A single peer can sustain a flood of max-size `GetBlocksProof` requests. Each request forces ~23,000 RocksDB reads for MMR proof generation plus additional reads for block headers, uncles, and extensions. With `max_peers = 125` inbound connections, the aggregate I/O load can saturate the RocksDB layer shared with block validation and sync, degrading or stalling normal node operation. No funds are at risk, but availability of the full node is compromised.

### Likelihood Explanation

The attack requires only a valid P2P connection and knowledge of any 1,000 block hashes on the main chain — all of which are publicly available. No PoW, no key material, no privileged role. The LightClient protocol is on by default, so every standard CKB full node is exposed.

### Recommendation

Add a per-peer rate limiter to `LightClientProtocol` analogous to the one in `HolePunching` (e.g., `governor::RateLimiter` keyed by `PeerIndex`). Additionally, consider reducing `GET_BLOCKS_PROOF_LIMIT` / `GET_TRANSACTIONS_PROOF_LIMIT` or introducing a per-request work budget tied to the chain height.

### Proof of Concept

1. Connect to a CKB full node (LightClient enabled by default).
2. Collect 1,000 valid block hashes from the public chain.
3. In a tight loop, send `GetBlocksProof { last_hash: <tip_hash>, block_hashes: [1000 valid hashes] }`.
4. Observe RocksDB I/O saturation and increased latency for block validation on the target node.

The per-request cost is deterministic and locally reproducible without mainnet access by running a local node with a simulated chain of sufficient height.

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

**File:** util/light-client-protocol-server/src/lib.rs (L199-216)
```rust
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
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/app-config/src/configs/network.rs (L236-250)
```rust
pub fn default_support_all_protocols() -> Vec<SupportProtocol> {
    vec![
        SupportProtocol::Ping,
        SupportProtocol::Discovery,
        SupportProtocol::Identify,
        SupportProtocol::Feeler,
        SupportProtocol::DisconnectMessage,
        SupportProtocol::Sync,
        SupportProtocol::Relay,
        SupportProtocol::Time,
        SupportProtocol::Alert,
        SupportProtocol::LightClient,
        SupportProtocol::Filter,
        SupportProtocol::HolePunching,
    ]
```

**File:** network/src/protocols/hole_punching/mod.rs (L247-257)
```rust
impl HolePunching {
    pub(crate) fn new(network_state: Arc<NetworkState>) -> Self {
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (CHECK_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        // In the request forwarding process, the same group of from/to should not be received by the same
        // node more than 1 times within one second.
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(1).unwrap());
        let forward_rate_limiter = RateLimiter::hashmap(quota);
```
