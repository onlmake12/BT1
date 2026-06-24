Audit Report

## Title
Unbounded `last_n_numbers` via Low `difficulty_boundary` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The upfront limit check at line 201 gates only on `difficulties.len() + last_n_blocks * 2`, but the actual work vector `last_n_numbers` is sized by `difficulty_boundary_block_number`, which is fully attacker-controlled via the `difficulty_boundary` field. By supplying `difficulty_boundary = total_difficulty[1]`, an attacker forces `last_n_numbers` to span the entire chain (N−1 entries), bypassing `GET_LAST_STATE_PROOF_LIMIT = 1000` entirely. Each entry triggers at minimum one `get_ancestor` traversal and one `chain_root_mmr(...).get_root()` call, resulting in O(N²) or O(N log N) synchronous DB work per single crafted request.

## Finding Description

**Limit check (lines 201–205):** With `difficulties=[]` and `last_n_blocks=1`, the expression evaluates to `0 + 1*2 = 2`, which is not `> 1000`. The check passes unconditionally.

**Else-branch entered (lines 291–298):** For a chain of N >> 1 blocks with `start_block_number=0` and `last_n_blocks=1`, the condition `N - 0 <= 1` is false, so the else branch is taken.

**`difficulty_boundary_block_number` resolves to 1 (lines 299–311):** `get_first_block_total_difficulty_is_not_less_than(0, N, total_difficulty[1])` binary-searches for the first block whose cumulative difficulty ≥ `total_difficulty[1]`. Since `total_difficulty[0] < total_difficulty[1]` by definition, and block 1 is exactly the first block meeting the threshold, the function returns `num = 1`.

**Adjustment guard skipped (lines 313–316):** The guard `N - 1 < 1` is false for any N > 2, so `difficulty_boundary_block_number` remains 1.

**`last_n_numbers` becomes `(1..N)` — N−1 entries (lines 318–319):** No bound is applied to this range before it is collected. `GET_LAST_STATE_PROOF_LIMIT` is never checked against `last_n_numbers.len()`.

**`complete_headers` iterates all N−1 entries (lines 356–366):** The full `block_numbers` vector is passed to `complete_headers`. For each entry, `get_ancestor(last_hash, *number)` (O(N) traversal from tip) and `chain_root_mmr(*number - 1).get_root()` (O(log N) MMR reads) are both called synchronously.

**Root cause:** The limit check is structurally disconnected from the actual work performed. It bounds `last_n_blocks` (a field the attacker can set to 1) but never bounds the derived `last_n_numbers` vector, which is controlled via the orthogonal `difficulty_boundary` field.

## Impact Explanation
A single malicious peer can send one crafted `GetLastStateProof` message that forces the server to perform O(N²) synchronous DB reads (N−1 `get_ancestor` calls each traversing up to N blocks) before returning. On a mainnet node with millions of blocks, this saturates I/O and CPU on the light-client server thread, effectively denying service to all other peers. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

## Likelihood Explanation
The attack requires only: (1) a valid `last_hash` pointing to a real tip block, observable from the P2P network; and (2) knowledge of block 1's total difficulty, a public fixed constant on mainnet. No privileges, no key material, and no victim mistakes are required. The condition is deterministic and reproducible by any peer with zero cost beyond sending one message.

## Recommendation
After computing `last_n_numbers` and before calling `complete_headers`, add a hard bound:

```rust
if last_n_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage
        .with_context("last_n_numbers exceeds limit");
}
```

Alternatively, clamp `difficulty_boundary_block_number` from below so that `last_n_numbers` cannot exceed `last_n_blocks`:

```rust
let min_boundary = last_block_number.saturating_sub(
    (constant::GET_LAST_STATE_PROOF_LIMIT as u64).saturating_sub(last_n_blocks)
);
if difficulty_boundary_block_number < min_boundary {
    difficulty_boundary_block_number = min_boundary;
}
```

The fix must be applied after line 311 (difficulty boundary resolved) and before line 318 (`last_n_numbers` collected).

## Proof of Concept
```
1. Observe the current tip hash H_tip and block number N from the P2P network.
2. Obtain total_difficulty[1] (public constant for mainnet genesis+1 block).
3. Send GetLastStateProof {
       last_hash:           H_tip,
       last_n_blocks:       1,
       difficulty_boundary: total_difficulty[1],
       difficulties:        [],
       start_hash:          genesis_hash,
       start_number:        0,
   }
4. Server passes limit check: 0 + 1*2 = 2 ≤ 1000.
5. Else-branch taken: N - 0 > 1.
6. difficulty_boundary_block_number = 1 (binary search result).
7. Guard N - 1 < 1 is false → no adjustment.
8. last_n_numbers = (1..N), length = N-1, unchecked against GET_LAST_STATE_PROOF_LIMIT.
9. complete_headers iterates N-1 blocks; each calls get_ancestor (O(N)) + chain_root_mmr (O(log N)).
10. Differential test: measure response latency for difficulty_boundary=total_difficulty[1]
    vs difficulty_boundary=total_difficulty[N-1]. The former scales as O(N²); the latter is O(log N). QED.
```