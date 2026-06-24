All seven cited code references have been verified against the actual repository. Every claim matches exactly.

**Citation verification:**

1. `Synchronizer` struct at [1](#0-0)  — confirmed, no `rate_limiter` field.

2. `try_process` dispatches `GetBlocks` with no preceding rate check at [2](#0-1)  — confirmed.

3. `Relayer` holds `rate_limiter: RateLimiter<(PeerIndex, u32)>` at [3](#0-2)  — confirmed.

4. `Relayer::try_process` gates every non-`CompactBlock` message at [4](#0-3)  — confirmed.

5. `MAX_HEADERS_LEN = 2_000` and `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` at [5](#0-4)  — confirmed.

6. `GetBlocksProcess::execute` bounds check and `.take(INIT_BLOCKS_IN_TRANSIT_PER_PEER)` at [6](#0-5)  — confirmed.

7. Per-hash async block send at [7](#0-6)  — confirmed.

---

Audit Report

## Title
Unbounded GetBlocks Amplification via Missing Rate Limiter in `Synchronizer::try_process` — (`sync/src/synchronizer/get_blocks_process.rs`)

## Summary
`Synchronizer::try_process` dispatches `GetBlocks` messages directly to `GetBlocksProcess::execute` with no rate limiting. Any unprivileged peer can send `GetBlocks` at wire speed, each message triggering up to 32 full serialized block responses. The `Relayer` already applies a 30 req/s per-peer rate limiter for equivalent message types; no such guard exists in the `Synchronizer` path.

## Finding Description
`Synchronizer` has no `rate_limiter` field — the struct contains only `chain`, `shared`, and `fetch_channel`. `try_process` matches `GetBlocks` and immediately calls `GetBlocksProcess::execute` with no preceding check. By contrast, `Relayer` initializes a `governor::RateLimiter<(PeerIndex, u32)>` at 30 req/s and gates every non-`CompactBlock` message before dispatch. `GetBlocksProcess::execute` accepts up to `MAX_HEADERS_LEN = 2000` hashes, processes up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32`, and for each hash resolving to a `BLOCK_VALID` block, spawns an async task that sends the full serialized block via `async_send_message_to`. There is no per-peer counter, token bucket, or any other throttle on this path.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** A single attacker connection can saturate a node's outbound bandwidth by sending ~1 KB `GetBlocks` requests in a tight loop, each eliciting up to 32 full block responses. This degrades the victim node's ability to propagate blocks and transactions to legitimate peers, contributing to network congestion. The amplification ratio (attacker inbound ~1 KB/req vs. victim outbound ~32 × block_size/req) makes the attack cheap to sustain and expensive to absorb.

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileges, no PoW, no keys. Valid block hashes are freely available from any block explorer or by syncing headers first. The attacker's cost is bounded by their own inbound bandwidth, which is far lower than the victim's outbound cost. The attack is repeatable and persistent.

## Recommendation
Add a per-peer rate limiter to `Synchronizer::try_process` for `GetBlocks` (and `GetHeaders`), mirroring the `governor::RateLimiter` pattern already present in `Relayer`. A limit of 10–30 req/s per peer per message type eliminates the amplification without impacting legitimate sync behavior.

## Proof of Concept
1. Connect to a victim CKB node as a normal peer.
2. Collect 32 valid block hashes (e.g., from a block explorer or by syncing headers).
3. In a tight loop, send `SyncMessage::GetBlocks` containing those 32 hashes.
4. Observe: victim sends 32 full blocks per request with no throttle; attacker inbound cost ~1 KB/req, victim outbound cost ~32 × block_size/req.
5. For differential confirmation: send equivalent `Relayer::GetBlockTransactions` messages — rate-limited to 30 req/s; `Synchronizer::GetBlocks` — unlimited. Measure outbound bandwidth ratio exceeding 100× attacker inbound bandwidth.

### Citations

**File:** sync/src/synchronizer/mod.rs (L357-362)
```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    /// Sync shared state
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
```

**File:** sync/src/synchronizer/mod.rs (L407-411)
```rust
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/relayer/mod.rs (L81-81)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
```

**File:** sync/src/relayer/mod.rs (L113-123)
```rust
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

**File:** util/constant/src/sync.rs (L8-14)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;

// The default number of download blocks that can be requested at one time
/* About Download Scheduler */

/// ckb2021 edition new limit
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L36-45)
```rust
        if block_hashes.len() > MAX_HEADERS_LEN {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "BlockHashes count({}) > MAX_HEADERS_LEN({})",
                block_hashes.len(),
                MAX_HEADERS_LEN,
            ));
        }
        let active_chain = self.synchronizer.shared.active_chain();

        let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L68-83)
```rust
            if let Some(block) = active_chain.get_block(&block_hash) {
                debug!(
                    "respond_block {} {} to peer {:?}",
                    block.number(),
                    block.hash(),
                    self.peer,
                );
                let content = packed::SendBlock::new_builder().block(block.data()).build();
                let message = packed::SyncMessage::new_builder().set(content).build();

                let nc = Arc::clone(self.nc);
                self.synchronizer
                    .shared()
                    .shared()
                    .async_handle()
                    .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
```
