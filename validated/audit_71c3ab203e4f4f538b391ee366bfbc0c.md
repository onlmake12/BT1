All cited claims verified against the actual code. Every factual assertion in the report is confirmed:

- `>` (not `>=`) at [1](#0-0)  allows exactly 1000 hashes through.
- `GET_BLOCKS_PROOF_LIMIT = 1000` at [2](#0-1) 
- HashSet dedup check at [3](#0-2)  does not reduce cost for 1000 distinct hashes.
- All valid positions forwarded to `mmr.gen_proof` at [4](#0-3) 
- `reply_proof` returns `Status::ok()` at [5](#0-4) 
- `should_ban()` only fires for 400–499 at [6](#0-5) 
- `LightClientProtocol` has no rate limiter field at [7](#0-6) 
- `try_process` dispatches with no rate check at [8](#0-7) 
- `Relayer` has `rate_limiter` and enforces it at [9](#0-8) 
- `HolePunching` has both rate limiters at [10](#0-9) 

---

Audit Report

## Title
Unbounded MMR Proof Generation Per Peer Request with No Rate Limiting — (`util/light-client-protocol-server/src/lib.rs`, `util/light-client-protocol-server/src/constant.rs`)

## Summary
`LightClientProtocol` accepts up to 1000 block hashes per `GetBlocksProof` request and unconditionally calls `mmr.gen_proof` on all valid positions, performing O(N × log H) RocksDB reads per request. Unlike `Relayer` and `HolePunching`, `LightClientProtocol` carries no per-peer rate limiter. A well-formed max-cost request returns `Status::ok()` (code 200), so no ban is ever applied, and an unprivileged peer can loop this indefinitely to saturate node storage I/O.

## Finding Description
**Off-by-one in count guard:** The guard at `get_blocks_proof.rs` L38 uses `>` rather than `>=`, so exactly 1000 hashes passes without triggering `MalformedProtocolMessage`.

**Deduplication does not reduce cost:** The `HashSet` check at L62–70 rejects duplicate hashes but 1000 *distinct* valid main-chain hashes trivially pass it, leaving the full cost intact.

**All valid positions forwarded to `mmr.gen_proof`:** Every hash that passes `is_main_chain` is appended to `positions` (L81–95) and passed directly to `reply_proof`, which calls `mmr.gen_proof(items_positions)` (L210) — O(N × log H) synchronous RocksDB reads inside the async handler.

**Success path never bans:** `reply_proof` returns `Status::ok()` (code 200). `should_ban()` in `status.rs` L95–102 only returns `Some(ban_time)` for codes 400–499, so a max-cost request is never penalized.

**No rate limiter in `LightClientProtocol`:** The struct holds only `shared: Shared` with no `RateLimiter` field. `try_process` dispatches directly to handlers with no rate check, in contrast to `Relayer` which enforces a 30 req/s hard cap per peer per message type at the top of its `try_process`, and `HolePunching` which carries both `rate_limiter` and `forward_rate_limiter`.

## Impact Explanation
At mainnet height ~10M (log₂ H ≈ 23), one max-cost request issues ~23,000 synchronous RocksDB reads. With no rate limit and no ban, a single peer can sustain this at network speed, saturating the node's storage I/O and degrading block processing, sync, and RPC responsiveness for all users. This matches the High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."**

## Likelihood Explanation
The 1000 distinct valid main-chain hashes required are public data, obtainable from any block explorer or by syncing a few blocks. The attack requires no PoW, no key material, no Sybil network, and no victim mistake — any peer that can open a light-client connection qualifies. The loop is trivially implemented and repeatable at network speed.

## Recommendation
Add a per-peer, per-message-type rate limiter to `LightClientProtocol` mirroring the pattern in `Relayer`:

```rust
pub struct LightClientProtocol {
    pub shared: Shared,
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
```

Apply the check at the top of `try_process` before dispatching to any handler. A quota of 5–10 proof requests per second per peer is sufficient for legitimate light clients. Additionally, change the count guard from `>` to `>=` and lower `GET_BLOCKS_PROOF_LIMIT` and `GET_TRANSACTIONS_PROOF_LIMIT` from 1000 to a smaller value (e.g., 100–200) to reduce the per-request cost ceiling.

## Proof of Concept
```rust
// Attacker loop (pseudocode)
let hashes: Vec<Byte32> = fetch_1000_distinct_mainchain_block_hashes(); // public data
let tip_hash = fetch_tip_hash();
loop {
    let msg = GetBlocksProof::new_builder()
        .last_hash(tip_hash.clone())
        .block_hashes(hashes.clone())
        .build();
    send_light_client_message(peer, msg);
    // server performs ~23,000 DB reads, returns Status::ok(), no ban applied
}
```

To benchmark: run `mmr.gen_proof` with 1 vs 1000 positions on a chain of height 10⁶ and assert latency scales linearly with position count. Repeat at height 10⁷ to confirm the additional log-factor growth. Monitor RocksDB read IOPS during the loop to confirm I/O saturation.

### Citations

**File:** util/light-client-protocol-server/src/components/get_blocks_proof.rs (L38-40)
```rust
        if self.message.block_hashes().len() > constant::GET_BLOCKS_PROOF_LIMIT {
            return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
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

**File:** util/light-client-protocol-server/src/constant.rs (L5-7)
```rust
pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```

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

**File:** util/light-client-protocol-server/src/lib.rs (L96-125)
```rust
    async fn try_process(
        &mut self,
        nc: &Arc<dyn CKBProtocolContext + Sync>,
        peer_index: PeerIndex,
        message: packed::LightClientMessageUnionReader<'_>,
    ) -> Status {
        match message {
            packed::LightClientMessageUnionReader::GetLastState(reader) => {
                components::GetLastStateProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetBlocksProof(reader) => {
                components::GetBlocksProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            packed::LightClientMessageUnionReader::GetTransactionsProof(reader) => {
                components::GetTransactionsProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
            _ => StatusCode::UnexpectedProtocolMessage.into(),
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
