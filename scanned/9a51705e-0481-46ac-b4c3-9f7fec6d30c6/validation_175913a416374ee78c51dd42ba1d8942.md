The code confirms the vulnerability. Here is the complete trace:

---

### Title
Unbounded `last_n_numbers` via Low `difficulty_boundary` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

The upfront limit check at line 201 uses the attacker-supplied `last_n_blocks` field to bound work, but the actual `last_n_numbers` vector is sized by `difficulty_boundary_block_number`, which is derived from the attacker-supplied `difficulty_boundary`. When `difficulty_boundary` is set to the total difficulty of block 1, `difficulty_boundary_block_number` resolves to 1, and `last_n_numbers` becomes `(1..last_block_number)` — N−1 entries — completely unchecked.

### Finding Description

**Limit check (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `difficulties=[]` and `last_n_blocks=1`, this evaluates to `0 + 2 = 2`, well under 1000. The check passes. [1](#0-0) 

**Else-branch entered (lines 291–292):** For a chain of N >> 1 blocks with `start_block_number=0`, `N - 0 <= 1` is false, so the else branch is taken. [2](#0-1) 

**`difficulty_boundary_block_number` resolves to 1 (lines 299–311):** `get_first_block_total_difficulty_is_not_less_than(0, N, total_difficulty[1])` returns block 1. [3](#0-2) 

**Adjustment skipped (lines 313–316):** The guard `N - 1 < 1` is false for any N > 2, so `difficulty_boundary_block_number` stays at 1. [4](#0-3) 

**`last_n_numbers` becomes `(1..N)` — N−1 entries (lines 318–319):** [5](#0-4) 

**`complete_headers` iterates all N−1 entries (lines 356–366):** For each entry, `chain_root_mmr(number - 1).get_root()` is called, performing O(log N) DB reads per block. [6](#0-5) 

Total DB reads per single request: **O(N log N)**, unbounded by `GET_LAST_STATE_PROOF_LIMIT = 1000`. [7](#0-6) 

### Impact Explanation

A single malicious peer can send one crafted `GetLastStateProof` message that forces the server to perform O(N log N) synchronous DB reads (MMR root computations) before returning. On a mainnet node with millions of blocks, this saturates I/O and CPU on the light-client server thread, effectively denying service to all other peers. No PoW, no privileged role, no key material required.

### Likelihood Explanation

The attack requires only a valid `last_hash` pointing to a real tip block (observable from the network) and knowledge of block 1's total difficulty (a public, fixed constant on mainnet). Any peer can craft this message. The condition is deterministic and reproducible.

### Recommendation

After computing `last_n_numbers`, add a hard bound before calling `complete_headers`:

```rust
if last_n_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage
        .with_context("last_n_numbers exceeds limit");
}
```

Alternatively, clamp `difficulty_boundary_block_number` from below:

```rust
let min_boundary = last_block_number.saturating_sub(
    (constant::GET_LAST_STATE_PROOF_LIMIT as u64).saturating_sub(last_n_blocks)
);
if difficulty_boundary_block_number < min_boundary {
    difficulty_boundary_block_number = min_boundary;
}
```

The fix must be applied **after** the difficulty boundary is resolved and **before** `last_n_numbers` is collected. [8](#0-7) 

### Proof of Concept

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
4. Server resolves difficulty_boundary_block_number = 1.
5. Guard (N - 1 < 1) is false → no adjustment.
6. last_n_numbers = (1..N), length = N-1.
7. complete_headers iterates N-1 blocks, each doing O(log N) MMR DB reads.
8. Differential test: measure response latency for difficulty_boundary=total_difficulty[1]
   vs difficulty_boundary=total_difficulty[N-last_n_blocks].
   The former scales as O(N log N); the latter is O(log N). QED.
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-298)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
        } else {
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L299-311)
```rust
            let mut difficulty_boundary_block_number = if let Some((num, _)) = sampler
                .get_first_block_total_difficulty_is_not_less_than(
                    start_block_number,
                    last_block_number,
                    &difficulty_boundary,
                ) {
                num
            } else {
                let errmsg = format!(
                    "the difficulty boundary ({difficulty_boundary:#x}) is not in the block range [{start_block_number}, {last_block_number})"
                );
                return StatusCode::InvaildDifficultyBoundary.with_context(errmsg);
            };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-348)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();

            if difficulty_boundary_block_number > 0 {
                if let Some(total_difficulty) =
                    sampler.get_block_total_difficulty(difficulty_boundary_block_number - 1)
                {
                    difficulties = difficulties
                        .into_iter()
                        .take_while(|d| *d <= total_difficulty)
                        .collect();
                } else {
                    let errmsg = format!(
                        "the total difficulty for block#{difficulty_boundary_block_number} is not found"
                    );
                    return StatusCode::InternalError.with_context(errmsg);
                };
                match sampler.get_block_numbers_via_difficulties(
                    start_block_number,
                    difficulty_boundary_block_number,
                    &difficulties,
                ) {
                    Ok(sampled_numbers) => (sampled_numbers, last_n_numbers),
                    Err(errmsg) => {
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            } else {
                (Vec::new(), last_n_numbers)
            }
        };
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
