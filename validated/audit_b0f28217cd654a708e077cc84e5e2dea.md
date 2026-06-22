### Title
Unbounded DB Lookup Loop in `GetBlockFilterCheckPointsProcess::execute` Allows Peer-Triggered Resource Exhaustion — (`sync/src/filter/get_block_filter_check_points_process.rs`)

### Summary

Any connected peer can send a `GetBlockFilterCheckPoints` message with `start_number=0` and force the responding node to execute up to 4,000 synchronous DB reads per request, with no rate limiting, no per-peer throttle, and no cap on request frequency. On a mature chain, multiple concurrent peers doing this can saturate the node's DB read capacity and degrade sync for all peers.

### Finding Description

`GetBlockFilterCheckPointsProcess::execute` loops up to `BATCH_SIZE = 2000` times, and in each iteration performs two DB lookups: [1](#0-0) [2](#0-1) 

Each iteration calls `get_block_hash(block_number)` followed by `get_block_filter_hash(&block_hash)` — 2 DB reads × 2000 iterations = **up to 4,000 DB reads per single peer request**. The loop only terminates early if a hash is missing; on a fully synced node with all filter data built, all 2000 iterations complete.

The `BlockFilter` protocol handler dispatches directly to `execute()` with no rate limiting, no per-peer request counter, and no cooldown: [3](#0-2) 

The only protection in the handler is banning for malformed messages (unparseable bytes): [4](#0-3) 

A well-formed `GetBlockFilterCheckPoints(start_number=0)` is never banned. There is no rate limiting anywhere in the filter protocol path — confirmed by the absence of any throttle in `sync/src/filter/`:



By contrast, the relayer protocol does implement rate limiting (`sync/src/relayer/mod.rs`), but this protection was never applied to the filter protocol.

### Impact Explanation

On a node with 4M+ blocks and all filter data built, each `GetBlockFilterCheckPoints(0)` request forces 4,000 RocksDB point-lookups. With 50 concurrent peers each sending this message in a tight loop, the node faces 200,000 DB reads/second from this path alone, competing with normal sync DB reads. This causes:
- Increased latency for block/header sync DB operations
- Potential queue buildup in the async filter handler
- Sync degradation visible to all connected peers

### Likelihood Explanation

The attack requires only a valid P2P connection to a node with the filter protocol enabled. The message is tiny (a single `BlockNumber` field), costs the attacker essentially nothing to send repeatedly, and is indistinguishable from a legitimate light client request. No PoW, no stake, no privileged role is required.

### Recommendation

1. Add a per-peer rate limit on `GetBlockFilterCheckPoints` messages (e.g., max 1 request per second per peer).
2. Alternatively, cap the effective `BATCH_SIZE` to a smaller value (e.g., 100–200) to reduce per-request cost.
3. Track per-peer request frequency in the `BlockFilter` handler and ban peers that exceed a threshold, consistent with how the relayer handles rate limiting.

### Proof of Concept

On a node with 4M+ blocks and filter data built:
1. Open 50 TCP connections to the node's filter protocol port.
2. From each connection, send `GetBlockFilterCheckPoints { start_number: 0 }` in a tight loop.
3. Monitor the node's RocksDB read IOPS (via `/proc` or `rocksdb.stats`) and measure response latency for normal `GetHeaders`/`GetBlocks` sync messages from a legitimate peer.
4. Expected: DB IOPS spike proportional to concurrent senders; sync message latency increases measurably. [5](#0-4)

### Citations

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L9-10)
```rust
const BATCH_SIZE: BlockNumber = 2000;
const CHECK_POINT_INTERVAL: BlockNumber = 2000;
```

**File:** sync/src/filter/get_block_filter_check_points_process.rs (L34-69)
```rust
    pub async fn execute(self) -> Status {
        let active_chain = self.filter.shared.active_chain();
        let start_number: BlockNumber = self.message.to_entity().start_number().into();
        let latest: BlockNumber = active_chain.get_latest_built_filter_block_number();

        let mut block_filter_hashes = Vec::new();

        if latest >= start_number {
            let mut block_number = start_number;
            for _ in 0..BATCH_SIZE {
                if let Some(block_filter_hash) = active_chain
                    .get_block_hash(block_number)
                    .and_then(|block_hash| active_chain.get_block_filter_hash(&block_hash))
                {
                    block_filter_hashes.push(block_filter_hash);
                } else {
                    break;
                }
                let Some(next_block_number) = block_number.checked_add(CHECK_POINT_INTERVAL) else {
                    break;
                };
                block_number = next_block_number;
            }
            let content = packed::BlockFilterCheckPoints::new_builder()
                .start_number(start_number)
                .block_filter_hashes(block_filter_hashes)
                .build();

            let message = packed::BlockFilterMessage::new_builder()
                .set(content)
                .build();
            async_send_message_to(&self.nc, self.peer, &message).await
        } else {
            Status::ignored()
        }
    }
```

**File:** sync/src/filter/mod.rs (L50-54)
```rust
            packed::BlockFilterMessageUnionReader::GetBlockFilterCheckPoints(msg) => {
                GetBlockFilterCheckPointsProcess::new(msg, self, nc, peer)
                    .execute()
                    .await
            }
```

**File:** sync/src/filter/mod.rs (L128-143)
```rust
        let msg = match packed::BlockFilterMessageReader::from_compatible_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                info_target!(
                    crate::LOG_TARGET_FILTER,
                    "Peer {} sends us a malformed message",
                    peer_index
                );
                nc.ban_peer(
                    peer_index,
                    BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };
```
