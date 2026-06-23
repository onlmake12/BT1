The code is fully readable. Let me trace the exact execution path for the claimed attack.

**Attack parameters:** `difficulties=[]`, `difficulty_boundary=U256::zero()`, `last_n_blocks=0`, `start_block_number=0`, valid `last_hash` on main chain.

**Trace through `execute()`:**

**Guard 1 — size check (lines 201–205):**
`difficulties.len() + last_n_blocks * 2 = 0 + 0 = 0 > 1000` → false → passes. [1](#0-0) 

**Guard 2 — monotonicity (line 254):**
`[].windows(2).any(...)` → false → passes. [2](#0-1) 

**Guard 3 — max difficulty < boundary (lines 259–266):**
`[].last()` → `None` → `.map(...)` → `None` → `.unwrap_or(false)` → `false` → **check is entirely skipped**. [3](#0-2) 

**Guard 4 — first difficulty > previous total difficulty (lines 268–288):**
`[].first()` → `None` → entire block skipped. [4](#0-3) 

**Main logic — `get_first_block_total_difficulty_is_not_less_than(0, N, &U256::zero())`:**
Block 0's total difficulty is always `>= U256::zero()`, so the function immediately returns `Some((0, genesis_total_difficulty))`, setting `difficulty_boundary_block_number = 0`. [5](#0-4) 

**Adjustment check (line 313):**
`last_block_number - 0 < 0` → false → no adjustment. [6](#0-5) 

**Result:**
`last_n_numbers = (0..last_block_number)` — the **entire chain**. Then `difficulty_boundary_block_number > 0` is false, so `sampled_numbers = []` and `last_n_numbers` is returned as-is. [7](#0-6) 

**`complete_headers` is then called with all N block numbers**, performing N DB reads for block hashes, N reads for `block_ext`, N reads for full blocks, and N MMR root computations — all for a single tiny P2P message. [8](#0-7) 

The `GET_LAST_STATE_PROOF_LIMIT = 1000` constant is intended to bound response size, but the size check formula `difficulties.len() + last_n_blocks * 2` only accounts for the sampled region and the reorg window — it does **not** account for the `last_n_numbers` range when `difficulty_boundary_block_number` collapses to `start_block_number`. [9](#0-8) 

---

### Title
Unbounded `last_n_numbers` via `difficulty_boundary=0` and empty `difficulties` bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetLastStateProof` message with `difficulties=[]` and `difficulty_boundary=U256::zero()`. All four input guards are bypassed, `difficulty_boundary_block_number` collapses to `start_block_number`, and `last_n_numbers` spans the entire chain. The server performs O(chain_length) database reads and MMR computations for a single tiny message, with no effective cap.

### Finding Description
The size guard at lines 201–205 checks `difficulties.len() + last_n_blocks * 2 <= 1000`. With `difficulties=[]` and `last_n_blocks=0`, this evaluates to `0`, passing trivially. The guard at lines 259–266 uses `.unwrap_or(false)`, which silently skips the boundary check when `difficulties` is empty. With `difficulty_boundary=U256::zero()`, `get_first_block_total_difficulty_is_not_less_than` returns `start_block_number` immediately (any total difficulty satisfies `>= 0`). The result is `last_n_numbers = (start_block_number..last_block_number)` — potentially hundreds of thousands of entries on mainnet — with no guard catching it.

### Impact Explanation
The server executes `complete_headers` over all N block numbers: N `get_ancestor` calls, N `get_block_ext` reads, N full block reads, and N MMR root computations. A single attacker-controlled peer can repeatedly trigger this to exhaust server CPU, memory, and I/O. On CKB mainnet (millions of blocks), this is a severe amplification DoS against any light-client-serving node.

### Likelihood Explanation
The attack requires only a valid `last_hash` on the main chain (trivially obtained via `GetLastState`) and a crafted `GetLastStateProof` message. No privilege, key, or hashpower is needed. The path is fully reachable from any P2P peer.

### Recommendation
1. Add an explicit lower bound check: reject `difficulty_boundary == U256::zero()`.
2. Fix the size guard to bound the actual response size: after computing `difficulty_boundary_block_number`, check `(last_block_number - difficulty_boundary_block_number) + sampled_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT` before proceeding.
3. Alternatively, enforce that `difficulty_boundary` must be strictly greater than the total difficulty of `start_block_number - 1`, ensuring it is a meaningful chain-anchored value.

### Proof of Concept
```rust
// On a chain mined to 10_000 blocks:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)           // valid tip on main chain
    .start_hash(genesis_hash)
    .start_number(0u64)
    .last_n_blocks(0u64)
    .difficulty_boundary(U256::zero().pack())  // zero boundary
    // difficulties left empty (default)
    .build();
// Server will call complete_headers with numbers [0..10_000],
// performing 10_000 DB reads + MMR computations.
// Assert: server either rejects or returns at most GET_LAST_STATE_PROOF_LIMIT headers.
// Actual result: server returns all 10_000 headers, violating the limit.
```

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L254-257)
```rust
            if difficulties.windows(2).any(|d| d[0] >= d[1]) {
                let errmsg = "the difficulties should be monotonically increasing";
                return StatusCode::InvalidRequest.with_context(errmsg);
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L268-289)
```rust
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
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-347)
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
