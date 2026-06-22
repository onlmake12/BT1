Looking at the full execution path for this claim:

### Title
Unbounded `GetBlocks` Flood Without Rate Limiting or Peer Ban — (`sync/src/synchronizer/get_blocks_process.rs`)

### Summary

`GetBlocksProcess::execute` unconditionally returns `Status::ok()` for any well-formed `GetBlocks` message whose hashes are unknown or unverified. `post_sync_process` only bans peers on 4xx status codes. The `Synchronizer` has no rate limiter for `GetBlocks` (unlike `Relayer`, which does). An unprivileged remote peer can therefore flood the victim with `GetBlocks` messages at wire speed, triggering up to 32 `contains_block_status` DB lookups per message, with zero risk of being banned.

---

### Finding Description

**Entrypoint:** Any connected peer sends `SyncMessage::GetBlocks` over the P2P sync protocol.

**Execution path:**

1. `Synchronizer::received` → `Synchronizer::process` → `Synchronizer::try_process` [1](#0-0) 

2. `GetBlocksProcess::execute` is called. The only early-exit that returns a 4xx (ban-triggering) code is:
   - `block_hashes.len() > MAX_HEADERS_LEN` → `ProtocolMessageIsMalformed` (400)
   - A hash equals genesis → `RequestGenesis` (417)
   - A duplicate hash → `RequestDuplicate` (418) [2](#0-1) 

3. For every hash that is **not** `BLOCK_VALID`, the code silently `continue`s and the loop ends with `Status::ok()` (code 100): [3](#0-2) [4](#0-3) 

4. `post_sync_process` only calls `nc.ban_peer` when `status.should_ban()` returns `Some`, which requires the status code to be in `400..500`: [5](#0-4) [6](#0-5) 

5. The `Synchronizer` struct has **no rate limiter** for any message type. A grep for `rate_limiter` in `sync/src/synchronizer/mod.rs` returns zero matches. By contrast, `Relayer::try_process` has an explicit per-peer, per-message-type rate limiter (30 req/sec): [7](#0-6) 

**Per-message cost:** Each `GetBlocks` message iterates up to `INIT_BLOCKS_IN_TRANSIT_PER_PEER = 32` hashes, calling `active_chain.contains_block_status` (a DB lookup) for each: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

An attacker who connects as a peer (inbound connection) can send `GetBlocks` messages at wire speed, each containing up to 2000 hashes (only 32 are processed). Each message causes 32 synchronous `contains_block_status` DB lookups inside `tokio::task::block_in_place`, blocking a tokio worker thread for the duration. This produces sustained, unbounded CPU and database I/O load on the victim node, degrading sync performance and potentially causing the node to fall behind the chain tip.

---

### Likelihood Explanation

The attack requires only a standard P2P connection — no special privileges, no PoW, no key material. The `StatusCode::TooManyRequests = 110` variant exists in the codebase but is never applied in the `Synchronizer` path. The asymmetry with `Relayer` (which has rate limiting) confirms this is an oversight, not a design choice. The attack is trivially reproducible with a minimal P2P client.

---

### Recommendation

Add a per-peer rate limiter to `Synchronizer::try_process` for `GetBlocks` messages, mirroring the pattern already used in `Relayer`: [10](#0-9) 

Return `StatusCode::TooManyRequests` (1xx, no ban, but drops the message) when the rate is exceeded. Alternatively, consider returning a 4xx code to ban persistent abusers, or disconnect the peer after repeated violations.

---

### Proof of Concept

**Invariant (provable from code):** For any `GetBlocks` message where:
- `0 < block_hashes.len() <= MAX_HEADERS_LEN` (2000)
- No hash equals genesis
- No duplicate hashes

`GetBlocksProcess::execute` always returns `Status::ok()` regardless of whether the hashes correspond to real, unknown, or fabricated blocks. [11](#0-10) 

**Attack loop (pseudocode):**
```
loop {
    hashes = [random_32_byte_hash() for _ in range(32)]  // all unknown
    send GetBlocks(hashes) to victim via SyncProtocol
    // victim does 32 DB lookups, returns ok(), never bans
}
```

The existing unit test `get_blocks_process` confirms the only ban-triggering cases are genesis and duplicate hashes — unknown hashes are never tested for ban behavior: [12](#0-11)

### Citations

**File:** sync/src/synchronizer/mod.rs (L407-411)
```rust
            packed::SyncMessageUnionReader::GetBlocks(reader) => {
                tokio::task::block_in_place(|| {
                    GetBlocksProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/synchronizer/get_blocks_process.rs (L33-97)
```rust
    pub fn execute(self) -> Status {
        let block_hashes = self.message.block_hashes();
        // use MAX_HEADERS_LEN as limit, we may increase the value of INIT_BLOCKS_IN_TRANSIT_PER_PEER in the future
        if block_hashes.len() > MAX_HEADERS_LEN {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "BlockHashes count({}) > MAX_HEADERS_LEN({})",
                block_hashes.len(),
                MAX_HEADERS_LEN,
            ));
        }
        let active_chain = self.synchronizer.shared.active_chain();

        let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);

        let mut dedup = HashSet::new();
        for block_hash in iter {
            debug!("get_blocks {} from peer {:?}", block_hash, self.peer);
            let block_hash = block_hash.to_entity();

            if block_hash == self.synchronizer.shared().consensus().genesis_hash() {
                return StatusCode::RequestGenesis.with_context("Request genesis block");
            }

            if !dedup.insert(block_hash.clone()) {
                return StatusCode::RequestDuplicate.with_context("Request duplicate block");
            }

            if !active_chain.contains_block_status(&block_hash, BlockStatus::BLOCK_VALID) {
                debug!(
                    "Ignoring get_block {} request from peer={} as it is not verified.",
                    block_hash, self.peer
                );
                continue;
            }

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
            } else {
                // TODO response not found
                // TODO add timeout check in synchronizer

                // We expect that `block_hashes` is sorted descending by height.
                // So if we cannot find the current one from local, we cannot find
                // the next either.
                debug!("Stopping getblocks, since {} is not found", block_hash);
                break;
            }
        }

        Status::ok()
    }
```

**File:** sync/src/types/mod.rs (L2008-2013)
```rust
    if let Some(ban_time) = status.should_ban() {
        error!(
            "Receive {} from {}. Ban {:?} for {}",
            item_name, peer, ban_time, status
        );
        nc.ban_peer(peer, ban_time, status.to_string());
```

**File:** sync/src/status.rs (L165-179)
```rust
    pub fn should_ban(&self) -> Option<Duration> {
        if !(400..500).contains(&(self.code as u16)) {
            return None;
        }
        if let Some(context) = &self.context {
            // TODO: it might be worthwhile to formalize all error texts
            // that won't be banned.
            if context.contains(ARGV_TOO_LONG_TEXT) {
                return None;
            }
        }
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** sync/src/relayer/mod.rs (L89-123)
```rust
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

**File:** util/constant/src/sync.rs (L14-14)
```rust
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
```

**File:** sync/src/tests/synchronizer/functions.rs (L1237-1271)
```rust
fn get_blocks_process() {
    let consensus = Consensus::default();
    let (chain, shared, synchronizer) = start_chain(Some(consensus));

    let num = 2;
    for i in 1..num {
        insert_block(chain.chain_controller(), &shared, u128::from(i), i);
    }

    let genesis_hash = shared.consensus().genesis_hash();
    let message_with_genesis = packed::GetBlocks::new_builder()
        .block_hashes(vec![genesis_hash])
        .build();

    let nc = Arc::new(mock_network_context(1)) as Arc<dyn CKBProtocolContext + Sync + 'static>;
    let peer: PeerIndex = 1.into();
    let process = GetBlocksProcess::new(message_with_genesis.as_reader(), &synchronizer, peer, &nc);
    assert_eq!(
        process.execute(),
        StatusCode::RequestGenesis.with_context("Request genesis block")
    );

    let hash = shared.snapshot().get_block_hash(1).unwrap();
    let message_with_dup = packed::GetBlocks::new_builder()
        .block_hashes(vec![hash.clone(), hash])
        .build();

    let nc = Arc::new(mock_network_context(1)) as Arc<dyn CKBProtocolContext + Sync + 'static>;
    let peer: PeerIndex = 1.into();
    let process = GetBlocksProcess::new(message_with_dup.as_reader(), &synchronizer, peer, &nc);
    assert_eq!(
        process.execute(),
        StatusCode::RequestDuplicate.with_context("Request duplicate block")
    );
}
```
