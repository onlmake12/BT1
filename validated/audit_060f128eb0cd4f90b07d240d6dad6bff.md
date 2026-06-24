I'll verify all technical claims against the actual source code before rendering a verdict.

All technical claims verified against source. The `RateLimiter` exists in `sync/src/relayer/mod.rs` but is absent from the entire `util/light-client-protocol-server/` tree, confirming that claim as well.

---

Audit Report

## Title
Unbounded DB Read Amplification via Crafted `GetLastStateProof` Difficulties Array — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
`GetLastStateProofProcess::execute` uses a strict `>` comparison against `GET_LAST_STATE_PROOF_LIMIT` (1000), allowing exactly 1000 difficulty entries to pass the guard when `last_n_blocks=0`. Each entry triggers a binary search over the full chain height performing O(log H) RocksDB reads. Because a well-formed request returns `Status::ok()` (HTTP 200), `should_ban()` never fires and the peer can repeat the request indefinitely with no penalty.

## Finding Description
**Root cause — off-by-one in the size guard:**
`GET_LAST_STATE_PROOF_LIMIT = 1000` is defined in `util/light-client-protocol-server/src/constant.rs` line 6. The guard at `get_last_state_proof.rs` lines 201–204 reads:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```
With `difficulties.len()=1000` and `last_n_blocks=0`, the expression evaluates to `1000 > 1000` = false, so the check passes.

**Amplification — O(log H) DB reads per difficulty entry:**
`get_block_numbers_via_difficulties` (lines 73–103) iterates all 1000 entries and calls `get_first_block_total_difficulty_is_not_less_than` for each. That function (lines 29–71) performs a binary search loop, calling `get_block_total_difficulty` each iteration. Each `get_block_total_difficulty` call (lines 111–116) issues two RocksDB reads: `get_block_hash` followed by `get_block_ext`.

**`start_block_number` narrowing does not eliminate worst-case:** Setting `start_block_number = num - 1` only helps when found blocks are near the end of the range. An attacker who crafts difficulties mapping to blocks near the start of the chain keeps the search range near-maximal for all 1000 iterations.

**Further amplification in `complete_headers`:** Each of the up to 1000 found block numbers then triggers `get_ancestor` (O(log H) skip-list reads), `get_block`, and `chain_root_mmr` (lines 132–163).

**No rate limiter on `LightClientProtocol`:** The struct (lib.rs lines 26–29) has only a `shared: Shared` field and no `RateLimiter`. The `received()` handler (lines 55–92) performs no rate-limit check before dispatching. By contrast, `sync/src/relayer/mod.rs` does carry a `RateLimiter`.

**No banning for valid requests:** `should_ban()` (status.rs lines 95–102) only fires for 4xx status codes. A well-formed request returns `Status::ok()` (code 200), which is outside the ban range.

**`start_block_number=0` bypasses first-difficulty validation:** The check at lines 268–288 is guarded by `start_block_number > 0`, so when the attacker sets `start_number=0` the first-difficulty validation is entirely skipped.

## Impact Explanation
With H = 10,000,000 (log₂ ≈ 23), a single request causes approximately 1000 × 23 × 2 ≈ 46,000 RocksDB point reads plus additional reads in `complete_headers`. A single persistent connection sending back-to-back requests saturates disk I/O and starves the main chain processing loop, leading to node unresponsiveness. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a valid light-client connection and knowledge of the chain's total difficulty range, which is public on-chain data. No proof-of-work, no key, no privileged role is needed. The crafted message is structurally valid and passes all existing validation checks. The attack is trivially repeatable with no cost to the attacker.

## Recommendation
1. Change `>` to `>=` in the limit check at line 201, so that exactly `GET_LAST_STATE_PROOF_LIMIT` entries are rejected rather than allowed.
2. Add a per-peer rate limiter to `LightClientProtocol` (e.g., `governor::RateLimiter` keyed by `PeerIndex`) analogous to the guards present in `sync/src/relayer/mod.rs`.
3. Consider counting each difficulty entry as costing more than 1 unit (e.g., proportional to log₂(H)) toward the limit, or caching total-difficulty lookups within a single request to reduce per-entry DB cost.

## Proof of Concept
```
1. Connect to a CKB node with light-client protocol enabled.
2. Obtain chain tip hash T, genesis hash G, and chain total difficulty D_tip.
3. Build GetLastStateProof:
     last_hash           = T
     start_hash          = G
     start_number        = 0
     last_n_blocks       = 0
     difficulty_boundary = D_tip   // maps to last block; boundary_block = last_block_number
     difficulties        = [D_tip/1001, 2*D_tip/1001, ..., 1000*D_tip/1001]
                           // 1000 strictly increasing values, all < D_tip
4. Send in a tight loop.
   - Each iteration: size guard passes (1000 > 1000 = false), all validation passes,
     ~46,000 RocksDB reads execute, response is SendLastStateProof (OK 200).
   - Peer is never banned. Disk I/O saturates; node becomes unresponsive.
```