### Title
Unbounded `GetBlocksProof` Request Flooding via All-Missing Block Hashes Triggers Unlimited DB Lookups and MMR Root Computation with No Rate Limit or Peer Ban — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`)

---

### Summary

An unprivileged remote peer can repeatedly send `GetBlocksProof` messages containing up to 1000 unique random (non-existent) block hashes paired with a valid main-chain `last_hash`. For each such request the server performs 1000 `COLUMN_INDEX` DB reads, one MMR root computation, and builds and sends a full `SendBlocksProofV1` response — all returning `Status::ok()`, which means the peer is never banned. There is no rate limiter on the light-client protocol. The attacker's cost is negligible (one small P2P message per round trip); the server's cost scales linearly with the request rate.

---

### Finding Description

**Entrypoint** — `LightClientProtocol::received` in `util/light-client-protocol-server/src/lib.rs` accepts any well-formed `LightClientMessage` from any connected peer and dispatches it to `try_process`. [1](#0-0) 

**Size guard** — `GET_BLOCKS_PROOF_LIMIT = 1000` is a *per-message* cap, not a rate limit. A message with exactly 1000 hashes passes. [2](#0-1) [3](#0-2) 

**`last_hash` guard** — if `last_hash` is not on the main chain the handler returns early. The attacker supplies a valid main-chain hash (e.g. the current tip, which is public), so this guard is bypassed. [4](#0-3) 

**1000 DB lookups** — the partition call invokes `snapshot.is_main_chain(block_hash)` for every hash. `is_main_chain` is a synchronous `COLUMN_INDEX` RocksDB point-read per hash. [5](#0-4) [6](#0-5) 

**MMR root computation** — `reply_proof` always calls `mmr.get_root()` for any non-genesis `last_block`, regardless of whether `positions` is empty. When all 1000 hashes are missing, `positions` is empty so `gen_proof` is skipped, but `get_root()` still traverses the MMR store. [7](#0-6) 

**No ban, no rate limit** — the handler returns `Status::ok()` for an all-missing response. The `received` dispatcher only bans on `status.should_ban()`, which is false for `ok`. There is no `RateLimiter` anywhere in the light-client protocol (contrast with the relayer, which has one). [8](#0-7) 

---

### Impact Explanation

Each request costs the attacker ~100 bytes on the wire. The server pays: 1000 synchronous RocksDB point-reads (COLUMN_INDEX), one MMR root traversal, and one serialized response. At even modest request rates (e.g. 100 req/s from a single peer) this is 100 000 DB reads/s and 100 MMR root computations/s sustained indefinitely. Because the full node's RocksDB instance is shared with block sync and chain validation, saturating it with COLUMN_INDEX reads degrades block processing and peer sync, causing indirect CKB network congestion. Multiple coordinated peers multiply the effect linearly.

---

### Likelihood Explanation

The attack requires only a standard P2P connection to a node that has the light-client protocol enabled. No key, no PoW, no privileged role. The attacker needs one valid main-chain block hash (trivially obtained from any block explorer or by first sending `GetLastState`). The attack is fully automatable and locally testable.

---

### Recommendation

1. **Per-peer rate limiting** on `GetBlocksProof` (and `GetTransactionsProof`, `GetLastStateProof`) analogous to the `RateLimiter<(PeerIndex, u32)>` already used in the relayer.
2. **Penalise all-missing responses**: if the ratio of missing to requested hashes exceeds a threshold, apply a short ban or score penalty to the peer.
3. **Reduce `GET_BLOCKS_PROOF_LIMIT`** or require a minimum fraction of hashes to be on-chain before proceeding with the full partition scan.

---

### Proof of Concept

```
1. Connect to a CKB full node with light-client protocol enabled.
2. Obtain any valid main-chain block hash H (e.g. via GetLastState → SendLastState).
3. In a tight loop:
     Send GetBlocksProof {
         last_hash:    H,
         block_hashes: [rand_hash_1, ..., rand_hash_1000]  // 1000 unique random 32-byte values
     }
4. Observe: peer is never banned; server responds with SendBlocksProofV1 containing
   missing_block_hashes of length 1000 every time.
5. Measure: COLUMN_INDEX read rate on the server spikes to (loop_rate × 1000) reads/s;
   block-sync latency increases proportionally.
```

### Citations

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

**File:** util/light-client-protocol-server/src/lib.rs (L199-208)
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
```

**File:** util/light-client-protocol-server/src/constant.rs (L5-5)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L44-50)
```rust
        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendBlocksProof>(self.peer, self.nc)
                .await;
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-74)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));
```

**File:** store/src/store.rs (L278-281)
```rust
    /// Returns true if the block is on the main chain.
    fn is_main_chain(&self, hash: &packed::Byte32) -> bool {
        self.get(COLUMN_INDEX, hash.as_slice()).is_some()
    }
```
