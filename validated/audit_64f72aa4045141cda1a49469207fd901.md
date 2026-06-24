Audit Report

## Title
Unbounded DB Read Amplification via `GetLastStateProof` with Maximum Difficulties Array — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The `GetLastStateProof` handler allows exactly 1000 `difficulties` entries due to a strict `>` comparison against `GET_LAST_STATE_PROOF_LIMIT`. Each entry triggers an O(log H) binary search over the full block range with one `get_block_total_difficulty` DB read per step. With no per-peer rate limiter on `LightClientProtocol` and no ban issued on successful responses, an attacker can sustain ~24,000 synchronous DB reads per request in a tight loop, saturating RocksDB I/O and degrading or halting the node.

## Finding Description
**Off-by-one in limit check:** In `execute()` at line 201–205 of `get_last_state_proof.rs`:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // 1000
```
With `len=1000` and `last_n_blocks=0`, `1000 + 0 > 1000` evaluates to `false`, so exactly 1000 entries pass the guard. `GET_LAST_STATE_PROOF_LIMIT` is defined as `1000` in `constant.rs`.

**O(log H) binary search per entry:** `get_first_block_total_difficulty_is_not_less_than` (lines 24–71) performs a binary search over `[start_block_number, end_block_number)`, issuing one `get_block_total_difficulty` call per iteration. For H ≈ 12 M blocks, this is ≈ log₂(12 M) ≈ 24 reads per call.

**`start_block_number` narrowing is ineffective:** In `get_block_numbers_via_difficulties` (lines 90–91):
```rust
if num > start_block_number {
    start_block_number = num - 1;
}
```
If the attacker supplies difficulties resolving to blocks 1, 2, 3, …, 1000, then for `num=1`, `start_block_number` stays at 0; for `num=2`, it becomes 1. Each subsequent binary search still spans ≈ H blocks, yielding ≈ 24 reads each. Total: ≈ 24,000 DB reads per request.

**Additional work in `complete_headers`:** For each resolved block number, `complete_headers` (lines 124–181) additionally calls `get_ancestor`, `get_block`, and `chain_root_mmr(...).get_root()`, multiplying the actual DB and MMR work beyond the binary search reads alone.

**No rate limiting:** `LightClientProtocol::received` (lib.rs lines 55–92) calls `try_process` directly with no rate limiter. No `governor::RateLimiter` or equivalent exists anywhere in `util/light-client-protocol-server/`.

**No ban on success:** `should_ban()` (status.rs lines 95–102) only returns `Some(ban_time)` for 4xx status codes. A successful `SendLastStateProof` response returns `StatusCode::OK` (200), which does not trigger a ban. `InternalError` (500) only emits a `warn!` log.

## Impact Explanation
An attacker with a single P2P connection to a node running the light client server can flood it with back-to-back `GetLastStateProof` messages, each causing ≈ 24,000+ synchronous RocksDB reads plus MMR computations. This saturates DB I/O, starves block-processing and other protocol threads of DB access, and can degrade or halt the node. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light client server enabled. Chain total-difficulty values are publicly observable on-chain. No PoW, no special privileges, and no leaked keys are required. The absence of any per-peer rate limit on this handler makes sustained exploitation straightforward and repeatable with a single connection.

## Recommendation
- Add a per-peer rate limiter to `LightClientProtocol` (analogous to `HolePunching`'s `governor::RateLimiter`) checked at the top of `received` before calling `try_process`.
- Change the limit check from `>` to `>=` so the maximum permitted count is `GET_LAST_STATE_PROOF_LIMIT - 1` (999), or reduce the constant to account for the O(log H) multiplier per entry.
- Consider tracking total DB read iterations across all binary searches in a single message and aborting early if a per-request budget is exceeded.

## Proof of Concept
1. Connect to a light client server node with chain height H (e.g., 12 M blocks).
2. Query the chain to obtain `total_difficulty(i)` for blocks `i = 1 … 1001`.
3. Send `GetLastStateProof { last_hash: tip_hash, start_hash: genesis_hash, start_number: 0, last_n_blocks: 0, difficulty_boundary: total_difficulty(1001), difficulties: [total_difficulty(1), …, total_difficulty(1000)] }`.
4. Observe: limit check passes (`1000 + 0 > 1000` → false); server performs 1000 binary searches each spanning [0, H); `complete_headers` performs additional MMR and block reads for each resolved block; server returns `SendLastStateProof` (200 OK); peer is not banned.
5. Repeat in a tight loop. Each iteration issues ≈ 24,000+ DB reads with no throttle, progressively starving the node of DB I/O.