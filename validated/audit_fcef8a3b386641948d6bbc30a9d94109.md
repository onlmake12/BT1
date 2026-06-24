Audit Report

## Title
Rate Limiter Counts Messages Not Hashes, Enabling ~983K Tx-Pool Lookups/Second Per Peer — (`sync/src/relayer/get_transactions_process.rs`, `sync/src/relayer/mod.rs`)

## Summary
The relay rate limiter consumes exactly one token per `GetRelayTransactions` message regardless of how many transaction hashes the message carries. Since `MAX_RELAY_TXS_NUM_PER_BATCH` is 32 767 and the per-peer cap is 30 messages/second, a single unprivileged peer can drive approximately 983 K `fetch_txs_with_cycles` hash-lookups per second into the serialized tx-pool controller, degrading or stalling legitimate tx-pool operations for all other peers on the same node.

## Finding Description
**Rate limiter setup:** `Relayer::new` creates a `governor::RateLimiter` keyed by `(PeerIndex, u32)` with a quota of 30 tokens/second. [1](#0-0) 

**Per-message token consumption:** `try_process` calls `check_key(&(peer, message.item_id()))`, consuming exactly one token per message with no awareness of payload size. [2](#0-1) 

**Batch size constant:** `MAX_RELAY_TXS_NUM_PER_BATCH` is 32 767. [3](#0-2) 

**Strictly-greater check:** The validation in `GetTransactionsProcess::execute` rejects only messages with `message_len > MAX_RELAY_TXS_NUM_PER_BATCH`, so a message carrying exactly 32 767 hashes passes. [4](#0-3) 

**Duplicate-hash guard scope:** The deduplication check at line 59 only catches repeated hashes *within* a single message; it provides no cross-message protection. [5](#0-4) 

**Tx-pool call:** All hashes from a passing message are forwarded in one `fetch_txs_with_cycles` call to the serialized tx-pool controller actor. [6](#0-5) 

Exploit flow: attacker establishes a standard P2P connection → sends 30 `GetRelayTransactions` messages/second, each containing 32 767 distinct (non-existent) hashes → rate limiter passes every message → tx-pool controller receives ~983 K lookup requests/second → its serialized message queue saturates → legitimate operations (tx admission, mining template generation, proposal processing) are delayed or stalled.

## Impact Explanation
The tx-pool controller is a single-threaded async actor; its channel is the serialization point for all tx-pool operations on the node. Sustained flooding at ~983 K lookups/second from one peer can fill that channel, causing measurable latency or complete stalling of mining template generation and transaction propagation. This matches the allowed CKB bounty impact: **Low (501–2000) — Any other important performance improvements for CKB.**

## Likelihood Explanation
No privilege, proof-of-work, or key material is required. Any peer that completes the standard P2P handshake can immediately begin the attack. The construction is trivial: generate 32 767 random 32-byte values per message, send 30 messages/second. The rate limiter passes every message by design. The attack is repeatable and sustained indefinitely.

## Recommendation
Replace per-message token consumption with per-hash token consumption. Before calling `fetch_txs_with_cycles`, consume `message_len` tokens from the rate limiter (using `check_key_n` or an equivalent n-token API) rather than a fixed 1 token. Alternatively, maintain a separate per-peer hash-per-second budget (e.g., 1 000 hashes/second) enforced independently of the message count. Either approach closes the amplification gap between the rate-limit unit (messages) and the actual work unit (hash lookups).

## Proof of Concept
```python
import time, random

def random_byte32():
    return bytes(random.getrandbits(8) for _ in range(32))

while True:
    for _ in range(30):
        hashes = [random_byte32() for _ in range(32767)]  # all unique, all absent from pool
        msg = build_get_relay_transactions(tx_hashes=hashes)
        p2p_send(msg)          # rate limiter passes: 1 token consumed
        time.sleep(1/30)
    # Expected: tx-pool controller receives ~983K fetch_txs_with_cycles lookups/second
    # from this single peer, measurably increasing latency for all other tx-pool callers.
```

### Citations

**File:** sync/src/relayer/mod.rs (L60-60)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L116-123)
```rust
        if should_check_rate
            && self
                .rate_limiter
                .check_key(&(peer, message.item_id()))
                .is_err()
        {
            return StatusCode::TooManyRequests.with_context(message.item_name());
        }
```

**File:** sync/src/relayer/get_transactions_process.rs (L35-39)
```rust
            if message_len > MAX_RELAY_TXS_NUM_PER_BATCH {
                return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                    "TxHashes count({message_len}) > MAX_RELAY_TXS_NUM_PER_BATCH({MAX_RELAY_TXS_NUM_PER_BATCH})",
                ));
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L59-61)
```rust
            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
            }
```

**File:** sync/src/relayer/get_transactions_process.rs (L63-63)
```rust
            let fetch_txs_with_cycles = tx_pool.fetch_txs_with_cycles(tx_hashes_set).await;
```
