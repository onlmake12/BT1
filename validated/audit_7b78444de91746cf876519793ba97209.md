Let me examine the relevant code files to trace the execution path and check for existing guards.

### Title
Unbounded I/O Amplification via `GetBlocksProof` with No Rate Limiting on `LightClientProtocol` — (`util/light-client-protocol-server/src/components/get_blocks_proof.rs`, `lib.rs`)

---

### Summary

The `LightClientProtocol` message handler has **no per-peer rate limiter** of any kind. A single well-formed `GetBlocksProof` message carrying the maximum 1,000 valid main-chain block hashes causes ~3,000 direct RocksDB reads (headers + uncles + extensions) followed by an `mmr.gen_proof(1000_positions)` call that reads O(1,000 × log₂ N) additional MMR nodes from RocksDB. On a 1 M-block chain that is roughly 20,000 extra reads — ~23,000 RocksDB reads total per single P2P message. Because there is no rate guard, any unprivileged peer can repeat this indefinitely, and a handful of concurrent peers can saturate the node's I/O and CPU.

---

### Finding Description

**Entry point — `LightClientProtocol::received`** [1](#0-0) 

The handler parses the message and immediately calls `try_process`. There is no rate-limiter field on the struct and no rate check before dispatch: [2](#0-1) 

Compare with `Relayer`, which carries a `rate_limiter` and checks it before every non-PoW message: [3](#0-2) 

And `HolePunching`, which also rate-limits per `(session_id, message_type)`: [4](#0-3) 

`LightClientProtocol` has **zero** such guard (confirmed: `grep rate_limiter util/light-client-protocol-server/**` → no matches).

**Step 1 — `GetBlocksProofProcess::execute` (lines 33–114)**

The only size check is `> GET_BLOCKS_PROOF_LIMIT` (1,000), so exactly 1,000 hashes pass: [5](#0-4) 

For every hash that passes `is_main_chain`, three synchronous RocksDB reads are issued: [6](#0-5) 

1,000 hashes × 3 reads = **3,000 RocksDB reads**.

**Step 2 — `reply_proof` (lib.rs lines 181–237)**

`mmr.get_root()` reads O(log N) MMR nodes, then `mmr.gen_proof(items_positions)` with 1,000 positions reads O(1,000 × log₂ N) nodes: [7](#0-6) 

For N = 1,000,000 blocks: log₂(2N−1) ≈ 21 → ~21,000 additional RocksDB reads per call.

**Total per message: ~24,000 RocksDB reads, triggered by one ~32 KB P2P frame.**

---

### Impact Explanation

- **I/O amplification ratio**: ~750× (32 KB message → ~24,000 RocksDB point-reads).
- **CPU**: MMR proof generation is CPU-bound (hashing at each tree level).
- **No cooldown**: the attacker can pipeline messages back-to-back; the server processes each one fully before the next.
- **Small attacker fleet**: even 5–10 concurrent peers sending at maximum rate can saturate a typical node's RocksDB I/O budget, causing block-processing latency to spike and the node to fall behind the chain tip.
- **Scope match**: sustained CPU/IO saturation of the light-client server; the full-node sync path shares the same RocksDB instance, so degradation is not isolated to the light-client subsystem.

---

### Likelihood Explanation

- Requires only a valid P2P connection to a node that has the light-client protocol enabled.
- No PoW, no key, no privileged role.
- The 1,000 block hashes can be pre-computed once from any public chain explorer and reused indefinitely.
- The `last_hash` just needs to be the current tip, which is broadcast by the node itself via `SendLastState`.

---

### Recommendation

1. **Add a per-peer rate limiter to `LightClientProtocol`**, mirroring the pattern in `Relayer` and `HolePunching` (e.g., `governor::RateLimiter` keyed by `(PeerIndex, message_item_id)`).
2. **Reduce `GET_BLOCKS_PROOF_LIMIT`** or introduce a cost-based limit that accounts for chain height (e.g., cap the number of MMR proof positions, not just the number of hashes).
3. Consider processing `GetBlocksProof` requests in a bounded worker pool so that a flood of requests cannot monopolise the async runtime.

---

### Proof of Concept

```
# Pre-conditions:
#   - Server has N = 1,000,000 blocks on main chain.
#   - Attacker knows tip_hash (received from SendLastState broadcast).
#   - Attacker pre-computes 1,000 distinct main-chain block hashes
#     (e.g., every 1,000th block: 0, 1000, 2000, ..., 999000).

loop:
    msg = GetBlocksProof {
        last_hash:    tip_hash,
        block_hashes: [hash_0, hash_1000, ..., hash_999000]  # 1000 entries
    }
    send(msg)   # ~32 KB on the wire
    # Server performs:
    #   3,000 RocksDB reads (get_block_header + get_block_uncles + get_block_extension)
    #   + mmr.get_root()        → O(log 1M) ≈ 21 reads
    #   + mmr.gen_proof(1000)   → O(1000 × 21) ≈ 21,000 reads
    # Total: ~24,021 RocksDB reads per iteration, no rate limit applied.
```

With 10 concurrent peers each sending at 10 msg/s, the server sustains ~2.4 million RocksDB reads/second from light-client traffic alone, crowding out normal block-sync I/O.

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

**File:** util/light-client-protocol-server/src/lib.rs (L199-211)
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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
        }
```

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L81-95)
```rust
        for block_hash in found {
            let header = snapshot
                .get_block_header(&block_hash)
                .expect("header should be in store");
            positions.push(leaf_index_to_pos(header.number()));
            block_headers.push(header.data());

            let uncles = snapshot
                .get_block_uncles(&block_hash)
                .expect("block uncles must be stored");
            let extension = snapshot.get_block_extension(&block_hash);

            uncles_hash.push(uncles.data().calc_uncles_hash());
            extensions.push(packed::BytesOpt::new_builder().set(extension).build());
        }
```
