### Title
Unauthenticated Amplification DoS via Genesis-Hash GetHeaders — (`sync/src/synchronizer/get_headers_process.rs`)

### Summary

An unprivileged remote peer can send a `GetHeaders` message with a single-entry locator containing the genesis hash. The node, when out of IBD, will unconditionally collect up to `MAX_HEADERS_LEN = 2000` headers from its database, serialize them (~384 KB), and transmit them back — with no per-peer rate limit, no cost to the attacker, and no ban triggered. This creates a high-amplification request/response asymmetry exploitable as a bandwidth and CPU DoS.

---

### Finding Description

**Entrypoint:** Any peer can send a `SyncMessage::GetHeaders` P2P message. It is dispatched to `GetHeadersProcess::execute()`. [1](#0-0) 

**Guard 1 — locator size:** Only rejects if `locator_size > MAX_LOCATOR_SIZE (101)`. A single-entry locator (size = 1) passes. [2](#0-1) 

**Guard 2 — IBD check:** Ignored when the node is out of IBD (the stated precondition). [3](#0-2) 

**`locate_latest_common_block` with `[genesis_hash]`:** The function first verifies `locator.last() == genesis_hash`. With a single-entry locator `[genesis_hash]`, `index = 0` and `latest_common = Some(0)`. The early-return condition `index == 0 || latest_common == Some(0)` fires, returning `Some(0)` — a valid common ancestor. [4](#0-3) 

**`get_locator_response(0, &zero_hash)`:** Starting from block 1, iterates up to `tip_number`, capped at `MAX_HEADERS_LEN = 2000`. On a 100,000-block chain this returns exactly 2000 headers. [5](#0-4) 

**No rate limiting on incoming GetHeaders:** The `pending_get_headers` cache with `GET_HEADERS_TIMEOUT` is only applied to *outgoing* `send_getheaders_to_peer` calls — it does not throttle *incoming* requests from peers. [6](#0-5) 

**No ban triggered:** `GetHeadersMissCommonAncestors` (which carries `SYNC_USELESS_BAN_TIME`) is only returned when `locate_latest_common_block` returns `None`. Since genesis is always a valid common ancestor, this ban path is never reached for the genesis-hash attack. [7](#0-6) 

**`RequestGenesis` (status 417) is never checked** in `get_headers_process.rs` — it exists in `status.rs` but is unused in this handler. [8](#0-7) 

The response is serialized and dispatched via a spawned async task with no backpressure: [9](#0-8) 

---

### Impact Explanation

Each small `GetHeaders` message (~70 bytes) causes the node to:
1. Perform up to 2000 DB reads (`get_block_hash` + `get_block_header`)
2. Serialize ~2000 × ~192 bytes ≈ **~384 KB** of headers
3. Spawn an async send task

An attacker with a single connection can send these messages in a tight loop. With multiple connections (each from a different IP or Sybil peer), the amplification multiplies. This exhausts outbound bandwidth, DB I/O, and async task queue capacity, causing network congestion and degraded sync performance for legitimate peers.

---

### Likelihood Explanation

The attack requires only a standard P2P connection and knowledge of the genesis hash (publicly known). No PoW, no keys, no privileged role. The genesis hash is hardcoded in the chain spec and trivially obtained. The attack is locally testable and repeatable.

---

### Recommendation

1. **Add per-peer rate limiting** on incoming `GetHeaders` messages (e.g., token bucket or sliding window counter per `PeerIndex`).
2. **Detect and ban genesis-only locators**: if the locator resolves to block 0 and the peer is not in a legitimate sync state, return `RequestGenesis` (already defined as status 417) and apply a ban.
3. **Cap response work**: consider refusing to serve the maximum 2000 headers to peers that have not demonstrated legitimate sync progress.

---

### Proof of Concept

```
1. Connect to a CKB node that is out of IBD with a long chain.
2. Repeatedly send:
     SyncMessage::GetHeaders {
         block_locator_hashes: [genesis_hash],
         hash_stop: 0x000...000
     }
3. Observe: each message triggers a ~384 KB SendHeaders response.
4. Assert: no ban, no rate limit, no cost to attacker.
5. Measure: CPU and bandwidth usage on the victim node scales linearly
   with message rate; no self-protection mechanism activates.
```

### Citations

**File:** sync/src/synchronizer/get_headers_process.rs (L36-51)
```rust
    pub fn execute(self) -> Status {
        let active_chain = self.synchronizer.shared.active_chain();

        let block_locator_hashes = self
            .message
            .block_locator_hashes()
            .iter()
            .map(|x| x.to_entity())
            .collect::<Vec<Byte32>>();
        let hash_stop = self.message.hash_stop().to_entity();
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

**File:** sync/src/synchronizer/get_headers_process.rs (L84-93)
```rust
            let content = packed::SendHeaders::new_builder()
                .headers(headers.into_iter().map(|x| x.data()).collect::<Vec<_>>())
                .build();
            let message = packed::SyncMessage::new_builder().set(content).build();
            let nc = Arc::clone(self.nc);
            self.synchronizer
                .shared()
                .shared()
                .async_handle()
                .spawn(async move { async_send_message_to(&nc, self.peer, &message).await });
```

**File:** sync/src/types/mod.rs (L1866-1880)
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
```

**File:** sync/src/types/mod.rs (L1905-1921)
```rust
    pub fn get_locator_response(
        &self,
        block_number: BlockNumber,
        hash_stop: &Byte32,
    ) -> Vec<core::HeaderView> {
        let tip_number = self.tip_header().number();
        let Some(start_number) = block_number.checked_add(1) else {
            return Vec::new();
        };
        std::iter::successors(Some(start_number), |number| number.checked_add(1))
            .take_while(|number| *number <= tip_number)
            .take(MAX_HEADERS_LEN)
            .filter_map(|block_number| self.snapshot.get_block_hash(block_number))
            .take_while(|block_hash| block_hash != hash_stop)
            .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash))
            .collect()
    }
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

**File:** sync/src/status.rs (L107-108)
```rust
    /// Request Genesis
    RequestGenesis = 417,
```

**File:** sync/src/status.rs (L176-179)
```rust
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```
