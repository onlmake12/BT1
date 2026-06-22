### Title
Unbounded MMR Proof Generation Per Peer Request with No Rate Limiting — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/constant.rs`)

---

### Summary

An unprivileged remote peer can repeatedly send `GetBlocksProof` (or `GetTransactionsProof`) messages containing exactly 1000 valid main-chain hashes — the protocol-defined maximum — causing the server to execute `mmr.gen_proof(1000_positions)` on every request. This is O(N × log H) RocksDB reads per request (N=1000, H=chain height). `LightClientProtocol` has no per-peer rate limiter, unlike `Relayer` and `HolePunching` which both carry an explicit `RateLimiter`. A valid max-cost request returns `Status::ok()` (HTTP-200 analogue), so no ban is ever applied, and the attacker can sustain the load indefinitely.

---

### Finding Description

**Entry point — `GetBlocksProofProcess::execute`**

The handler enforces only a count ceiling:

```rust
if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
}
```

`GET_BLOCKS_PROOF_LIMIT = 1000`, so exactly 1000 hashes is accepted without error. [1](#0-0) [2](#0-1) 

After the count check, every hash that passes `is_main_chain` is added to `positions`, and the full vector is forwarded to `reply_proof`: [3](#0-2) 

**Cost sink — `reply_proof`**

```rust
match mmr.gen_proof(items_positions) {
    Ok(proof) => proof.proof_items().to_owned(),
    ...
}
```

`gen_proof` on an MMR of size H with N leaf positions requires reading O(N × log₂ H) internal nodes from RocksDB. On mainnet (H ≈ 10 M, log₂ H ≈ 23), one max-cost request issues ≈ 23 000 DB reads. [4](#0-3) 

On success the function returns `Status::ok()` (code 200). [5](#0-4) 

**No ban for valid requests**

`should_ban()` only fires for status codes 400–499:

```rust
if !(400..500).contains(&code) { None } else { Some(BAD_MESSAGE_BAN_TIME) }
```

A 1000-hash request that passes all checks returns code 200, so `should_ban()` returns `None` and the peer is never banned. [6](#0-5) 

**No rate limiter in `LightClientProtocol`**

The struct holds only `shared: Shared` — no `RateLimiter` field, no per-peer request counter, no token bucket. [7](#0-6) 

Compare with `Relayer`, which carries `rate_limiter: RateLimiter<(PeerIndex, u32)>` and checks it at the top of `try_process` (30 req/s hard cap per peer per message type): [8](#0-7) 

`HolePunching` similarly has both `rate_limiter` and `forward_rate_limiter`. [9](#0-8) 

---

### Impact Explanation

Each max-cost request forces ~23 000 synchronous RocksDB reads inside the async handler. With no rate limit, a single peer can saturate the node's storage I/O, degrading block processing, sync, and RPC responsiveness for all other users. The attack requires no special privilege — any peer that can open a light-client connection qualifies.

---

### Likelihood Explanation

The 1000 valid main-chain hashes needed are trivially obtained from any block explorer or by syncing a few blocks. The attack is repeatable at network speed with a simple loop. No PoW, no key material, no Sybil network is required.

---

### Recommendation

Add a per-peer, per-message-type rate limiter to `LightClientProtocol` mirroring the pattern already used in `Relayer`:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Apply the check at the top of `try_process` before dispatching to any handler. A quota of ~5–10 proof requests per second per peer is sufficient for legitimate light clients while making the attack economically infeasible.

Additionally, consider lowering `GET_BLOCKS_PROOF_LIMIT` and `GET_TRANSACTIONS_PROOF_LIMIT` to a smaller value (e.g., 100–200) to reduce the per-request cost ceiling. [10](#0-9) 

---

### Proof of Concept

```rust
// Attacker loop (pseudocode)
let hashes: Vec<Byte32> = fetch_1000_mainchain_block_hashes(); // public data
let tip_hash = fetch_tip_hash();
loop {
    let msg = GetBlocksProof::new_builder()
        .last_hash(tip_hash.clone())
        .block_hashes(hashes.clone())
        .build();
    send_light_client_message(peer, msg); // no sleep needed
    // server performs ~23 000 DB reads, returns OK, no ban
}
```

To benchmark: run `mmr.gen_proof` with 1 vs 1000 positions on a chain of height 10⁶ and assert that latency scales linearly with position count. The same benchmark with height 10⁷ will show the additional log-factor growth.

### Citations

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

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

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L72-95)
```rust
        let (found, missing): (Vec<_>, Vec<_>) = block_hashes
            .into_iter()
            .partition(|block_hash| snapshot.is_main_chain(block_hash));

        let mut positions = Vec::with_capacity(found.len());
        let mut block_headers = Vec::with_capacity(found.len());
        let mut uncles_hash = Vec::with_capacity(found.len());
        let mut extensions = Vec::with_capacity(found.len());

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

**File:** util/light-client-protocol-server/src/lib.rs (L235-237)
```rust
        nc.reply(peer, &message).await;
        Status::ok()
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

**File:** network/src/protocols/hole_punching/mod.rs (L44-47)
```rust
    pending_delivered: HashMap<PeerId, PendingDeliveredInfo>,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
    forward_rate_limiter: RateLimiter<(PeerId, PeerId, u32)>,
}
```
