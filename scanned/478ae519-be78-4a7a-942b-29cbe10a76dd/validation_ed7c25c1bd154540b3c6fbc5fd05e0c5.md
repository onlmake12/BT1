Based on my code analysis, here is my determination:

---

### Title
Unbounded DB Read Amplification via Genesis-Anchored GetHeaders — (`sync/src/synchronizer/get_headers_process.rs`, `sync/src/types/mod.rs`)

### Summary

An unprivileged remote peer can repeatedly send `GetHeaders` messages with a locator whose only known ancestor is the genesis block, forcing the node to perform up to `2 × MAX_HEADERS_LEN` (4,000) RocksDB reads per message with no application-level rate limiting on incoming requests.

### Finding Description

**Entry point:** `GetHeadersProcess::execute()` in `get_headers_process.rs`.

The handler has two guards:

1. **Locator size check** — rejects if `locator_size > MAX_LOCATOR_SIZE` (101). A two-element locator `[unknown_hash, genesis_hash]` passes trivially. [1](#0-0) 

2. **IBD check** — ignores the message during IBD. The question's precondition is non-IBD, so this guard is bypassed. [2](#0-1) 

After both guards pass, `get_locator_response(block_number=0, ...)` is called:

```rust
std::iter::successors(Some(start_number), |number| number.checked_add(1))
    .take_while(|number| *number <= tip_number)
    .take(MAX_HEADERS_LEN)                                          // up to 2000 iterations
    .filter_map(|block_number| self.snapshot.get_block_hash(block_number))   // DB read #1
    .take_while(|block_hash| block_hash != hash_stop)
    .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash)) // DB read #2
    .collect()
``` [3](#0-2) 

With `block_number = 0` and `tip > 2000`, this iterates all 2,000 slots, issuing:
- `snapshot.get_block_hash(n)` — one RocksDB snapshot read per block number
- `store().get_block_header(&hash)` — one RocksDB read per block hash

Total: **up to 4,000 RocksDB reads per single GetHeaders message**.

**No incoming rate limit exists.** The `pending_get_headers` / `GET_HEADERS_TIMEOUT` mechanism is exclusively for *outgoing* `send_getheaders_to_peer` calls: [4](#0-3) 

`getheaders_received` (called at line 77) records statistics only; it does not throttle processing. [5](#0-4) 

`MAX_HEADERS_LEN = 2_000` and `MAX_LOCATOR_SIZE = 101` are confirmed constants: [6](#0-5) [7](#0-6) 

### Impact Explanation

Each crafted `GetHeaders` message causes up to 4,000 RocksDB reads on the victim node. An attacker maintaining a persistent peer connection can send these messages at the maximum rate the network allows, causing sustained IO amplification proportional to `2 × MAX_HEADERS_LEN` per message. This degrades node responsiveness for legitimate peers and sync operations. Impact is scoped as **Low (501–2000)** — measurable performance degradation, not a full outage.

### Likelihood Explanation

The attack requires only a standard P2P peer connection. No PoW, no keys, no special privileges. The locator `[random_unknown_hash, genesis_hash]` is trivially constructed. The genesis hash is public. The condition `locate_latest_common_block` returning `Some(0)` is guaranteed when the first hash is unknown and the second is genesis.

### Recommendation

1. Add a per-peer rate limit on incoming `GetHeaders` messages (e.g., token bucket, or a cooldown analogous to `pending_get_headers` for outbound).
2. Consider banning or throttling peers that repeatedly anchor to genesis (block 0) when the node's tip is far ahead — this is a strong signal of adversarial behavior.
3. Alternatively, cap `get_locator_response` to a smaller window when the common ancestor is very far behind the tip.

### Proof of Concept

```
1. Connect to a non-IBD CKB node (tip > 2000) as a standard peer.
2. Send GetHeaders with block_locator_hashes = [random_32_byte_hash, genesis_hash],
   hash_stop = 0x000...000.
3. Observe: node calls get_locator_response(0, ...) → 2000 × get_block_hash + 2000 × get_block_header.
4. Repeat in a tight loop.
5. Profile RocksDB read counters (via metrics or perf) — assert ~4000 reads per message vs.
   ~2 reads when common ancestor is near tip.
```

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L46-51)
```rust
        let locator_size = block_locator_hashes.len();
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

**File:** sync/src/synchronizer/get_headers_process.rs (L77-79)
```rust
            self.synchronizer.peers().getheaders_received(self.peer);
            let headers: Vec<core::HeaderView> =
                active_chain.get_locator_response(block_number, &hash_stop);
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

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```

**File:** util/constant/src/sync.rs (L45-45)
```rust
pub const MAX_LOCATOR_SIZE: usize = 101;
```
