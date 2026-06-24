Audit Report

## Title
Unbounded DB Read Cost Per Incoming `GetHeaders` Message with No Per-Peer Rate Limit — (`sync/src/synchronizer/get_headers_process.rs`)

## Summary
An unprivileged remote peer can send a `GetHeaders` message with exactly `MAX_LOCATOR_SIZE` (101) locator entries — 100 unknown hashes followed by the genesis hash — forcing the receiving node to perform up to ~4,101 DB reads per message. Because there is no per-peer incoming rate limit on `GetHeaders` and the peer is never banned for this pattern, a single connection can sustain this at TCP wire speed, causing sustained RocksDB I/O and CPU exhaustion.

## Finding Description

**Step 1 — Size check passes.**
The guard at `get_headers_process.rs:47` is `locator_size > MAX_LOCATOR_SIZE`, so exactly 101 entries is accepted without rejection. [1](#0-0) 

**Step 2 — IBD guard is bypassed.**
The node is assumed to be out of IBD (post-sync), so execution falls through to `locate_latest_common_block`. [2](#0-1) 

**Step 3 — `locate_latest_common_block` iterates all 101 entries.**
The function first validates that the last entry is the genesis hash. Then the lazy `.find()` iterator calls `get_block_number` for each entry until it finds a known one. With 100 unknown hashes, all 101 `get_block_number` DB reads are made before the genesis hash matches at index 100. The condition `latest_common == Some(0)` then causes an immediate return of `Some(0)` — the peer is **not** rejected. [3](#0-2) 

**Step 4 — `get_locator_response` performs up to 4,000 additional DB reads.**
Starting from block 1 (genesis+1), it iterates up to `MAX_HEADERS_LEN = 2,000` block numbers, calling `get_block_hash` (snapshot read) and `get_block_header` (store read) for each — up to 4,000 reads total. [4](#0-3) 

**Step 5 — No rate limit on incoming `GetHeaders`.**
The `pending_get_headers` LRU cache is used exclusively inside `send_getheaders_to_peer` — the node's own **outgoing** request deduplication. It is never consulted when processing an incoming `GetHeaders` message. No `RateLimiter` or equivalent guard exists in `GetHeadersProcess::execute` or the sync dispatcher. [5](#0-4) [6](#0-5) 

**Step 6 — Peer is never banned.**
`GetHeadersMissCommonAncestors` (the only ban path in this handler) is only triggered when `locate_latest_common_block` returns `None`. With a valid genesis-terminated locator, it returns `Some(0)`, so `Status::ok()` is returned and the peer is never penalized. [7](#0-6) [8](#0-7) 

## Impact Explanation
Each crafted `GetHeaders` message causes ~4,101 DB reads (101 `get_block_number` + up to 2,000 `get_block_hash` + up to 2,000 `get_block_header`). With no incoming rate limit and no ban triggered, a single peer can sustain this at TCP wire speed, causing sustained RocksDB I/O saturation and degraded node responsiveness. This matches the **Low (501–2000)** impact band: "Any other important performance improvements for CKB." It is a resource exhaustion DoS, not a consensus or fund-safety issue. [9](#0-8) [10](#0-9) 

## Likelihood Explanation
The attack requires only a standard P2P connection — no privileges, no PoW, no keys. The crafted message is trivially constructable (100 random 32-byte hashes + genesis hash). The node's connection limit is the only practical throttle, but a single connection is sufficient to sustain the load. The exploit is deterministic and repeatable.

## Recommendation
1. Add a per-peer incoming `GetHeaders` rate limit (e.g., max N messages per second per peer) in `GetHeadersProcess::execute` or the sync protocol dispatcher, using the existing `TooManyRequests` status code infrastructure.
2. Consider banning or disconnecting peers that repeatedly send all-unknown locators: if `locate_latest_common_block` returns `Some(0)` more than K times from the same peer within a window, apply `SYNC_USELESS_BAN_TIME`.
3. Alternatively, short-circuit `get_locator_response` when `block_number == 0` and the chain height is large, or cap the response to a smaller window in this case.

## Proof of Concept
```
1. Connect to a non-IBD CKB node (chain height > 2000).
2. Craft GetHeaders {
       block_locator_hashes: [rand_hash_1, ..., rand_hash_100, genesis_hash],
       hash_stop: 0x00..00
   }.
3. Send in a tight loop (no sleep).
4. Observe: node never bans the peer; each message triggers ~4101 DB reads;
   node RocksDB I/O and CPU spike proportionally to send rate.
5. Instrument: assert total DB reads == N * 4101 for N messages sent.
```

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L47-51)
```rust
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L53-66)
```rust
        if active_chain.is_initial_block_download() {
            info!(
                "Ignoring getheaders from peer={} because the node is in initial block download stage.",
                self.peer
            );
            self.send_in_ibd();
            let shared = self.synchronizer.shared();
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
            return Status::ignored();
        }
```

**File:** sync/src/synchronizer/get_headers_process.rs (L94-98)
```rust
        } else {
            return StatusCode::GetHeadersMissCommonAncestors
                .with_context(format!("{block_locator_hashes:#x?}"));
        }
        Status::ok()
```

**File:** sync/src/types/mod.rs (L1866-1881)
```rust
        let locator_hash = locator.last().expect("empty checked");
        if locator_hash != &self.sync_shared.consensus().genesis_hash() {
            return None;
        }

        // iterator are lazy
        let (index, latest_common) = locator
            .iter()
            .enumerate()
            .map(|(index, hash)| (index, self.snapshot.get_block_number(hash)))
            .find(|(_index, number)| number.is_some())
            .expect("locator last checked");

        if index == 0 || latest_common == Some(0) {
            return latest_common;
        }
```

**File:** sync/src/types/mod.rs (L1914-1920)
```rust
        std::iter::successors(Some(start_number), |number| number.checked_add(1))
            .take_while(|number| *number <= tip_number)
            .take(MAX_HEADERS_LEN)
            .filter_map(|block_number| self.snapshot.get_block_hash(block_number))
            .take_while(|block_hash| block_hash != hash_stop)
            .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash))
            .collect()
```

**File:** sync/src/types/mod.rs (L1929-1951)
```rust
        if let Some(last_time) = self
            .state()
            .pending_get_headers
            .write()
            .get(&(peer, block_number_and_hash.hash()))
        {
            if Instant::now() < *last_time + GET_HEADERS_TIMEOUT {
                debug!(
                    "Last get_headers request to peer {} is less than {:?}; Ignore it.",
                    peer, GET_HEADERS_TIMEOUT,
                );
                return;
            } else {
                debug!(
                    "Can not get headers from {} in {:?}, retry",
                    peer, GET_HEADERS_TIMEOUT,
                );
            }
        }
        self.state()
            .pending_get_headers
            .write()
            .put((peer, block_number_and_hash.hash()), Instant::now());
```

**File:** sync/src/synchronizer/mod.rs (L396-401)
```rust
        match message {
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/status.rs (L176-179)
```rust
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** util/constant/src/sync.rs (L7-8)
```rust
/// Default max get header response length, if it is greater than this value, the message will be ignored
pub const MAX_HEADERS_LEN: usize = 2_000;
```

**File:** util/constant/src/sync.rs (L44-45)
```rust
/// The maximum number of entries in a locator
pub const MAX_LOCATOR_SIZE: usize = 101;
```
