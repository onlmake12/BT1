Audit Report

## Title
Unbounded `GetHeaders` Message Processing Without Rate Limiting Enables CPU/IO Exhaustion DoS — (File: `sync/src/synchronizer/get_headers_process.rs`)

## Summary
The `Synchronizer` protocol handler processes `GetHeaders` messages from any connected peer with no per-peer rate limit. Each message with a valid genesis-hash locator triggers up to 2,000 sequential RocksDB reads inside `block_in_place`, blocking a tokio worker thread for the full duration. Because the response path returns `Status::ok()` (no ban), a single inbound peer can flood the node indefinitely, saturating RocksDB I/O and the tokio thread pool and rendering the node unresponsive.

## Finding Description

**No rate limiter on the Synchronizer.** The `Relayer` installs a per-peer, per-message-type governor rate limiter capped at 30 req/s: [1](#0-0) 

The `Synchronizer::try_process` has no equivalent guard and dispatches `GetHeaders` directly via `block_in_place`: [2](#0-1) 

**Exploit path with valid genesis-hash locator.** `locate_latest_common_block` requires the last locator entry to equal the consensus genesis hash; if it does not, `None` is returned and the peer is banned via `GetHeadersMissCommonAncestors` (10-minute ban): [3](#0-2) [4](#0-3) 

However, when the attacker supplies 101 copies of the real genesis hash (publicly known), the function finds block number 0 at index 0 and returns `Some(0)` immediately — no ban is triggered and `Status::ok()` is returned: [5](#0-4) 

`get_locator_response` then performs up to `MAX_HEADERS_LEN = 2,000` sequential RocksDB reads (one `get_block_hash` + one `get_block_header` per block): [6](#0-5) 

Constants confirm the per-message work budget: [7](#0-6) [8](#0-7) 

The only existing guard — the locator size check — bounds input size, not message rate: [9](#0-8) 

Because `block_in_place` is used, each message occupies a tokio worker thread for the entire DB read sequence. Multiple concurrent flooding peers can exhaust the thread pool, stalling all async tasks.

## Impact Explanation

Sustained flooding forces the victim node to execute ~2,000 RocksDB reads per message with no rate cap and no ban path for valid-looking messages. This saturates RocksDB read throughput and blocks tokio worker threads, causing the node to stop processing blocks, relaying transactions, and responding to RPC calls. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10,001–15,000 points). It also qualifies as **High — bad designs which could cause CKB network congestion with few costs**, since a single attacker with a cheap TCP connection can degrade or halt a fully-synced node.

## Likelihood Explanation

No authentication, mining power, or privileged role is required. Any peer that can establish a TCP connection can send `GetHeaders`. The attack is trivially scriptable: the genesis hash is public, the message format is documented, and the flood loop requires no state. The contrast with the Relayer's explicit 30 req/s cap confirms the Synchronizer's exposure is unintentional. The attack is repeatable and sustainable indefinitely because the node never bans the peer for this message pattern.

## Recommendation

Apply a per-peer rate limiter to `Synchronizer::try_process` for `GetHeaders`, mirroring the `governor`-based pattern already used in the `Relayer`: [10](#0-9) 

A limit of 10–30 `GetHeaders` messages per second per peer is sufficient for legitimate sync. Additionally, consider caching `get_locator_response` results for a short TTL keyed by `(start_block_number, hash_stop)` to amortize repeated identical queries.

## Proof of Concept

1. Connect to a fully-synced CKB mainnet node as an inbound peer using the Sync protocol.
2. Obtain the mainnet genesis block hash (publicly available).
3. Construct a molecule-encoded `SyncMessage::GetHeaders` with `block_locator_hashes` set to 101 copies of the genesis hash and `hash_stop` set to `Byte32::zero()`.
4. Send this message in a tight loop at maximum TCP throughput from one or more peers.
5. **Expected result**: Each message causes `locate_latest_common_block` to return `Some(0)` (1 DB lookup, no ban), then `get_locator_response` to execute up to 2,000 RocksDB reads inside `block_in_place`, blocking a tokio worker thread. Sustained flooding saturates RocksDB I/O and the tokio thread pool, causing the node to stop processing blocks, relaying transactions, and responding to RPC calls. The node never bans the attacker peer because `Status::ok()` is returned for every message.

### Citations

**File:** sync/src/relayer/mod.rs (L63-67)
```rust
type RateLimiter<T> = governor::RateLimiter<
    T,
    governor::state::keyed::HashMapStateStore<T>,
    governor::clock::DefaultClock,
>;
```

**File:** sync/src/relayer/mod.rs (L89-98)
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
```

**File:** sync/src/synchronizer/mod.rs (L397-401)
```rust
            packed::SyncMessageUnionReader::GetHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    GetHeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```

**File:** sync/src/types/mod.rs (L1866-1869)
```rust
        let locator_hash = locator.last().expect("empty checked");
        if locator_hash != &self.sync_shared.consensus().genesis_hash() {
            return None;
        }
```

**File:** sync/src/types/mod.rs (L1879-1881)
```rust
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

**File:** sync/src/status.rs (L176-179)
```rust
        match self.code {
            StatusCode::GetHeadersMissCommonAncestors => Some(SYNC_USELESS_BAN_TIME),
            _ => Some(BAD_MESSAGE_BAN_TIME),
        }
```

**File:** util/constant/src/sync.rs (L8-8)
```rust
pub const MAX_HEADERS_LEN: usize = 2_000;
```

**File:** util/constant/src/sync.rs (L45-45)
```rust
pub const MAX_LOCATOR_SIZE: usize = 101;
```

**File:** sync/src/synchronizer/get_headers_process.rs (L46-51)
```rust
        let locator_size = block_locator_hashes.len();
        if locator_size > MAX_LOCATOR_SIZE {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "Locator count({locator_size}) > MAX_LOCATOR_SIZE({MAX_LOCATOR_SIZE})"
            ));
        }
```
