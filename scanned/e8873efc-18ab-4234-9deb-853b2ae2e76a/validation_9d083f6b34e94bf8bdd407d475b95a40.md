### Title
Missing Implementation of `getheaders_received()` Allows Unbounded Per-Peer `GetHeaders` Flooding — (`File: sync/src/types/mod.rs`)

---

### Summary

The `Peers::getheaders_received()` function in `sync/src/types/mod.rs` is called every time a valid incoming `GetHeaders` P2P message is processed, but its body is entirely empty (`// TODO:`). This is the exact structural analog to the reported Solidity bug: a protective/tracking hook is wired into the critical code path but never implemented, leaving the node with no per-peer rate-limiting or state tracking for incoming `GetHeaders` requests. Any unprivileged peer can flood the node with repeated `GetHeaders` messages, each triggering up to 2,101 synchronous database reads and a spawned async send task, with no mechanism to detect or throttle the abuse.

---

### Finding Description

**Root cause — the no-op stub:**

`Peers::getheaders_received()` is defined as:

```rust
pub fn getheaders_received(&self, _peer: PeerIndex) {
    // TODO:
}
``` [1](#0-0) 

It is called unconditionally inside `GetHeadersProcess::execute()` immediately before the expensive response path:

```rust
self.synchronizer.peers().getheaders_received(self.peer);
let headers: Vec<core::HeaderView> =
    active_chain.get_locator_response(block_number, &hash_stop);
``` [2](#0-1) 

**What the function was supposed to do:**

Two constants defined in the same file signal that a rate-limiting mechanism was planned but never completed:

```rust
const GET_HEADERS_CACHE_SIZE: usize = 10000;
// TODO: Need discussed
const GET_HEADERS_TIMEOUT: Duration = Duration::from_secs(15);
``` [3](#0-2) 

An equivalent outgoing-request guard already exists: `send_getheaders_to_peer()` uses a `pending_get_headers` LRU cache keyed on `(PeerIndex, Byte32)` to suppress duplicate outgoing requests within `GET_HEADERS_TIMEOUT`. No such guard exists for *incoming* requests. [4](#0-3) 

**The expensive work triggered per message:**

Each valid `GetHeaders` message that passes the `MAX_LOCATOR_SIZE` (101) check causes:

1. `locate_latest_common_block()` — iterates up to 101 locator hashes, each requiring a DB block-status lookup.
2. `get_locator_response()` — iterates from the common block up to `MAX_HEADERS_LEN = 2,000` blocks, calling `get_block_hash()` and `get_block_header()` for each. [5](#0-4) 

3. A `tokio::spawn` async task to serialize and send up to 2,000 headers back to the peer. [6](#0-5) 

`MAX_HEADERS_LEN` is 2,000: [7](#0-6) 

**The only existing guard is insufficient:**

The only input check before the expensive path is a locator-size bound:

```rust
if locator_size > MAX_LOCATOR_SIZE {
    return StatusCode::ProtocolMessageIsMalformed...
}
``` [8](#0-7) 

This rejects malformed messages but does nothing to limit the *rate* at which a single well-formed peer can send `GetHeaders` messages.

---

### Impact Explanation

A single malicious peer can send a continuous stream of syntactically valid `GetHeaders` messages (each with ≤101 locator hashes pointing to real chain blocks). For every message the node:

- Performs up to 2,101 RocksDB reads (101 for locator resolution + 2,000 for header collection).
- Allocates and serializes a `SendHeaders` response of up to 2,000 headers.
- Spawns a tokio task to transmit the response.

Because `getheaders_received()` is a no-op, none of this work is gated, counted, or throttled per peer. The result is sustained CPU and disk I/O exhaustion on the victim node, degrading or halting block synchronization for legitimate peers. The node cannot distinguish a flooding peer from a normal one because the tracking hook was never implemented.

**Impact category:** Resource exhaustion / denial of service against the sync subsystem, reachable by any unprivileged P2P peer.

---

### Likelihood Explanation

- **Attacker capability required:** None beyond establishing a standard P2P connection. No keys, no stake, no privileged role.
- **Protocol exposure:** The `GetHeaders` message is a core sync protocol message accepted from all connected peers.
- **Effort:** Trivially automated — a script that opens a connection and loops sending `GetHeaders` with a fixed valid locator is sufficient.
- **Detection:** The node has no counter, log throttle, or ban trigger tied to incoming `GetHeaders` rate; the empty `getheaders_received()` is the exact hook where such detection would live.

Likelihood: **High** — the attack surface is always open, requires no special knowledge, and the missing implementation is confirmed by the `// TODO:` comment and the unused `GET_HEADERS_CACHE_SIZE`/`GET_HEADERS_TIMEOUT` constants.

---

### Recommendation

Implement `getheaders_received()` to track per-peer incoming `GetHeaders` request timestamps using the already-defined `GET_HEADERS_CACHE_SIZE` and `GET_HEADERS_TIMEOUT` constants, mirroring the existing `pending_get_headers` guard used for outgoing requests in `send_getheaders_to_peer()`. If a peer has sent a `GetHeaders` within the timeout window for the same locator tip, the request should be ignored or the peer penalized via `ban_peer`. [1](#0-0) 

---

### Proof of Concept

1. Connect to a CKB mainnet/testnet node as a standard sync peer.
2. Obtain any valid block hash from the chain tip (e.g., via RPC `get_tip_header`).
3. In a tight loop, send `SyncMessage::GetHeaders` with `block_locator_hashes = [<tip_hash>]` and `hash_stop = Byte32::zero()`.
4. Each message passes the `locator_size > MAX_LOCATOR_SIZE` check (size = 1).
5. The node calls `locate_latest_common_block()`, then `getheaders_received()` (no-op), then `get_locator_response()` fetching up to 2,000 headers from RocksDB, then spawns a send task.
6. Observe sustained RocksDB read I/O and CPU usage on the victim node with no ban or throttle applied to the sender.

The call chain is:

```
P2P message received
  → SyncProtocol::received()
    → GetHeadersProcess::execute()          [get_headers_process.rs:36]
      → locate_latest_common_block()        [up to 101 DB reads]
      → getheaders_received()               [NO-OP — types/mod.rs:897]
      → get_locator_response()              [up to 2,000 DB reads]
      → tokio::spawn(send SendHeaders)
``` [9](#0-8)

### Citations

**File:** sync/src/types/mod.rs (L48-50)
```rust
const GET_HEADERS_CACHE_SIZE: usize = 10000;
// TODO: Need discussed
const GET_HEADERS_TIMEOUT: Duration = Duration::from_secs(15);
```

**File:** sync/src/types/mod.rs (L897-899)
```rust
    pub fn getheaders_received(&self, _peer: PeerIndex) {
        // TODO:
    }
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

**File:** sync/src/synchronizer/get_headers_process.rs (L36-98)
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

        if let Some(block_number) =
            active_chain.locate_latest_common_block(&hash_stop, &block_locator_hashes[..])
        {
            debug!(
                "headers latest_common={} tip={} begin",
                block_number,
                active_chain.tip_header().number(),
            );

            self.synchronizer.peers().getheaders_received(self.peer);
            let headers: Vec<core::HeaderView> =
                active_chain.get_locator_response(block_number, &hash_stop);
            // response headers

            debug!("headers len={}", headers.len());

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
        } else {
            return StatusCode::GetHeadersMissCommonAncestors
                .with_context(format!("{block_locator_hashes:#x?}"));
        }
        Status::ok()
```

**File:** util/constant/src/sync.rs (L7-8)
```rust
/// Default max get header response length, if it is greater than this value, the message will be ignored
pub const MAX_HEADERS_LEN: usize = 2_000;
```
