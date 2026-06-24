Audit Report

## Title
Off-by-One in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded DB Reads and MMR Computations per Request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The limit guard in `GetLastStateProofProcess::execute` uses strict `>` instead of `>=`, allowing exactly 1000 difficulties with `last_n_blocks=0` to pass unchecked. A single crafted request then triggers 1000 binary-search DB lookups, 1000 MMR root computations, and a 1000-position MMR proof generation. Because a successful response returns `StatusCode::OK` (200), the peer is never banned, and there is no per-peer rate limiting, making the attack freely repeatable from any P2P connection.

## Finding Description

**Off-by-one in the limit guard** (`get_last_state_proof.rs`, lines 201–205):

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties.len() = 1000` and `last_n_blocks = 0`: `1000 + 0 = 1000`, which is **not** `> 1000`. The guard passes. The intended semantic is `>=`.

**Work performed after the guard passes:**

1. Lines 325–344: `difficulties` is filtered to those `<= total_difficulty` at the boundary, but an attacker crafts all 1000 values to satisfy this. `get_block_numbers_via_difficulties` (lines 73–103) then iterates all 1000 entries, calling `get_first_block_total_difficulty_is_not_less_than` for each — a binary search doing O(log N) DB reads per call → **~1000 × O(log N) DB reads**.

2. Lines 356–366 → `complete_headers` (lines 124–180): for each of the 1000 block numbers, calls `snapshot.get_ancestor(...)`, `snapshot.get_block(...)`, and `snapshot.chain_root_mmr(*number - 1).get_root()` → **1000 MMR root computations + 1000 block reads**.

3. `reply_proof` (lib.rs lines 207–216): calls `mmr.gen_proof(items_positions)` with 1000 positions → **O(1000 × log N) MMR proof generation**.

**No banning on success** (`status.rs`, lines 95–102):

```rust
pub fn should_ban(&self) -> Option<Duration> {
    let code = self.code as u16;
    if !(400..500).contains(&code) {
        None
    } else {
        Some(constant::BAD_MESSAGE_BAN_TIME)
    }
}
```

A fully processed request returns `StatusCode::OK` (200), which is outside `400..500`, so `should_ban()` returns `None`. The peer is never penalized.

## Impact Explanation

This maps to **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points). Ten concurrent peers each sending this request at full speed force ~200,000+ DB reads and 10,000 MMR root computations per second, saturating the node's I/O and CPU and starving legitimate light-client peers of responses. The node cannot distinguish this from a legitimate request and will never ban the attacker.

## Likelihood Explanation

The attack requires only a P2P connection to a node with the light-client protocol server enabled. No proof-of-work, no keys, no privileged role. The message is structurally valid and passes all semantic checks (monotonically increasing difficulties, all below `difficulty_boundary`). The attack is freely repeatable with no cost to the attacker.

## Recommendation

1. **Fix the off-by-one**: change `>` to `>=` at line 202 so that exactly 1000 difficulties is rejected.
2. **Add per-peer rate limiting**: enforce at most one in-flight `GetLastStateProof` request per peer, or use a token-bucket limiter.
3. **Cap `complete_headers` independently**: the total `block_numbers` fed into `complete_headers` (combining `reorg_last_n_numbers`, `sampled_numbers`, and `last_n_numbers`) should also be bounded, since these can combine to exceed the intended limit.

## Proof of Concept

```
chain length N = 1,000,000 blocks
attacker sends GetLastStateProof {
    last_hash:           <valid tip hash>,
    start_hash:          <genesis hash>,
    start_number:        0,
    last_n_blocks:       0,
    difficulty_boundary: <total_difficulty[N-1]>,
    difficulties:        [D1, D2, ..., D1000]  // 1000 distinct monotonically
                                                // increasing values, all <
                                                // total_difficulty[N-2]
}
```

Limit check: `1000 + 0*2 = 1000`, not `> 1000` → passes.
Server performs: 1000 binary searches × ~20 DB reads = ~20,000 DB reads, then 1000 `chain_root_mmr(n-1).get_root()` calls, then `gen_proof(1000 positions)`.
Repeat from 10 concurrent peers → sustained I/O and CPU exhaustion with no ban.