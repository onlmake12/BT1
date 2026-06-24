Audit Report

## Title
Unsolicited `SendBlock` Full Deserialization Before Inflight Check Enables CPU Exhaustion — (`sync/src/synchronizer/block_process.rs`)

## Summary

`BlockProcess::execute()` unconditionally deserializes the full block payload (byte copy + Blake2b hash computation) at line 35 before checking at line 43 whether the block was ever solicited. The `Synchronizer` struct has no rate limiter for `SendBlock` messages, unlike the `Relayer` which enforces 30 req/s per peer. Any peer can flood the node with valid-molecule `SendBlock` messages for unsolicited blocks, consuming CPU proportional to block size per message with no throttle.

## Finding Description

In `BlockProcess::execute()`, the full deserialization occurs unconditionally before any inflight check:

```rust
// line 35 — full copy + hash computation
let block = Arc::new(self.message.block().to_entity().into_view());
// ...
// line 43 — inflight check happens AFTER all CPU work
if shared.new_block_received(&block) {
``` [1](#0-0) 

`to_entity()` copies all raw bytes into owned types; `into_view()` computes Blake2b hashes for the block header and every transaction. Only after this work does `new_block_received` call `remove_by_block`. If the block hash was never inserted into `inflight_states`, `remove_by_block` returns `false` (the `inflight_states.remove(&block)` returns `None`, so `.is_some()` is `false`), and `new_block_received` returns `false` immediately. [2](#0-1) [3](#0-2) 

The block is silently dropped, but all CPU work has already been done. The `Synchronizer` struct has no `rate_limiter` field:

```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
``` [4](#0-3) 

The `try_process` dispatch for `SendBlock` applies no rate check before invoking `BlockProcess`: [5](#0-4) 

This is in direct contrast to the `Relayer`, which has an explicit `rate_limiter: RateLimiter<(PeerIndex, u32)>` field and enforces 30 req/s per peer per message type before any processing: [6](#0-5) [7](#0-6) 

## Impact Explanation

An attacker with a single P2P connection can send a continuous stream of valid-molecule `SendBlock` messages for blocks not in the local inflight set. Each message triggers: (1) `check_data()` molecule structural validation, (2) `to_entity()` full byte copy of the block payload, (3) `into_view()` Blake2b hash computation over header and all transactions, (4) `Arc::new` allocation, (5) `remove_by_block` lookup returning false. For a block near the consensus size limit (~597 KB), this is significant CPU work per message. With no rate limiting, the attacker can saturate CPU resources, degrading sync throughput for all honest peers. This matches the **High** impact: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

The attack requires only a valid P2P handshake — no special privileges, no valid PoW. Only valid molecule encoding is needed, which is trivially constructable from the public molecule schema. A single connection is sufficient. The attack is continuously repeatable with minimal bandwidth relative to the CPU cost imposed on the victim (amplification proportional to block size).

## Recommendation

1. **Move the inflight check before deserialization**: Use the zero-copy `SendBlockReader` to extract the block hash from the raw molecule bytes and check `inflight_states` before calling `to_entity().into_view()`. Molecule readers are zero-copy and do not compute hashes.
2. **Add a rate limiter to `Synchronizer`** for `SendBlock` messages, mirroring the existing `Relayer` rate limiter (30 req/s per peer per message type) at the `try_process` dispatch level.

## Proof of Concept

1. Connect to a CKB node as a peer via the Sync protocol (complete the P2P handshake).
2. Construct a valid-molecule `SendBlock` message containing a block with a hash not present in the node's inflight set. The block does not need valid PoW — only valid molecule structure.
3. Send the message in a tight loop from a single connection.
4. Observe CPU usage on the victim node: each message triggers full deserialization (`to_entity().into_view()`) with no throttle, while the block is silently dropped after the inflight check fails.
5. Measure CPU time per message vs. bytes sent to confirm amplification proportional to block size with no rate limiting applied. [1](#0-0) [5](#0-4)

### Citations

**File:** sync/src/synchronizer/block_process.rs (L34-43)
```rust
    pub fn execute(self) -> crate::Status {
        let block = Arc::new(self.message.block().to_entity().into_view());
        debug!(
            "BlockProcess received block {} {}",
            block.number(),
            block.hash(),
        );
        let shared = self.synchronizer.shared();

        if shared.new_block_received(&block) {
```

**File:** sync/src/types/mod.rs (L785-819)
```rust
    pub fn remove_by_block(&mut self, block: BlockNumberAndHash) -> bool {
        let should_punish = self.download_schedulers.len() > self.protect_num;
        let download_schedulers = &mut self.download_schedulers;
        let trace = &mut self.trace_number;
        let time_analyzer = &mut self.time_analyzer;
        let adjustment = self.adjustment;
        self.inflight_states
            .remove(&block)
            .map(|state| {
                let elapsed = unix_time_as_millis().saturating_sub(state.timestamp);
                if let Some(set) = download_schedulers.get_mut(&state.peer) {
                    set.hashes.remove(&block);
                    if adjustment {
                        match time_analyzer.push_time(elapsed) {
                            TimeQuantile::MinToFast => set.increase(2),
                            TimeQuantile::FastToNormal => set.increase(1),
                            TimeQuantile::NormalToUpper => {
                                if should_punish {
                                    set.decrease(1)
                                }
                            }
                            TimeQuantile::UpperToMax => {
                                if should_punish {
                                    set.decrease(2)
                                }
                            }
                        }
                    }
                    if !trace.is_empty() {
                        trace.remove(&block);
                    }
                };
            })
            .is_some()
    }
```

**File:** sync/src/types/mod.rs (L1199-1207)
```rust
    /// Return true when the block is that we have requested and received first time.
    pub fn new_block_received(&self, block: &core::BlockView) -> bool {
        if !self
            .state()
            .write_inflight_blocks()
            .remove_by_block((block.number(), block.hash()).into())
        {
            return false;
        }
```

**File:** sync/src/synchronizer/mod.rs (L357-362)
```rust
pub struct Synchronizer {
    pub(crate) chain: ChainController,
    /// Sync shared state
    pub shared: Arc<SyncShared>,
    fetch_channel: Option<channel::Sender<FetchCMD>>,
}
```

**File:** sync/src/synchronizer/mod.rs (L412-418)
```rust
            packed::SyncMessageUnionReader::SendBlock(reader) => {
                if reader.check_data() {
                    BlockProcess::new(reader, self, peer, nc).execute()
                } else {
                    StatusCode::ProtocolMessageIsMalformed.with_context("SendBlock is invalid")
                }
            }
```

**File:** sync/src/relayer/mod.rs (L81-82)
```rust
    rate_limiter: RateLimiter<(PeerIndex, u32)>,
}
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
