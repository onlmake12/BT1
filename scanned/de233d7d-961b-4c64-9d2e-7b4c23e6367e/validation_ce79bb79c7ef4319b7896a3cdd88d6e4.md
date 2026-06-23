### Title
Single Rate Limit Quota Applied Uniformly to All Relay Message Types Enables Resource Exhaustion via Low-Frequency Message Flooding — (File: sync/src/relayer/mod.rs)

---

### Summary

The `Relayer` protocol applies a single rate-limit quota of 30 req/s uniformly to every relay message type, despite those types having vastly different natural rates. Low-frequency, computationally expensive messages such as `BlockTransactions` and `BlockProposal` are permitted at 300× their natural rate, allowing any connected peer to exhaust CPU on the victim node.

---

### Finding Description

In `Relayer::new()`, a single `governor::Quota` of 30 req/s is created and shared across all relay message types:

```rust
// setup a rate limiter keyed by peer and message type that lets through 30 requests per second
// current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
let rate_limiter = RateLimiter::hashmap(quota);
``` [1](#0-0) 

The quota is enforced via `check_key(&(peer, message.item_id()))`, meaning each `(peer, message_type)` pair gets its own 30 req/s bucket — but every bucket uses the **same** quota regardless of the message type's natural rate. [2](#0-1) 

The relay protocol handles seven rate-limited message types (CompactBlock is explicitly exempted because it is PoW-verified):

| Message Type | Natural Rate | Allowed Rate | Ratio |
|---|---|---|---|
| `RelayTransactionHashes` | ~10 req/s | 30 req/s | 3× |
| `GetRelayTransactions` | ~10 req/s | 30 req/s | 3× |
| `RelayTransactions` | ~10 req/s | 30 req/s | 3× |
| `GetBlockTransactions` | ~0.1 req/s (1/block) | 30 req/s | **300×** |
| `BlockTransactions` | ~0.1 req/s (1/block) | 30 req/s | **300×** |
| `GetBlockProposal` | ~0.1 req/s | 30 req/s | **300×** |
| `BlockProposal` | ~0.1 req/s | 30 req/s | **300×** |

The comment itself acknowledges the mismatch: the quota is calibrated for the highest-frequency types (`ASK_FOR_TXS_TOKEN` / `TX_PROPOSAL_TOKEN` at 10 req/s), with a 3× buffer. No adjustment is made for message types whose natural rate is two orders of magnitude lower. [3](#0-2) 

This is structurally identical to the oracle staleness-period bug: one global parameter is chosen to fit the fastest-updating entity, making it far too permissive for slow-updating entities.

---

### Impact Explanation

`BlockTransactions` triggers compact-block reconstruction: the node must merge the short-ID prefill list with locally known transactions, re-hash the result, and verify the assembled block. At 30 req/s (300× the natural rate), a single malicious peer can saturate the relay worker with reconstruction work, starving legitimate compact-block processing and degrading block propagation across the network. Similarly, `BlockProposal` messages cause proposal-set lookups and deduplication work at 300× the expected rate. The net effect is CPU exhaustion and relay-protocol DoS reachable by any unprivileged connected peer.

---

### Likelihood Explanation

Any peer that completes the CKB handshake can send relay messages. No keys, no hashpower, and no Sybil capability are required. The attacker simply opens a connection and sends the target message type at the maximum permitted rate in a tight loop. The attack is trivially scriptable.

---

### Recommendation

**Short term**: Replace the single shared quota with per-message-type quotas that reflect each type's natural rate:

```rust
// Example per-type quotas
let high_freq_quota  = Quota::per_second(NonZeroU32::new(30).unwrap()); // tx hashes, get-txs
let low_freq_quota   = Quota::per_second(NonZeroU32::new(2).unwrap());  // block-txs, proposals
```

**Long term**: Add automated tests that assert each message type's rate limit is within a reasonable multiple of its observed natural rate on mainnet, analogous to testing oracle heartbeat alignment.

---

### Proof of Concept

1. Connect to a CKB mainnet/testnet node and complete the identify/handshake protocol.
2. In a tight loop, send `BlockTransactions` (or `BlockProposal`) relay messages at 30 req/s — the maximum the rate limiter permits.
3. Each message passes the rate-limit gate at `check_key(&(peer, message.item_id()))` and enters `BlockTransactionsProcess::execute()`, triggering compact-block reconstruction work.
4. Observe elevated CPU on the victim node and degraded compact-block relay latency for legitimate peers.

The root cause is the hardcoded single quota in `Relayer::new()`: [4](#0-3) 

applied uniformly through the single rate-limit check: [5](#0-4)

### Citations

**File:** sync/src/relayer/mod.rs (L88-98)
```rust
    pub fn new(chain: ChainController, shared: Arc<SyncShared>) -> Self {
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
