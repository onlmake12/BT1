Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=U256::zero()` causes server-side DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
Any unprivileged P2P peer can send a `GetLastStateProof` message with `difficulty_boundary=U256::zero()`, `difficulties=[]`, and `last_n_blocks=1`. The size guard at line 201 checks only the attacker-supplied `last_n_blocks`, not the actual number of blocks that will be processed. With `difficulty_boundary=0`, `get_first_block_total_difficulty_is_not_less_than` immediately returns `start_block_number` (since any U256 ≥ 0), causing `last_n_numbers` to span the entire chain. `complete_headers` is then called for every block, performing O(chain-length) disk I/O and MMR computation per request.

## Finding Description

**Size guard (lines 201–205):** [1](#0-0) 
Checks `difficulties.len() + last_n_blocks * 2 > 1000`. With `difficulties=[]` and `last_n_blocks=1`, this evaluates to `2 > 1000` → false. Guard passes.

**Empty-array guard (lines 259–266):** [2](#0-1) 
`difficulties.last()` returns `None` for an empty slice; `.unwrap_or(false)` returns `false`. Guard does not fire.

**`else` branch entered (line 291):** [3](#0-2) 
With `start_block_number=0` and `last_block_number=N >> 1`, the condition `N <= 1` is false, so the `else` branch executes.

**`get_first_block_total_difficulty_is_not_less_than` with `min_total_difficulty=0` (lines 30–32):** [4](#0-3) 
Any U256 total difficulty satisfies `>= U256::zero()`, so the function immediately returns `Some((start_block_number, ...))` = `Some((0, ...))`. Thus `difficulty_boundary_block_number = 0`.

**Adjustment check skipped (line 313):** [5](#0-4) 
`N - 0 = N < 1` is false for any real chain. No adjustment.

**`last_n_numbers` spans entire chain (lines 318–319):** [6](#0-5) 
`(0..N).collect()` — all N blocks, not the attacker-supplied `last_n_blocks=1`.

**`sampled_numbers` is empty (lines 345–346):** [7](#0-6) 
`difficulty_boundary_block_number == 0`, so the `else` branch fires and `sampled_numbers = Vec::new()`.

**`complete_headers` called for all N blocks (lines 356–366):** [8](#0-7) 
For each of the N blocks: `get_ancestor`, `get_block`, and `chain_root_mmr` are called — O(N) disk I/O and MMR computation.

The root cause is that `GET_LAST_STATE_PROOF_LIMIT` is checked against the attacker-controlled `last_n_blocks` field, not against the actual size of `last_n_numbers` as computed. [9](#0-8) 

## Impact Explanation
A single malicious peer can trigger O(chain-length) disk reads, memory allocations, and MMR root computations per `GetLastStateProof` request. On mainnet with millions of blocks, repeated requests exhaust server memory and CPU, crashing the node. This matches: **High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attack requires no privileges, no keys, and no hashpower. Any peer that can establish a P2P connection and send a `GetLastStateProof` message can trigger it. The crafted message is trivially small (empty difficulties array, `last_n_blocks=1`, `difficulty_boundary=0`, any valid main-chain `last_hash`). The attack is repeatable and can be parallelized from multiple peers.

## Recommendation
After computing `last_n_numbers` and `sampled_numbers` (and before calling `complete_headers`), add a post-computation size guard:
```rust
if sampled_numbers.len() + last_n_numbers.len() + reorg_last_n_numbers.len()
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
}
```
Additionally, explicitly reject `difficulty_boundary=U256::zero()` at the start of `execute()`, since it is semantically meaningless as a FlyClient threshold and the only value that causes `get_first_block_total_difficulty_is_not_less_than` to return `start_block_number` unconditionally.

## Proof of Concept
Send a `GetLastStateProof` P2P message with:
- `last_hash` = any current main-chain block hash (e.g., tip)
- `start_hash` = genesis hash, `start_number = 0`
- `difficulty_boundary = U256::zero()`
- `difficulties = []`
- `last_n_blocks = 1`

Observe that `execute()` passes the size guard with value `2`, then calls `complete_headers` for every block from `0` to `last_block_number`, performing unbounded I/O proportional to chain length. A unit test can be written by constructing a mock chain of N blocks, sending the above message, and asserting that `complete_headers` is invoked N times rather than 1.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L345-347)
```rust
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
