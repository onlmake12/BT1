Audit Report

## Title
Unbounded DB Read Amplification via Genesis-Anchored GetHeaders with No Incoming Rate Limit — (`sync/src/synchronizer/get_headers_process.rs`, `sync/src/types/mod.rs`)

## Summary

An unprivileged remote peer can send repeated `GetHeaders` messages with a two-element locator `[unknown_hash, genesis_hash]`, forcing the victim node to perform up to 4,000 RocksDB reads per message with no application-level rate limiting on the `Synchronizer`'s incoming sync message path. The `getheaders_received` hook intended for throttling is an unimplemented no-op, and the `pending_get_headers` guard applies only to outgoing requests.

## Finding Description

**Entry point:** `GetHeadersProcess::execute()` in `sync/src/synchronizer/get_headers_process.rs`.

Two guards exist but are insufficient:

1. **Locator size check** (L46–51): rejects if `locator_size > MAX_LOCATOR_SIZE` (101). A two-element locator `[unknown_hash, genesis_hash]` passes trivially.

2. **IBD check** (L53–66): ignored when the node is not in IBD, which is the precondition for this attack.

After both guards pass, `locate_latest_common_block` (L68–69) is called. In `sync/src/types/mod.rs` L1857–1903, the function first verifies the last locator element is the genesis hash (L1867), then iterates to find the first known block. With `[unknown_hash, genesis_hash]`, genesis is found at index 1 with `block_number = 0`. The condition at L1879 (`index == 0 || latest_common == Some(0)`) returns `Some(0)` immediately, guaranteeing the worst-case path.

`get_locator_response(0, ...)` is then called (L78–79), executing:

```rust
std::iter::successors(Some(start_number), |number| number.checked_add(1))
    .take_while(|number| *number <= tip_number)
    .take(MAX_HEADERS_LEN)                                          // up to 2000 iterations
    .filter_map(|block_number| self.snapshot.get_block_hash(block_number))   // DB read #1
    .take_while(|block_hash| block_hash != hash_stop)
    .filter_map(|block_hash| self.sync_shared.store().get_block_header(&block_hash)) // DB read #2
    .collect()
```

With `tip > 2000`, this issues up to **4,000 RocksDB reads per single `GetHeaders` message**.

**No incoming rate limit exists on the `Synchronizer` path.** `Synchronizer::try_process` (L381–423 in `sync/src/synchronizer/mod.rs`) dispatches `GetHeaders` directly to `GetHeadersProcess::execute()` with no rate-limiter check — unlike `Relayer`, which has an explicit `governor`-based `rate_limiter` field checked before every message dispatch.

`getheaders_received` (called at L77) is confirmed to be a no-op TODO:

```rust
pub fn getheaders_received(&self, _peer: PeerIndex) {
    // TODO:
}
```

The `pending_get_headers` / `GET_HEADERS_TIMEOUT` mechanism (L1929–1951 in `sync/src/types/mod.rs`) is exclusively inside `send_getheaders_to_peer` — it throttles *outgoing* requests only and has no effect on incoming `GetHeaders` processing.

## Impact Explanation

Each crafted `GetHeaders` message causes up to 4,000 RocksDB reads on the victim node. An attacker maintaining a persistent peer connection can send these messages at the maximum rate the network allows, causing sustained IO amplification. This degrades node responsiveness for legitimate peers and sync operations. Impact is **Low (501–2000)** — measurable performance degradation on a single node, not a full outage or network-wide event. This matches the allowed CKB bounty impact: *"Any other important performance improvements for CKB."*

## Likelihood Explanation

The attack requires only a standard P2P peer connection — no PoW, no keys, no special privileges. The locator `[random_unknown_hash, genesis_hash]` is trivially constructed; the genesis hash is public. The condition `locate_latest_common_block` returning `Some(0)` is guaranteed by the code path at L1867–1879 when the first hash is unknown and the second is genesis. The attack is repeatable indefinitely from a single persistent connection.

## Recommendation

1. Add a per-peer rate limit on incoming `GetHeaders` messages in `Synchronizer::try_process`, analogous to the `governor`-based `rate_limiter` already present in `Relayer`.
2. Implement the `getheaders_received` TODO with actual throttling logic (e.g., token bucket or cooldown per peer).
3. Consider banning or throttling peers that repeatedly anchor to genesis (block 0) when the node's tip is far ahead — this is a strong signal of adversarial behavior.
4. Alternatively, cap `get_locator_response` to a smaller window when the common ancestor is very far behind the tip.

## Proof of Concept

```
1. Connect to a non-IBD CKB node (tip > 2000) as a standard peer.
2. Send GetHeaders with:
     block_locator_hashes = [random_32_byte_hash, genesis_hash]
     hash_stop = 0x000...000
3. Observe: locate_latest_common_block returns Some(0);
   get_locator_response(0, ...) iterates 2000 slots,
   issuing 2000 × get_block_hash + 2000 × get_block_header = 4000 RocksDB reads.
4. Repeat in a tight loop from the same peer connection.
5. Profile RocksDB read counters via metrics — assert ~4000 reads per message
   vs. ~2 reads when common ancestor is near tip.
```