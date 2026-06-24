Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=0` bypasses `GET_LAST_STATE_PROOF_LIMIT` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The early-exit guard at lines 201–205 checks only `difficulties.len() + last_n_blocks * 2`, which is trivially small. When an attacker sends `difficulty_boundary=U256::zero()` and `difficulties=[]`, the binary-search helper immediately returns `start_block_number` (since any total difficulty satisfies `>= 0`), causing `last_n_numbers` to span the entire chain. `complete_headers` then performs O(N) MMR root computations and block lookups for every block — completely unbounded by the 1000-entry limit.

## Finding Description

**Step 1 — Limit check passes trivially.** [1](#0-0) 
With `difficulties=[]` and `last_n_blocks=10`, the expression evaluates to `0 + 20 = 20`, well under `GET_LAST_STATE_PROOF_LIMIT = 1000`. [2](#0-1) 

**Step 2 — Short-circuit branch is not taken.** [3](#0-2) 
With `start_block_number=0` and `last_block_number=50000`, `50000 - 0 <= 10` is false; execution falls into the else branch.

**Step 3 — `difficulty_boundary=0` forces `difficulty_boundary_block_number = start_block_number`.** [4](#0-3) 
`get_first_block_total_difficulty_is_not_less_than` is called with `min_total_difficulty = U256::zero()`. Since `start_total_difficulty >= U256::zero()` is always true for any valid block, the function immediately returns `Some((start_block_number, ...))` — i.e., block 0. Thus `difficulty_boundary_block_number = 0`. [5](#0-4) 

**Step 4 — The adjustment guard is skipped.** [6](#0-5) 
`50000 - 0 < 10` is false, so `difficulty_boundary_block_number` is not clamped.

**Step 5 — `last_n_numbers` collects the entire chain.** [7](#0-6) 
`(0..50000).collect()` produces 50,000 entries. Additionally, since `difficulty_boundary_block_number == 0`, the `if difficulty_boundary_block_number > 0` branch at line 321 is skipped, so `sampled_numbers = Vec::new()` and the full `last_n_numbers` is used. [8](#0-7) 

**Step 6 — `complete_headers` performs O(N) MMR root computations.** [9](#0-8) 
For every one of the N entries, `get_ancestor`, `get_block`, and `chain_root_mmr(*number - 1).get_root()` are called. This is unbounded I/O and CPU work proportional to chain length. There is no post-computation length check before `complete_headers` is invoked. [10](#0-9) 

**All input validation checks pass with `difficulties=[]` and `difficulty_boundary=0`:** [11](#0-10) 
- Sorted-difficulties check: empty iterator → no error.
- Max-difficulty-less-than-boundary check: `None.unwrap_or(false)` → no error.
- First-difficulty-greater-than-start check: `difficulties.first()` is `None` → no match → no error.

## Impact Explanation
A single unprivileged light-client peer can send one crafted `GetLastStateProof` message to force the server to iterate and compute MMR roots for every block in the chain. On a mainnet-length chain (hundreds of thousands of blocks), this exhausts server CPU and storage I/O. Multiple concurrent peers amplify the effect. This matches the **High** impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"* and *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points). [12](#0-11) 

## Likelihood Explanation
The attack requires no credentials, no PoW, and no special state — only a valid `last_hash` pointing to a main-chain tip (publicly observable on-chain). The malformed field values (`difficulty_boundary=0`, `difficulties=[]`) pass all existing validation checks. Any peer connected to the light-client protocol server can trigger this repeatedly at negligible cost. [13](#0-12) 

## Recommendation
After computing `last_n_numbers` (and `sampled_numbers`), add an explicit length check before calling `complete_headers`:

```rust
if last_n_numbers.len() + sampled_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, reject requests where `difficulty_boundary` is zero or less than the total difficulty of `start_block_number`, since a zero boundary is semantically meaningless and only serves to collapse the sampling range. [14](#0-13) 

## Proof of Concept

```
MockChain: 50,000 blocks
Message: GetLastStateProof {
    last_hash:           tip_hash,
    start_number:        0,
    start_hash:          genesis_hash,
    last_n_blocks:       10,
    difficulty_boundary: U256::zero(),
    difficulties:        [],
}
```

Expected (buggy) behavior: `complete_headers` is invoked with a 50,000-entry `block_numbers` slice; execution time and I/O grow linearly with chain length. The limit check at lines 201–205 passes with a value of 20, never reaching 1000. A unit test can assert that `execute` returns before calling `complete_headers` with more than `GET_LAST_STATE_PROOF_LIMIT` entries, which it currently does not. [1](#0-0)

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-205)
```rust
    pub(crate) async fn execute(self) -> Status {
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L209-215)
```rust
        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendLastStateProof>(self.peer, self.nc)
                .await;
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L252-289)
```rust
        {
            // The difficulties should be sorted.
            if difficulties.windows(2).any(|d| d[0] >= d[1]) {
                let errmsg = "the difficulties should be monotonically increasing";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The maximum difficulty should be less than the difficulty boundary.
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
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-366)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();

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
