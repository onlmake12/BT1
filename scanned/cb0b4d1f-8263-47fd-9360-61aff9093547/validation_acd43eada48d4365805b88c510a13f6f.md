Audit Report

## Title
Unbounded `last_n_numbers` bypasses `GET_LAST_STATE_PROOF_LIMIT` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The upfront limit check in `execute` bounds `last_n_numbers` to at most `last_n_blocks` entries, but this invariant is not enforced. An attacker-controlled `difficulty_boundary` set to `1` forces `difficulty_boundary_block_number = start_block_number`, making `last_n_numbers` span the entire chain from `start_block_number` to `last_block_number`. The resulting `block_numbers` vector can contain millions of entries, causing unbounded memory allocation and CPU work in `complete_headers`.

## Finding Description
The upfront guard at lines 201–205 checks:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

This assumes `last_n_numbers.len() ≤ last_n_blocks`. That assumption holds only when the adjustment at lines 313–316 fires:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

The adjustment only fires when there are **fewer** than `last_n_blocks` blocks after the boundary. When `difficulty_boundary_block_number` equals `start_block_number` (because `difficulty_boundary` is set to a tiny value like `1`), the condition `last_block_number - start_block_number < last_n_blocks` is false on a long chain, so the guard does not fire.

`get_first_block_total_difficulty_is_not_less_than` returns `start_block_number` immediately at lines 30–32 when the total difficulty there already meets `difficulty_boundary`:

```rust
if start_total_difficulty >= *min_total_difficulty {
    return Some((start_block_number, start_total_difficulty));
}
```

With `difficulties=[]`, the validation checks at lines 259–288 are all gated on `difficulties` being non-empty (via `unwrap_or(false)` and `if let Some(...) = difficulties.first()`), so `difficulty_boundary` is never validated against chain state.

Result: `last_n_numbers = (start_block_number..last_block_number)` at lines 318–319, which spans the entire chain. The oversized `block_numbers` is then passed to `complete_headers` at line 359, which performs O(n) `get_ancestor`, `get_block`, and `chain_root_mmr` calls per entry. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

## Impact Explanation
An unprivileged light-client peer can force the full-node server to allocate a `Vec` of millions of block numbers and then perform millions of database reads and MMR root computations inside `complete_headers`. On a mainnet node with millions of blocks, this causes severe CPU and memory exhaustion, leading to OOM kill or an indefinite hang. This matches **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The attack requires only a valid `last_hash` on the main chain (trivially obtained from `SendLastState`) and a `difficulty_boundary` of `1`. No proof-of-work, no key, no privileged role is needed. A single malicious peer can trigger this repeatedly, and the cost to the attacker is negligible (one crafted message per attack).

## Recommendation
After computing `block_numbers` at lines 350–354, add a post-condition guard:

```rust
if block_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, clamp `difficulty_boundary_block_number` so that `last_n_numbers.len()` is always ≤ `last_n_blocks`, or fix the upfront check to account for the actual upper bound of `last_n_numbers`.

## Proof of Concept
Craft a `GetLastStateProof` message with:
- `last_hash` = current tip hash (from `SendLastState`)
- `start_hash` = any hash not equal to the ancestor of `last_hash` at `start_number`
- `start_number` = 500
- `last_n_blocks` = 499
- `difficulty_boundary` = `U256::from(1u64)`
- `difficulties` = `[]`

On a node with chain height ≥ 1,000,000:
1. Limit check: `0 + 998 = 998 ≤ 1000` → passes.
2. `reorg_last_n_numbers` = 499 entries.
3. `difficulty_boundary_block_number` = 500 (total difficulty at block 500 ≥ 1).
4. `1,000,000 − 500 = 999,500 ≥ 499` → adjustment guard does not fire.
5. `last_n_numbers` = 999,500 entries.
6. `block_numbers.len()` ≈ 999,999 >> 1000.
7. `complete_headers` performs ~999,999 `get_ancestor` + `get_block` + `chain_root_mmr` calls → OOM or hang.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L259-266)
```rust
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-319)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }

            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L356-366)
```rust
        let (positions, headers) = {
            let mut positions: Vec<u64> = Vec::new();
            let headers =
                match sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers) {
                    Ok(headers) => headers,
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                };
            (positions, headers)
        };
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
