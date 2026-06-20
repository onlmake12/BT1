Let me read the key lines more carefully to confirm the exact flow.

### Title
Unbounded `last_n_numbers` Vec allocation via attacker-controlled `difficulty_boundary` resolving to genesis — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

`GetLastStateProofProcess::execute` enforces a size limit using `last_n_blocks` as a proxy, but the actual `last_n_numbers` Vec is sized by `last_block_number - difficulty_boundary_block_number`. An unprivileged peer can set `difficulty_boundary` to a value that resolves to block 0, causing the server to allocate a Vec containing every block number from 0 to the chain tip — potentially millions of entries on a long-running mainnet node.

---

### Finding Description

The limit guard at lines 201–205 checks:

```
difficulties.len() + (last_n_blocks as usize) * 2 > GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

With `difficulties=[1 entry]` and `last_n_blocks=499`, this evaluates to `1 + 998 = 999 < 1000` and passes.

The actual `last_n_numbers` Vec is built at lines 318–319:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
``` [2](#0-1) 

Its size is `last_block_number - difficulty_boundary_block_number`, which is entirely independent of `last_n_blocks`.

The only adjustment to `difficulty_boundary_block_number` (lines 313–316) only raises it when there are **too few** blocks after the boundary — it never lowers it when there are **too many**:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
``` [3](#0-2) 

**Concrete attack path:**

1. Set `start_block_number = 0`, `last_n_blocks = 499`, `difficulties = [X]` where `X < genesis_total_difficulty`, `difficulty_boundary = genesis_total_difficulty`, `last_hash = current tip hash`.
2. Limit check: `1 + 499*2 = 999 < 1000` → passes. [1](#0-0) 
3. The `start_block_number > 0` guard at line 269 is skipped, so no difficulty-vs-start-block validation fires. [4](#0-3) 
4. `last_block_number - start_block_number = N >> 499` → enters the else branch. [5](#0-4) 
5. `get_first_block_total_difficulty_is_not_less_than(0, N, &genesis_total_difficulty)` returns `Some((0, genesis_total_difficulty))` because block 0's total difficulty equals `difficulty_boundary`. [6](#0-5) 
6. `difficulty_boundary_block_number = 0`; the adjustment check `N - 0 < 499` is false → no correction. [3](#0-2) 
7. `last_n_numbers = (0..N).collect::<Vec<_>>()` — N entries allocated. [2](#0-1) 
8. Because `difficulty_boundary_block_number == 0`, the code takes the `else` branch at line 345–346 and returns this unbounded Vec directly. [7](#0-6) 
9. `complete_headers` is then called with N block numbers, performing N DB lookups and N MMR root computations. [8](#0-7) 

The constant `GET_LAST_STATE_PROOF_LIMIT = 1000` provides no protection here. [9](#0-8) 

---

### Impact Explanation

On a mainnet node at height 10,000,000, a single crafted message causes:
- Allocation of a `Vec<u64>` with ~10M entries (~80 MB per request)
- ~10M sequential DB reads and MMR root computations in `complete_headers`
- Repeated requests from one or more peers can exhaust memory (OOM crash) or saturate I/O, causing a local denial of service

---

### Likelihood Explanation

Any unauthenticated peer on the light-client P2P protocol can send this message. No PoW, no key, no special role required. The crafted values (`last_n_blocks=499`, one difficulty entry, `difficulty_boundary=genesis_total_difficulty`) are trivially constructable. The node processes the message synchronously before any further bound check.

---

### Recommendation

After resolving `difficulty_boundary_block_number`, clamp `last_n_numbers` to at most `last_n_blocks` entries:

```rust
// After the existing adjustment at line 313-316, add:
let effective_start = last_block_number.saturating_sub(last_n_blocks);
if difficulty_boundary_block_number < effective_start {
    difficulty_boundary_block_number = effective_start;
}
```

Alternatively, assert `last_block_number - difficulty_boundary_block_number <= last_n_blocks` and reject the request if violated, since the client controls `last_n_blocks` and is responsible for providing a `difficulty_boundary` consistent with it.

---

### Proof of Concept

```rust
// Pseudocode unit test
let chain_height = 10_000_000u64;
let genesis_total_difficulty = /* chain's actual genesis difficulty */;
let msg = GetLastStateProof {
    last_n_blocks: 499,
    difficulties: vec![genesis_total_difficulty - 1],  // 1 entry, < boundary
    difficulty_boundary: genesis_total_difficulty,      // resolves to block 0
    start_number: 0,
    start_hash: genesis_hash,
    last_hash: tip_hash,
};
// After execute(), last_n_numbers.len() == 10_000_000
// Assert: last_n_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT  ← FAILS
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L29-33)
```rust
    ) -> Option<(BlockNumber, U256)> {
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L268-270)
```rust
            if let Some(start_difficulty) = difficulties.first()
                && start_block_number > 0
            {
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
