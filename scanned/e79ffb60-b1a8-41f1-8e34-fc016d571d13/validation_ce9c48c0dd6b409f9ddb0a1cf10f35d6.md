Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=0` + `difficulties=[]` Bypasses Server-Side Size Guard — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The `GetLastStateProofProcess::execute` handler in the light-client protocol server contains a size guard that only counts request-parameter entries, not the actual number of blocks that will be fetched. An unauthenticated peer can send a crafted `GetLastStateProof` message with `difficulties=[]`, `difficulty_boundary=U256::zero()`, and `last_n_blocks=0` to force the server to iterate, fetch, and serialize every block in the chain with no secondary cap, exhausting CPU, memory, and I/O.

## Finding Description

**Size guard is bypassed.**
The only upfront limit is: [1](#0-0) 

With `difficulties=[]` (len = 0) and `last_n_blocks = 0`, the expression evaluates to `0 + 0 = 0`, which is not `> 1000`. The check passes unconditionally.

**All per-field validation checks pass on empty difficulties.** [2](#0-1) 

- `difficulties.windows(2).any(...)` — empty slice, `any()` returns `false`. No error.
- `difficulties.last().map(...).unwrap_or(false)` — `last()` is `None`, `unwrap_or(false)` is `false`. Boundary check skipped.
- `difficulties.first()` — `None`, so the `if let Some(start_difficulty)` block is skipped entirely.

**Zero boundary causes `difficulty_boundary_block_number = start_block_number`.**

The code enters the `else` branch when `last_block_number - start_block_number > last_n_blocks (= 0)`, which is true for any non-empty chain: [3](#0-2) 

`get_first_block_total_difficulty_is_not_less_than` is called with `min_total_difficulty = U256::zero()`. Its first check: [4](#0-3) 

Since `U256` is unsigned, `start_total_difficulty >= U256::zero()` is always true. The function returns `start_block_number` immediately, so `difficulty_boundary_block_number = start_block_number`.

**`last_n_numbers` covers the entire chain.**

The adjustment guard `last_block_number - difficulty_boundary_block_number < last_n_blocks` becomes `last_block_number < 0`, which is always false for `u64`. So no adjustment occurs, and: [5](#0-4) 

`last_n_numbers` is `(start_block_number..last_block_number)` — every block in the chain.

**`complete_headers` iterates over all of them with no cap.**

For each block number in `block_numbers`, the server performs three expensive operations with no secondary size check: [6](#0-5) 

- `snapshot.get_ancestor(last_hash, *number)` — O(N) chain traversal per call
- `snapshot.get_block(...)` — full block DB fetch
- `snapshot.chain_root_mmr(*number - 1).get_root()` — MMR root computation

The final `block_numbers` vector is assembled and passed to `complete_headers` with no cap: [7](#0-6) 

## Impact Explanation

A single unauthenticated peer can force the light-client server to perform O(N) database lookups, O(N) MMR root computations, and O(N) memory allocation for every block in the chain. On CKB mainnet (millions of blocks), this exhausts CPU, memory, and I/O on the server process. Multiple concurrent requests from different peer connections amplify the effect. This matches the **High** impact category: *Vulnerabilities which could easily crash a CKB node* and *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

The attack requires no credentials, no proof-of-work, and no prior state. The attacker only needs a valid tip hash, which is publicly observable from any chain explorer or peer. The crafted message is minimal (empty difficulties array, zero boundary scalar, zero `last_n_blocks`). The attack is repeatable from any number of peer connections simultaneously, and the server has no rate-limiting or per-peer request throttling visible in this code path.

## Recommendation

1. Add a secondary size check on the computed `last_n_numbers` (and `block_numbers`) before calling `complete_headers`, capping it at `GET_LAST_STATE_PROOF_LIMIT`:
   ```rust
   if last_n_numbers.len() + sampled_numbers.len() + reorg_last_n_numbers.len()
       > constant::GET_LAST_STATE_PROOF_LIMIT
   {
       return StatusCode::InvalidRequest.with_context("too many blocks in response");
   }
   ```
2. Reject `difficulty_boundary == U256::zero()` explicitly when `last_block_number - start_block_number > last_n_blocks`, since a zero boundary is semantically meaningless and enables this bypass.
3. Consider also capping `reorg_last_n_numbers` independently, as it is also derived from `last_n_blocks` without a direct size guard.

## Proof of Concept

```
Setup: CKB node with light-client server enabled, chain height N (e.g., 10,000 blocks).

Craft GetLastStateProof message:
  last_hash           = current tip hash (publicly known)
  start_hash          = genesis hash
  start_number        = 0
  last_n_blocks       = 0
  difficulty_boundary = U256::zero() (32 zero bytes)
  difficulties        = [] (empty)

Send from any unauthenticated peer connection.

Expected: server rejects with InvalidRequest or returns at most last_n_blocks headers.
Actual:
  1. Size guard: 0 + 0*2 = 0 <= 1000 → passes.
  2. Difficulty validations: all skipped (empty slice).
  3. get_first_block_total_difficulty_is_not_less_than(0, N, 0) → returns block 0 immediately.
  4. difficulty_boundary_block_number = 0.
  5. last_n_numbers = (0..N) → N entries.
  6. complete_headers iterates all N blocks: N get_ancestor + N get_block + N MMR root calls.
  7. Server allocates N-entry VerifiableHeader Vec and attempts serialization/transmission.

Repeat from multiple connections to amplify resource exhaustion.
```

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L254-288)
```rust
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
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-311)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
        } else {
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
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
