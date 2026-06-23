### Title
Unbounded `last_n_numbers` allocation bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The upfront limit check in `GetLastStateProofProcess::execute` is structurally unsound. It guards against `difficulties.len() + last_n_blocks * 2 > GET_LAST_STATE_PROOF_LIMIT`, implicitly assuming `last_n_numbers.len() <= last_n_blocks`. That assumption is only guaranteed when the adjustment at line 313–316 fires. When `difficulty_boundary_block_number` resolves to a block near the start of the chain (because the attacker supplies a near-zero `difficulty_boundary`), the adjustment is **not** triggered and `last_n_numbers` grows to `last_block_number − difficulty_boundary_block_number`, which is bounded only by chain height — not by `last_n_blocks`.

---

### Finding Description

**Limit check (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

The check budgets `last_n_blocks` slots for `last_n_numbers`. [1](#0-0) 

**`last_n_numbers` construction (lines 318–319):**

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

Its length is `last_block_number − difficulty_boundary_block_number`, not `last_n_blocks`. [2](#0-1) 

**The adjustment guard (lines 313–316):**

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

This only clamps `difficulty_boundary_block_number` **upward** when there are *too few* trailing blocks. When the attacker supplies a tiny `difficulty_boundary`, the sampler resolves `difficulty_boundary_block_number` to block 0 or 1, giving `last_block_number − 0 >> last_n_blocks`, so the guard never fires. [3](#0-2) 

**`difficulty_boundary` is not lower-bounded.** When `difficulties = []`, the check at lines 259–266 evaluates `None.unwrap_or(false)` and is skipped entirely, so any `difficulty_boundary` value (including 1) is accepted. [4](#0-3) 

---

### Impact Explanation

After `block_numbers` is assembled at lines 350–354, `complete_headers` is called for every entry. Each iteration performs:
- `snapshot.get_ancestor(last_hash, number)` — traverses the chain backwards from the tip to `number`
- `snapshot.get_block(...)` — random DB read
- `snapshot.chain_root_mmr(number - 1).get_root()` — MMR recomputation [5](#0-4) 

For a chain of height N with `last_n_numbers` containing N entries, `get_ancestor` alone is O(N) per call, making the total O(N²). On a mainnet node with millions of blocks, a single crafted P2P message causes unbounded CPU and memory consumption, effectively hanging or crashing the light-client server thread.

---

### Likelihood Explanation

The attack requires only a valid `last_hash` on the main chain (publicly observable from any synced node) and a `difficulty_boundary` of 1. No PoW, no key, no privileged role. Any peer that can send a `LightClientMessage::GetLastStateProof` packet can trigger this. [6](#0-5) 

---

### Recommendation

After computing `last_n_numbers`, enforce the invariant before proceeding:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();

// Hard cap: last_n_numbers must not exceed last_n_blocks entries.
if last_n_numbers.len() > last_n_blocks as usize {
    return StatusCode::MalformedProtocolMessage
        .with_context("last_n_numbers exceeds last_n_blocks");
}
```

Alternatively, clamp `difficulty_boundary_block_number` from below as well:

```rust
let lower_bound = last_block_number.saturating_sub(last_n_blocks);
if difficulty_boundary_block_number < lower_bound {
    difficulty_boundary_block_number = lower_bound;
}
```

And add a post-condition assertion before `complete_headers`:

```rust
assert!(block_numbers.len() <= constant::GET_LAST_STATE_PROOF_LIMIT);
```

---

### Proof of Concept

**Setup:** A CKB node with chain height N = 1,000,000.

**Crafted message fields:**
| Field | Value |
|---|---|
| `last_hash` | tip block hash (public) |
| `start_hash` | genesis hash |
| `start_number` | 0 |
| `last_n_blocks` | 1 |
| `difficulty_boundary` | U256::from(1) |
| `difficulties` | `[]` |

**Trace through `execute`:**

1. Limit check: `0 + 1×2 = 2 ≤ 1000` → **passes**. [1](#0-0) 
2. `start_block_number = 0` → `reorg_last_n_numbers = []`. [7](#0-6) 
3. `last_block_number − start_block_number = 1,000,000 > 1` → enters else branch. [8](#0-7) 
4. `difficulty_boundary = 1` → `difficulty_boundary_block_number = 0` (genesis has total difficulty ≥ 1). [9](#0-8) 
5. `last_block_number − 0 = 1,000,000 ≥ 1` → adjustment **not triggered**. [3](#0-2) 
6. `last_n_numbers = (0..1,000,000)` → **1,000,000 entries**. [2](#0-1) 
7. `difficulty_boundary_block_number = 0` → `sampled_numbers = []`. [10](#0-9) 
8. `block_numbers.len() = 1,000,000 >> GET_LAST_STATE_PROOF_LIMIT (1000)`. [11](#0-10) 
9. `complete_headers` iterates 1,000,000 times with O(N) `get_ancestor` per call → O(N²) total work → node hangs/OOM. [12](#0-11) 

---

**Note on the question's specific scenario:** The reorg-path variant described in the prompt (adjustment fires, `sampled_numbers` overflows) does not work because `sampled_numbers.len() ≤ difficulties.len()` always holds, so `2×last_n_blocks + sampled_numbers.len() ≤ 2×last_n_blocks + difficulties.len()` — exactly the limit-check bound. The real exploit is the non-adjustment path shown above, where `last_n_numbers` itself is unbounded.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-243)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L345-347)
```rust
            } else {
                (Vec::new(), last_n_numbers)
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-354)
```rust
        let block_numbers = reorg_last_n_numbers
            .into_iter()
            .chain(sampled_numbers)
            .chain(last_n_numbers)
            .collect::<Vec<_>>();
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

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```
