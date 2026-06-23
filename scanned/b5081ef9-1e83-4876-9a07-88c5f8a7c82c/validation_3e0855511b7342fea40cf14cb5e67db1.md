Now I have the full code. Let me carefully trace the exact arithmetic to verify or refute the claim.

The code is fully readable. Let me trace the exact arithmetic.

**Limit check (lines 201–205):**
```
difficulties.len() + last_n_blocks * 2 > GET_LAST_STATE_PROOF_LIMIT (1000)
```
With `difficulties=[]`, `last_n_blocks=499`: `0 + 998 = 998 ≤ 1000` → **passes**.

**`reorg_last_n_numbers` (lines 237–247):**
When `start_hash` ≠ ancestor at `start_number`, the range is `(start_number - min(start_number, last_n_blocks)..start_number)` → up to `last_n_blocks` = 499 entries.

**`last_n_numbers` (lines 298–319) — the broken assumption:**

`difficulty_boundary_block_number` is set by `get_first_block_total_difficulty_is_not_less_than(start_block_number, last_block_number, &difficulty_boundary)`. If `difficulty_boundary` is set to `1` (or any value ≤ total difficulty at `start_block_number`), the function immediately returns `start_block_number` at line 31–32.

The guard at line 313 only fires when `last_block_number - difficulty_boundary_block_number < last_n_blocks` (i.e., too few blocks after the boundary). When `difficulty_boundary_block_number = start_block_number` and the chain is long, `last_block_number - start_block_number >> last_n_blocks`, so the guard does **not** fire.

Result: `last_n_numbers = (start_block_number..last_block_number)` → `last_block_number - start_block_number` entries, which is **unbounded**.

**Concrete numbers:**
- Chain height: 1,000,000
- `start_block_number = 500`, `last_n_blocks = 499`, `difficulties = []`, `difficulty_boundary = 1`
- `reorg_last_n_numbers.len() = 499`
- `sampled_numbers.len() = 0`
- `last_n_numbers.len() = 999,500`
- `block_numbers.len() = 999,999` >> 1000

`complete_headers` then iterates all 999,999 entries, calling `get_ancestor`, `get_block`, and `chain_root_mmr` per entry.

---

### Title
Unbounded `last_n_numbers` bypasses `GET_LAST_STATE_PROOF_LIMIT` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
The upfront limit check in `execute` assumes `last_n_numbers.len() ≤ last_n_blocks`, but this invariant is not enforced. An attacker-controlled `difficulty_boundary` value can force `difficulty_boundary_block_number = start_block_number`, making `last_n_numbers` span the entire chain from `start_block_number` to `last_block_number`. The resulting `block_numbers` vector can contain millions of entries, causing unbounded memory allocation and CPU work in `complete_headers`.

### Finding Description
The guard at lines 201–205 checks:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

This check assumes `last_n_numbers.len() ≤ last_n_blocks`. That assumption holds only when the adjustment at line 313–316 fires:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
``` [2](#0-1) 

The adjustment only fires when there are **fewer** than `last_n_blocks` blocks after the boundary. When `difficulty_boundary_block_number` is near `start_block_number` (because `difficulty_boundary` is set to a tiny value), the condition is false and `last_n_numbers` becomes:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

…which spans the entire chain from `start_block_number` to `last_block_number`.

`get_first_block_total_difficulty_is_not_less_than` returns `start_block_number` immediately when the total difficulty there already meets `difficulty_boundary`: [4](#0-3) 

With `difficulties=[]`, there is no validation on `difficulty_boundary` (the checks at lines 259–288 are all gated on `difficulties` being non-empty), so the attacker can freely set it to `1`. [5](#0-4) 

The oversized `block_numbers` is then passed to `complete_headers`, which performs O(n) chain lookups and MMR root computations per entry: [6](#0-5) 

`GET_LAST_STATE_PROOF_LIMIT` is 1000: [7](#0-6) 

### Impact Explanation
An unprivileged light-client peer can force the full-node server to allocate a `Vec` of millions of block numbers and then perform millions of database reads and MMR root computations inside `complete_headers`. On a mainnet node with millions of blocks, this causes severe CPU and memory exhaustion, leading to OOM kill or an indefinite hang — matching the "local node crash or excessive resource use" scope.

### Likelihood Explanation
The attack requires only a valid `last_hash` on the main chain (trivially obtained from `SendLastState`) and a crafted `start_hash` that does not match the ancestor at `start_number` (any random hash suffices). No PoW, no key, no privileged role. A single malicious peer can trigger this repeatedly.

### Recommendation
After computing `block_numbers` at line 350–354, add a post-condition guard:

```rust
if block_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, fix the upfront check to use the actual upper bound: `difficulties.len() + last_n_blocks * 2 + (last_block_number - start_block_number)`, or clamp `difficulty_boundary_block_number` so that `last_n_numbers.len()` is always ≤ `last_n_blocks`.

### Proof of Concept
Craft a `GetLastStateProof` message:
- `last_hash` = current tip hash (obtained from `SendLastState`)
- `start_hash` = any 32-byte value that is **not** the ancestor of `last_hash` at `start_number`
- `start_number` = 500
- `last_n_blocks` = 499
- `difficulty_boundary` = `U256::from(1u64)` (guaranteed ≤ total difficulty at block 500)
- `difficulties` = `[]`

On a node with chain height ≥ 1,000,000:
1. Limit check: `0 + 998 = 998 ≤ 1000` → passes.
2. `reorg_last_n_numbers` = 499 entries (reorg path active).
3. `difficulty_boundary_block_number` = 500 (total difficulty at 500 ≥ 1).
4. `last_block_number − 500 ≥ 499` → no adjustment.
5. `last_n_numbers` = ~999,500 entries.
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L259-288)
```rust
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The first difficulty should be greater than the total difficulty before the start block.
            if let Some(start_difficulty) = difficulties.first()
                && start_block_number > 0
            {
                let previous_block_number = start_block_number - 1;
                if let Some(total_difficulty) =
                    sampler.get_block_total_difficulty(previous_block_number)
                {
                    if total_difficulty >= *start_difficulty {
                        let errmsg = format!(
                            "the start difficulty is {start_difficulty:#x} too less than \
                                the previous block #{previous_block_number} of the start block"
                        );
                        return StatusCode::InvalidRequest.with_context(errmsg);
                    }
                } else {
                    let errmsg = format!(
                        "the total difficulty for block#{previous_block_number} is not found"
                    );
                    return StatusCode::InternalError.with_context(errmsg);
                };
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
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
