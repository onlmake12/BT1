Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=U256::zero()` bypasses size guard, enabling server-side DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The size guard in `GetLastStateProofProcess::execute` checks `last_n_blocks` from the attacker-controlled message, not the actual number of blocks that will be fetched. Sending `difficulty_boundary=U256::zero()` with an empty `difficulties` array causes `get_first_block_total_difficulty_is_not_less_than` to return `start_block_number=0` immediately (any U256 ≥ 0), setting `difficulty_boundary_block_number=0` and making `last_n_numbers` span the entire chain. `complete_headers` is then called for every block, performing O(chain-length) disk I/O and MMR computation per request.

## Finding Description

**Size guard (lines 201–205):** The guard checks:
```rust
self.message.difficulties().len() + (last_n_blocks as usize) * 2 > GET_LAST_STATE_PROOF_LIMIT
```
With `difficulties=[]` and `last_n_blocks=1`, this evaluates to `0 + 2 = 2 ≤ 1000`. Passes. [1](#0-0) 

**Empty-array guard (lines 259–266):** With empty `difficulties`, `.last()` returns `None`, `.unwrap_or(false)` returns `false`. Guard does not fire. [2](#0-1) 

**`else` branch taken (line 291):** With `start_block_number=0`, `last_block_number=N`, `last_n_blocks=1`: `N - 0 > 1`, so the `else` branch executes. [3](#0-2) 

**`get_first_block_total_difficulty_is_not_less_than` with `min_total_difficulty=U256::zero()` (lines 30–32):** Any U256 total difficulty satisfies `>= 0`, so the function returns `Some((start_block_number, ...))` immediately, setting `difficulty_boundary_block_number = 0`. [4](#0-3) 

**Adjustment check does not fire (line 313):** With `difficulty_boundary_block_number=0` and `last_n_blocks=1`: `N - 0 < 1` is false for any non-empty chain, so `difficulty_boundary_block_number` remains 0. [5](#0-4) 

**`last_n_numbers` spans entire chain (lines 318–319):** With `difficulty_boundary_block_number=0`:
```rust
let last_n_numbers = (0..last_block_number).collect::<Vec<_>>();
``` [6](#0-5) 

**`sampled_numbers` is empty (lines 345–346):** Since `difficulty_boundary_block_number == 0`, the `else` branch fires: `(Vec::new(), last_n_numbers)`. [7](#0-6) 

**`complete_headers` called for all N blocks (lines 356–366):** For each block: `get_ancestor`, `get_block`, and `chain_root_mmr` — O(N) disk I/O and MMR computation. [8](#0-7) 

The `GET_LAST_STATE_PROOF_LIMIT = 1000` constant provides no protection because it is checked against `last_n_blocks` from the message (value: 1), not against the actual computed `last_n_numbers.len()` (value: N). [9](#0-8) 

## Impact Explanation
A single malicious peer can trigger O(chain-length) disk reads, memory allocation, and MMR root computations per request. On mainnet with millions of blocks, repeated requests exhaust server memory and CPU, crashing the CKB node. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
The attack requires no privileges, no keys, and no hashpower. Any peer that can establish a P2P connection and send a `GetLastStateProof` message can trigger it. The crafted message is trivially small (empty difficulties, `last_n_blocks=1`, `difficulty_boundary=U256::zero()`). The attack is repeatable and can be sustained by a single attacker.

## Recommendation
After computing `last_n_numbers` and `sampled_numbers`, add a post-computation size guard:
```rust
if sampled_numbers.len() + last_n_numbers.len() + reorg_last_n_numbers.len()
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many blocks");
}
```
Additionally, explicitly reject `difficulty_boundary=U256::zero()` at the start of `execute`, since it is semantically meaningless as a FlyClient threshold and the current code does not handle it safely.

## Proof of Concept
Send a `GetLastStateProof` P2P message with:
- `last_hash` = current tip hash (on main chain)
- `start_hash` = genesis hash, `start_number = 0`
- `difficulty_boundary = U256::zero()`
- `difficulties = []`
- `last_n_blocks = 1`

Observe that `execute()` calls `complete_headers` for every block from 0 to tip. The size check passes with value `2`. On a node with N blocks, this performs N calls to `get_ancestor`, `get_block`, and `chain_root_mmr`, exhausting memory and CPU proportional to chain length.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-32)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L345-346)
```rust
            } else {
                (Vec::new(), last_n_numbers)
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
