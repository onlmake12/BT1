The code confirms all claims in the report. Let me trace the exact path:

**Guard (L201–205):** With `difficulties=[]` and `last_n_blocks=10`, evaluates to `0 + 20 = 20 < 1000` — passes. [1](#0-0) 

**Validation checks (L259–266):** With `difficulties=[]`, `difficulties.last()` returns `None`, so `.unwrap_or(false)` returns `false` — the boundary check is skipped entirely. [2](#0-1) 

**Short-circuit (L30–32):** With `min_total_difficulty = U256::zero()` and `start_block_number=0`, `get_block_total_difficulty(0)` returns the genesis total difficulty (always > 0), and `genesis_difficulty >= U256::zero()` is unconditionally true — returns `Some((0, ...))` immediately. [3](#0-2) 

**Adjustment skipped (L313–316):** With `difficulty_boundary_block_number=0` and large `last_block_number=N`, `N - 0 >= 10` is true, so the adjustment is not applied. [4](#0-3) 

**Unbounded vector (L318–319):** `last_n_numbers = (0..N)` — N entries, no size check. [5](#0-4) 

**Else branch (L345–347):** `difficulty_boundary_block_number == 0` → returns `(Vec::new(), last_n_numbers)` with the full N-entry vector. [6](#0-5) 

**`complete_headers` with N entries (L132–163):** Each iteration calls `get_ancestor` (O(depth)), `get_block` (disk I/O), and `chain_root_mmr(*number - 1).get_root()` (MMR computation) — O(N) unbounded work per request. [7](#0-6) 

All claims are verified against the actual code. The vulnerability is valid.

---

Audit Report

## Title
Unbounded `last_n_numbers` via `difficulty_boundary=U256::zero()` Bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The upfront guard at lines 201–205 bounds only `difficulties.len() + last_n_blocks * 2`, not the actual size of `last_n_numbers` computed later. When an attacker sends `difficulty_boundary=U256::zero()` with `difficulties=[]`, the binary-search helper short-circuits at the genesis block and returns `difficulty_boundary_block_number=0`, causing `last_n_numbers = (0..last_block_number)` — the entire chain — to be passed to `complete_headers` with no size check. This allows a single unauthenticated peer to force O(N) disk reads and MMR root computations per request, where N is the full chain height.

## Finding Description
**Root cause:** The guard at L201–205 uses the message-supplied `last_n_blocks` field to bound work, but the actual work is determined by `last_n_numbers.len()`, which is computed from `difficulty_boundary_block_number` and is never independently bounded.

**Exploit path:**
1. Attacker sends `GetLastStateProof { start_number: 0, difficulty_boundary: U256::zero(), difficulties: [], last_n_blocks: 10 }`.
2. Guard (L201–205): `0 + 20 = 20 < 1000` — passes.
3. Difficulty validation (L259–266): `difficulties=[]` → `unwrap_or(false)` → skipped.
4. `last_block_number - start_block_number = N > 10` → enters the `else` branch at L298.
5. `get_first_block_total_difficulty_is_not_less_than(0, N, &U256::zero())` (L299–304): genesis total difficulty ≥ 0 is always true → immediately returns `Some((0, ...))` → `difficulty_boundary_block_number = 0`.
6. Adjustment (L313–316): `N - 0 >= 10` → not applied.
7. `last_n_numbers = (0..N)` (L318–319): N entries, no size check.
8. `difficulty_boundary_block_number == 0` → else branch (L345–347): returns `(Vec::new(), last_n_numbers)`.
9. `complete_headers` (L132–163) iterates all N entries, each performing `get_ancestor` (O(depth) traversal), `get_block` (disk I/O), and `chain_root_mmr(*number - 1).get_root()` (MMR computation).

**Why existing checks fail:** The guard at L201–205 is structurally insufficient — it bounds a message field (`last_n_blocks`), not the derived computation size. The difficulty validation at L259–266 is bypassed by empty `difficulties`. There is no post-computation size check on `last_n_numbers`.

## Impact Explanation
A single unauthenticated remote peer can force O(N) disk reads and MMR root computations per `GetLastStateProof` message, where N is the full chain height (50,000+ blocks on mainnet). Repeated requests exhaust CPU and I/O, stalling the light-client protocol server for all connected peers. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, as the light-client server runs within the CKB node process and its resource exhaustion degrades or crashes the node.

## Likelihood Explanation
The attack requires no privileges, no proof-of-work, and no keys — only a valid P2P connection to the light-client protocol port. The crafted message is trivially constructable. The attack is repeatable: each new connection or message re-triggers the same O(N) path. No existing mitigation bounds `last_n_numbers.len()` after it is computed.

## Recommendation
After line 319, add an explicit size check before proceeding:

```rust
if last_n_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage
        .with_context("too many last_n blocks");
}
```

Additionally, either reject `difficulty_boundary=U256::zero()` outright (since it trivially collapses the boundary to the chain start), or update the upfront guard to account for the actual bounded range rather than the message-supplied `last_n_blocks` value.

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
