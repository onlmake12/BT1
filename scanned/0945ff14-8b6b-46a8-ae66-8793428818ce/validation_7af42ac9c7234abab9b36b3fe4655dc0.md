The code is confirmed. All claims trace correctly against the actual source.

**Verification summary:**

- **Line 201–205**: Guard is `difficulties().len() + last_n_blocks * 2 > 1000`. With `last_n_blocks=0` and empty `difficulties`, evaluates to `0 > 1000` → false. Guard bypassed. [1](#0-0) 

- **Line 291–292**: Branch condition `last_block_number - start_block_number <= last_n_blocks`. With `last_n_blocks=0` and `start_block_number=0`, this is `H <= 0` — false for any non-empty chain. `else` branch always taken. [2](#0-1) 

- **Lines 299–319**: `get_first_block_total_difficulty_is_not_less_than` with `difficulty_boundary=U256::one()` returns block 0 (genesis total difficulty ≥ 1, confirmed by lines 30–32). Adjustment guard at line 313 (`H < 0`) never fires for `u64`. Result: `last_n_numbers = (0..H)` — H elements collected. [3](#0-2) 

- **Lines 132–163**: `complete_headers` performs 2 DB reads + uncle hash computation + MMR root computation per element — all O(H) with no cap. [4](#0-3) 

- **`GET_LAST_STATE_PROOF_LIMIT = 1000`** confirmed. [5](#0-4) 

---

Audit Report

## Title
Unbounded `last_n_numbers` Allocation via `last_n_blocks=0` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The per-request limit guard in `GetLastStateProofProcess::execute` only bounds `difficulties.len() + last_n_blocks * 2`, so a message with `last_n_blocks=0` and an empty `difficulties` array always passes the check with a score of 0. The subsequent `else` branch then constructs `last_n_numbers` as the range `(difficulty_boundary_block_number..last_block_number)`, which spans the entire chain height H with no cap. Every element in that range triggers two database reads, an uncle hash computation, and an MMR root computation, all synchronously per unauthenticated P2P message.

## Finding Description
**Limit guard (lines 201–205):** The expression `difficulties().len() + (last_n_blocks as usize) * 2` evaluates to `0` when `last_n_blocks=0` and `difficulties=[]`. This is never `> GET_LAST_STATE_PROOF_LIMIT (1000)`, so the guard is fully bypassed.

**Branch selection (lines 291–292):** The fast-path condition `last_block_number - start_block_number <= last_n_blocks` becomes `H <= 0` for any non-empty chain, which is always false. The `else` branch is unconditionally taken.

**Unbounded range construction (lines 299–319):** Inside the `else` branch, `get_first_block_total_difficulty_is_not_less_than` is called with `difficulty_boundary=U256::one()`. Because genesis block total difficulty ≥ 1, the binary search short-circuits at line 31–32 and returns block 0, setting `difficulty_boundary_block_number = 0`. The adjustment guard at line 313 checks `last_block_number - 0 < 0`, which is impossible for `u64`, so it never fires. The result is `last_n_numbers = (0..last_block_number).collect::<Vec<_>>()` — a vector of H elements.

**O(H) work in `complete_headers` (lines 132–163):** For each of the H elements, the function calls `snapshot.get_ancestor`, `snapshot.get_block`, `calc_uncles_hash`, and `snapshot.chain_root_mmr(n-1).get_root()`. There is no cap on the number of iterations.

## Impact Explanation
A single unauthenticated P2P message forces the server to allocate a `Vec<u64>` of H elements and perform H×2 database reads, H uncle hash computations, and H MMR root computations synchronously. On mainnet CKB (chain height ~14 million blocks), this is catastrophic per-message CPU and I/O amplification. Multiple concurrent connections sending this message would exhaust server resources and crash the node. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation
The attacker requires only: (1) a valid `last_hash` on the main chain, which is publicly broadcast via `SendLastState`; (2) a TCP connection to any node running the light-client-protocol-server; (3) no proof-of-work, no authentication, and no rate limiting in this code path. The malicious message is minimal (fixed-size fields, empty arrays). The attack is trivially repeatable and automatable.

## Recommendation
The limit check must bound the actual size of `last_n_numbers`, not just `last_n_blocks`. Concrete fixes:
1. **Reject `last_n_blocks=0`** unless `difficulties` is non-empty and provides a meaningful bound.
2. **Cap `last_n_numbers` after construction**: after computing `difficulty_boundary_block_number`, assert `last_block_number - difficulty_boundary_block_number <= GET_LAST_STATE_PROOF_LIMIT` and return `InvalidRequest` if exceeded.
3. **Fix the limit formula**: change the guard to also bound `last_block_number - start_block_number` against the limit, independent of `last_n_blocks`.

## Proof of Concept
```rust
// On a chain of height H=10000:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)                      // valid main-chain tip hash (publicly available)
    .start_hash(genesis_hash)
    .start_number(0u64.pack())                // start_block_number = 0
    .last_n_blocks(0u64.pack())               // last_n_blocks = 0 → guard: 0+0=0 ≤ 1000 ✓
    .difficulty_boundary(U256::one().pack())  // resolves to block 0 via binary search
    .difficulties(Default::default())         // empty
    .build();
// Result: last_n_numbers = (0..10000) → 10000 DB reads + MMR computations
// Invariant violated: last_n_numbers.len() (10000) ≤ last_n_blocks (0) is false
```
Send this message repeatedly from multiple connections to exhaust node I/O and CPU.

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-319)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }

            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
