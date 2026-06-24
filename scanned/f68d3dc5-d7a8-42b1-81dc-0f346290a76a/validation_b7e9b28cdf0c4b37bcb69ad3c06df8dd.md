Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=U256::zero()` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The upfront guard at lines 201–205 only bounds `difficulties.len() + last_n_blocks * 2`, not the actual size of `last_n_numbers` that is computed later. When an attacker sends `difficulty_boundary=U256::zero()` and `difficulties=[]`, the binary-search helper immediately returns `start_block_number=0` as the boundary, causing `last_n_numbers = (0..last_block_number)` — the entire chain — to be passed to `complete_headers` with no size check. This allows a single unauthenticated peer to force O(N) disk reads and MMR root computations, where N is the full chain height.

## Finding Description

**Guard (lines 201–205):** [1](#0-0) 
With `difficulties=[]` and `last_n_blocks=10`, this evaluates to `0 + 20 = 20 < 1000`. The guard passes unconditionally.

**Short-circuit in `get_first_block_total_difficulty_is_not_less_than` (lines 30–32):** [2](#0-1) 
When `min_total_difficulty = U256::zero()`, the condition `start_total_difficulty >= 0` is always true for any block, so the function immediately returns `Some((start_block_number, ...))`. With `start_block_number=0`, this sets `difficulty_boundary_block_number = 0`.

**Adjustment skipped (lines 313–316):** [3](#0-2) 
`last_block_number - 0 = N >= last_n_blocks=10`, so the adjustment is not applied.

**Unbounded vector (lines 318–319):** [4](#0-3) 
`last_n_numbers = (0..last_block_number)` — a vector of N entries with no size check.

**`difficulty_boundary_block_number == 0` skips sampling (lines 321–347):** [5](#0-4) 
The `else` branch returns `(Vec::new(), last_n_numbers)`, so `sampled_numbers=[]` and the full N-entry `last_n_numbers` proceeds.

**`complete_headers` called with N entries (lines 132–163):** [6](#0-5) 
Each iteration performs `get_ancestor` (O(depth) traversal), `get_block` (disk I/O), and `chain_root_mmr(*number - 1).get_root()` (MMR computation). With N = 50,000+ blocks this is unbounded work per request.

**Constant provides no protection here:** [7](#0-6) 

## Impact Explanation
A single unauthenticated remote peer can send one crafted `GetLastStateProof` message and force the light-client protocol server to perform O(N) disk reads and MMR root computations, where N is the full chain height. On mainnet this exhausts CPU and I/O, stalling the light-client protocol server for all connected peers. This constitutes a **High** severity impact: **Vulnerabilities which could easily crash a CKB node** (10001–15000 points), as the light-client server is an integral component of the CKB node process and its resource exhaustion degrades or crashes the node.

## Likelihood Explanation
The attack requires no privileges, no proof-of-work, and no keys — only a valid P2P connection to the light-client protocol port. The crafted message is trivially constructable. The attack is repeatable: each new connection can re-trigger the same O(N) path. There are no existing mitigations that bound `last_n_numbers.len()` after it is computed.

## Recommendation
After line 319, add an explicit size check before proceeding:

```rust
if last_n_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage
        .with_context("too many last_n blocks");
}
```

Additionally, the upfront guard should be updated to account for the actual bounded range rather than the message-supplied `last_n_blocks` value, or `difficulty_boundary=0` should be rejected outright since it trivially collapses the boundary to the start of the chain.

## Proof of Concept
1. Spin up a CKB node with the light-client protocol enabled and a chain of N = 50,000 blocks.
2. Connect as a light-client peer and send:
   ```
   GetLastStateProof {
       last_hash:           <tip hash>,
       start_number:        0,
       start_hash:          <genesis hash>,
       last_n_blocks:       10,
       difficulty_boundary: U256::zero(),
       difficulties:        [],
   }
   ```
3. Observe that `complete_headers` is invoked with a 50,000-entry slice and that wall-clock time scales linearly with N, while a request with a valid non-zero `difficulty_boundary` completes in bounded constant time proportional to `last_n_blocks`.
4. Send the request repeatedly from a single peer to confirm resource exhaustion and stalling of the light-client server for other peers.

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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
