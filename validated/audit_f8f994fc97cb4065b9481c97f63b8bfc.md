Audit Report

## Title
Off-by-One in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded MMR DB Read Amplification with No Rate Limiting — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The limit guard in `GetLastStateProofProcess::execute()` uses strict `>` instead of `>=`, allowing a crafted `GetLastStateProof` message with `last_n_blocks=500` and an empty `difficulties` list to pass the check at exactly the boundary (`0 + 500×2 = 1000`, which is not `> 1000`). This causes the server to perform 500 separate `chain_root_mmr(n).get_root()` RocksDB reads, 500 ancestor/block lookups, and one large MMR proof generation per request. No per-peer rate limiter exists in `LightClientProtocol`, and a well-formed request always returns `Status::ok()`, so the peer is never banned or throttled.

## Finding Description

**Off-by-one in the limit guard.**
`GET_LAST_STATE_PROOF_LIMIT = 1000` is defined at `util/light-client-protocol-server/src/constant.rs` L6. The guard at `get_last_state_proof.rs` L201–204 is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

With `difficulties = []` and `last_n_blocks = 500`: `0 + 500×2 = 1000`, which is **not** `> 1000`. The check passes.

**500 block numbers are collected.**
At L291–297, when `last_block_number - start_block_number <= last_n_blocks` (i.e., `500 <= 500`, true), the "not enough blocks" branch fires and `last_n_numbers` collects all 500 block numbers via `(start_block_number..last_block_number).collect()`.

**`complete_headers` calls `chain_root_mmr().get_root()` once per block.**
At L150–163, for each of the 500 block numbers, a fresh `ChainRootMMR` is constructed via `self.snapshot.chain_root_mmr(*number - 1)` and `mmr.get_root()` is called. Each `get_root()` reads all MMR peak nodes from RocksDB — O(log N) reads for a chain of height N (≈20–30 reads on mainnet). Additionally, `get_ancestor()` and `get_block()` are called per block at L133–146.

**No rate limiter in `LightClientProtocol`.**
A grep for `rate_limiter`, `RateLimiter`, or `rate_limit` across all of `util/light-client-protocol-server/` returns zero matches. By contrast, `sync/src/relayer/mod.rs` L88–123 explicitly constructs a `governor::RateLimiter` keyed by `(PeerIndex, message_item_id)` and checks it before every message dispatch.

**A valid request never triggers a ban.**
`should_ban()` in `util/light-client-protocol-server/src/status.rs` L95–102 returns `Some(BAD_MESSAGE_BAN_TIME)` only for 4xx status codes. A well-formed `GetLastStateProof` with `last_n_blocks=500` and empty difficulties passes all guards and returns `Status::ok()` (code 200), so `should_ban()` returns `None` and the peer is never penalized.

**Additional work per request.**
Beyond the 500 `get_root()` calls, `reply_proof` at `lib.rs` L207–216 calls `mmr.gen_proof(500 positions)` — an O(N log N) proof generation step over the full MMR.

## Impact Explanation
A single attacker peer can continuously send max-cost `GetLastStateProof` messages in a tight loop. Each message triggers ~500 RocksDB peak reads (×O(log N) each), 500 ancestor/block lookups, and one large MMR proof generation — all on the server's I/O and CPU. With no rate limiting and no ban, the attacker is never throttled. Multiple attacker IPs multiply the effect linearly. This can saturate the node's I/O subsystem and degrade or halt service for all other peers (sync, relay, legitimate light clients).

**Impact class: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires only a valid P2P connection to the light-client protocol endpoint, which is an unprivileged role. The crafted message is structurally valid and passes all existing guards. No PoW, no key, and no special privilege is needed. The attacker needs only one valid main-chain block hash, trivially obtained from any public explorer or by first sending `GetLastState`. The attack is repeatable indefinitely.

## Recommendation
1. **Fix the off-by-one**: change `>` to `>=` at `get_last_state_proof.rs` L202 so that `last_n_blocks=500` with empty difficulties is rejected (`1000 >= 1000`).
2. **Add a per-peer rate limiter** to `LightClientProtocol`, mirroring the `governor::RateLimiter` already used in `sync/src/relayer/mod.rs` L88–99, keyed by `(PeerIndex, message_item_id)`.
3. **Batch or cache MMR root reads**: instead of calling `chain_root_mmr(n).get_root()` independently for each block in `complete_headers`, compute roots in a single MMR traversal or cache peak nodes across calls within the same request.

## Proof of Concept
```
Preconditions:
  - Server chain height >= 500
  - Attacker connects as a light-client peer
  - tip_hash = any valid main-chain tip hash (from GetLastState)

Message fields:
  last_hash           = tip_hash
  start_hash          = hash of block (tip_number - 500)
  start_number        = tip_number - 500
  last_n_blocks       = 500
  difficulty_boundary = U256::MAX  (irrelevant for this branch)
  difficulties        = []

Limit check: 0 + 500*2 = 1000, NOT > 1000 → passes (L201–204)
Branch taken: last_block_number - start_block_number = 500 <= 500 → "not enough blocks" path (L291–297)
Result:
  - complete_headers called with 500 block numbers (L358–365)
  - 500 × chain_root_mmr(n-1).get_root() DB reads (L153–154)
  - 500 × get_ancestor() + get_block() lookups (L133–146)
  - mmr.gen_proof(500 positions) (L210)
  - Status::ok() returned → no ban (status.rs L95–102)

Repeat in a tight loop. No ban, no rate limit.
```