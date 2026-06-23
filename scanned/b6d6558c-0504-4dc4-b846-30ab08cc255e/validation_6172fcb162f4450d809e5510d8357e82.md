### Title
Rate Limiter Counts Messages Not Hashes, Allowing ~983K Tx-Pool Lookups/Second Per Peer — (`sync/src/relayer/get_transactions_process.rs`, `sync/src/relayer/mod.rs`)

---

### Summary

The relay rate limiter is keyed by `(PeerIndex, message_item_id)` and consumes exactly **one token per message**, regardless of how many transaction hashes that message contains. Because `GetRelayTransactions` messages are permitted to carry up to `MAX_RELAY_TXS_NUM_PER_BATCH` (32 767) hashes, and the rate cap is 30 messages/second, a single unprivileged peer can force **30 × 32 767 ≈ 983 K** `fetch_txs_with_cycles` hash-lookups per second against the tx-pool controller.

---

### Finding Description

**Rate limiter setup** — `Relayer::new` creates a `governor::RateLimiter` with a quota of 30 tokens/second, keyed by `(PeerIndex, u32)`: [1](#0-0) 

**Rate check in `try_process`** — one token is consumed per message, with no awareness of payload size: [2](#0-1) 

**Batch size constant** — `MAX_RELAY_TXS_NUM_PER_BATCH` is 32 767: [3](#0-2) 

**Validation in `execute`** — the check is `> MAX_RELAY_TXS_NUM_PER_BATCH` (strictly greater), so exactly 32 767 hashes pass: [4](#0-3) 

**Tx-pool call** — all hashes are forwarded in a single `fetch_txs_with_cycles` call: [5](#0-4) 

The duplicate-hash guard (line 59) only prevents repeating the same hash within one message; it does not reduce the per-second lookup budget across messages. [6](#0-5) 

---

### Impact Explanation

The tx-pool controller is a serialized async actor. Flooding it with ~983 K hash lookups/second from a single peer can saturate its message queue, delaying or stalling legitimate transaction admission, mining template generation, and proposal processing for all other peers. This is a targeted, sustained DoS on the tx-pool subsystem — not a consensus failure or fund loss, consistent with the Low (501–2000) scope.

---

### Likelihood Explanation

The path requires only a standard P2P connection (no privilege, no PoW, no key). The attacker constructs `GetRelayTransactions` messages with 32 767 distinct (but non-existent) hashes and sends 30/second. The rate limiter passes every message. No additional preconditions are needed.

---

### Recommendation

Replace the per-message token consumption with a per-hash token consumption. Instead of `check_key(&(peer, message.item_id()))` consuming 1 token, consume `message_len` tokens (or use a separate hash-count quota). Alternatively, enforce a much lower per-peer hash-per-second budget (e.g., 1 000 hashes/second) independently of the message count.

---

### Proof of Concept

```
for _ in 0..forever:
    hashes = [random_byte32() for _ in range(32767)]   # all unique, all absent from pool
    msg = GetRelayTransactions(tx_hashes=hashes)
    send(msg)          # 30 times per second — rate limiter passes each one
    sleep(1/30)
```

Expected: tx-pool controller receives ~983 K `fetch_txs_with_cycles` lookup requests per second from this single peer, measurably delaying responses to legitimate peers.

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

**File:** sync/src/relayer/get_transactions_process.rs (L59-60)
```rust
            if message_len != tx_hashes_set.len() {
                return StatusCode::RequestDuplicate.with_context("Request duplicate transaction");
```

**File:** sync/src/relayer/get_transactions_process.rs (L63-63)
```rust
            let fetch_txs_with_cycles = tx_pool.fetch_txs_with_cycles(tx_hashes_set).await;
```
