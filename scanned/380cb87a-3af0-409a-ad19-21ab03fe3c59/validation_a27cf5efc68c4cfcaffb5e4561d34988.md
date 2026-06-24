Audit Report

## Title
Unbounded Per-Peer MMR Proof Request Rate in `LightClientProtocol` Enables I/O Exhaustion DoS — (`util/light-client-protocol-server/src/lib.rs`)

## Summary

`LightClientProtocol` accepts `GetBlocksProof` and `GetTransactionsProof` messages from any peer with no per-peer rate limiting. Each valid request with up to 1,000 block hashes unconditionally triggers `mmr.gen_proof(1000_positions)`, which performs O(positions × log₂ N) RocksDB reads. Because the protocol is enabled by default and no throttle exists, a single attacker can saturate the RocksDB layer shared with block validation, degrading or stalling normal node operation.

## Finding Description

**No rate-limiter in `LightClientProtocol`:** The struct holds only `pub shared: Shared` with no rate-limiter field. [1](#0-0) 

`received()` dispatches every well-formed message immediately to `try_process` with no throttle check. [2](#0-1) 

**Per-message cap is not a rate limit:** `GET_BLOCKS_PROOF_LIMIT = 1000` and `GET_TRANSACTIONS_PROOF_LIMIT = 1000` bound only the number of hashes per message, not how frequently a peer may send such messages. [3](#0-2) 

`GetBlocksProofProcess::execute` rejects oversized messages but accepts any valid 1,000-hash request unconditionally and proceeds to proof generation. [4](#0-3) 

**Expensive work per request:** `reply_proof` calls `snapshot.chain_root_mmr(last_block.number() - 1)` and then `mmr.gen_proof(items_positions)` for every accepted request. [5](#0-4) 

`chain_root_mmr` constructs an MMR backed by RocksDB reads proportional to chain height. [6](#0-5) 

**`BAD_MESSAGE_BAN_TIME` does not apply:** The ban is only triggered for malformed messages. A well-formed 1,000-hash `GetBlocksProof` with a valid `last_hash` returns `Status::ok()` and is never penalised. [7](#0-6) 

**LightClient is enabled by default:** Both the shipped `ckb.toml` and `default_support_all_protocols()` include `LightClient`. [8](#0-7) [9](#0-8) 

**Contrast with `HolePunching`:** That protocol uses `governor::RateLimiter` keyed per peer; no equivalent exists anywhere under `util/light-client-protocol-server/`. [10](#0-9) 

## Impact Explanation

A single attacker with a valid P2P connection can sustain a continuous flood of max-size `GetBlocksProof` requests. At chain height N = 10,000,000, each request forces ~23,000 RocksDB reads for MMR proof generation plus additional reads for block headers, uncles, and extensions. The RocksDB instance is shared with block validation and sync; saturating it stalls those subsystems. With `max_peers = 125` inbound connections, the aggregate I/O load can easily crash or stall a CKB node. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node"** (10001–15000 points).

## Likelihood Explanation

The attack requires only a valid P2P connection and 1,000 publicly known block hashes — no PoW, no key material, no privileged role. The LightClient protocol is on by default, so every standard CKB full node is exposed. The per-request cost is deterministic and the attack is trivially repeatable in a tight loop.

## Recommendation

Add a per-peer rate limiter to `LightClientProtocol` analogous to the one in `HolePunching` (e.g., `governor::RateLimiter` keyed by `PeerIndex`). Additionally, consider reducing `GET_BLOCKS_PROOF_LIMIT` / `GET_TRANSACTIONS_PROOF_LIMIT` or introducing a per-request work budget tied to chain height.

## Proof of Concept

1. Connect to a standard CKB full node (LightClient enabled by default).
2. Collect 1,000 valid block hashes from the public chain (all publicly available).
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

**File:** util/light-client-protocol-server/src/lib.rs (L198-216)
```rust
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

**File:** util/app-config/src/configs/network.rs (L236-251)
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
}
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
