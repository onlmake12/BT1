Audit Report

## Title
Unbounded DoS via `difficulty_boundary=U256::zero()` and empty `difficulties` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
An unprivileged remote peer can send a `GetLastStateProof` message with `difficulty_boundary = U256::zero()` and an empty `difficulties` array. The upfront size guard checks only the attacker-supplied `last_n_blocks` field, not the actual number of blocks the server will process. With a zero boundary, the server resolves `difficulty_boundary_block_number = start_block_number`, builds `last_n_numbers = (start_block_number..last_block_number)` spanning the entire chain, and calls `complete_headers` with millions of entries — performing one disk read and one MMR root computation per block — exhausting CPU and memory from a single P2P message.

## Finding Description

**Guard 1 (lines 201–205) — passes trivially:** [1](#0-0) 

With `difficulties.len() = 0` and `last_n_blocks = 1`: `0 + 1*2 = 2`, which does not exceed 1000. The check passes.

**Guard 2 (lines 259–266) — vacuously skipped for empty `difficulties`:** [2](#0-1) 

`difficulties.last()` returns `None`; `.unwrap_or(false)` returns `false`. The guard never fires, so no enforcement that `difficulty_boundary > max(difficulties)` occurs.

**Branch selection (lines 291–297) — `else` branch taken:** [3](#0-2) 

With `start_block_number = 0`, `last_n_blocks = 1`, and `last_block_number = tip` (millions): `tip - 0 <= 1` is false, so the `else` branch is taken.

**`get_first_block_total_difficulty_is_not_less_than` with `min_total_difficulty = U256::zero()` — always returns `start_block_number`:** [4](#0-3) 

Any block's total difficulty is `>= U256::zero()`, so the function immediately returns `Some((start_block_number, start_total_difficulty))`. Thus `difficulty_boundary_block_number = 0`.

**Adjustment check (lines 313–315) — does not fire:** [5](#0-4) 

`tip - 0 < 1` is false, so no correction to `difficulty_boundary_block_number` occurs.

**`last_n_numbers` spans the entire chain (lines 318–319):** [6](#0-5) 

`(0..tip_number)` — potentially tens of millions of entries.

**`difficulty_boundary_block_number == 0` short-circuits sampling (lines 321–347):** [7](#0-6) 

The `if difficulty_boundary_block_number > 0` branch is skipped; `sampled_numbers = Vec::new()` and the full `last_n_numbers` is returned.

**`complete_headers` performs O(N) database work per block (lines 132–177):** [8](#0-7) 

For each of the millions of entries: `snapshot.get_ancestor(...)`, `snapshot.get_block(...)`, and `snapshot.chain_root_mmr(*number - 1).get_root()` are called sequentially. This is unbounded CPU and memory consumption.

## Impact Explanation

A single crafted P2P message causes the server to allocate a `Vec<BlockNumber>` of up to `tip_number` entries (tens of millions on mainnet), then perform that many sequential disk reads and MMR root computations. This can exhaust server memory (OOM kill) and saturate CPU, causing a full node crash or sustained denial of service. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

Any peer that can connect to the light-client protocol port can send this message. No authentication, stake, or special role is required. The crafted message is trivial to construct and can be sent repeatedly. The attack is fully deterministic and reproducible.

## Recommendation

1. Explicitly reject requests where `difficulty_boundary == U256::zero()` before any processing.
2. After computing `last_n_numbers`, enforce `last_n_numbers.len() + sampled_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT` and return an error if exceeded.
3. Alternatively, cap `last_n_numbers` to at most `last_n_blocks` entries regardless of where `difficulty_boundary_block_number` lands.

## Proof of Concept

Send a `GetLastStateProof` message with:
- `last_n_blocks = 1`
- `start_number = 0`, `start_hash = genesis_hash`
- `last_hash = current_tip_hash` (any valid main-chain tip)
- `difficulty_boundary = U256::zero()`
- `difficulties = []`

Execution trace:
1. Size check: `0 + 1*2 = 2 <= 1000` → passes.
2. Empty-difficulties guard: `None.unwrap_or(false) = false` → skipped.
3. Branch: `tip - 0 > 1` → `else` branch taken.
4. `get_first_block_total_difficulty_is_not_less_than(0, tip, &U256::zero())` → `Some((0, genesis_diff))`.
5. `difficulty_boundary_block_number = 0`; adjustment check `tip < 1` → false, no correction.
6. `last_n_numbers = (0..tip)` — millions of entries.
7. `complete_headers` called with millions of block numbers → unbounded disk I/O, MMR computation, and memory allocation → node crash.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L321-347)
```rust
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
