### Title
Unbounded `last_n_numbers` allocation in `GetLastStateProofProcess::execute` bypasses `GET_LAST_STATE_PROOF_LIMIT` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit check in `execute()` only guards `difficulties.len() + last_n_blocks * 2`, but the `last_n_numbers` vector built in the `else` branch is bounded by `difficulty_boundary_block_number`, not by `last_n_blocks`. An attacker who sets `last_n_blocks=0`, `difficulties=[]`, and `difficulty_boundary=U256::from(1)` forces `difficulty_boundary_block_number` to collapse to `start_block_number`, making `last_n_numbers = (start_block_number..last_block_number)` — a range of arbitrary length. `complete_headers` then iterates over every entry, performing one `get_ancestor` + one `chain_root_mmr(n-1).get_root()` DB read per block and pushing a `VerifiableHeader` into a heap-allocated `Vec`, with no upper bound.

---

### Finding Description

**Limit check (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With `last_n_blocks=0` and `difficulties=[]` the expression evaluates to `0`, which passes. [1](#0-0) 

**Branch selection (lines 291–292):**

```rust
if last_block_number - start_block_number <= last_n_blocks  // 0 <= 0 only when equal
```

When `last_block_number > start_block_number` (chain tip far ahead), the `else` branch is taken. [2](#0-1) 

**`difficulty_boundary_block_number` resolution (lines 299–311):**

`get_first_block_total_difficulty_is_not_less_than(start_block_number, last_block_number, &U256::from(1))` immediately returns `Some((start_block_number, …))` because every block's cumulative difficulty is ≥ 1. So `difficulty_boundary_block_number = start_block_number`. [3](#0-2) 

**Unbounded `last_n_numbers` (lines 318–319):**

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (start_block_number..last_block_number) — millions of entries
``` [4](#0-3) 

**`complete_headers` loop (lines 132–177):**

For every entry in `block_numbers`, the function calls `snapshot.get_ancestor(last_hash, *number)`, fetches the full block, and calls `snapshot.chain_root_mmr(*number - 1).get_root()`, then pushes a `VerifiableHeader` onto the heap. With millions of entries this exhausts memory. [5](#0-4) 

---

### Impact Explanation

A single malicious P2P peer can crash the light-client-protocol-server process with OOM. On a chain of height H, the attack allocates O(H) `VerifiableHeader` objects and issues O(H) DB reads in one request. No authentication or PoW is required; any peer can open a connection and send the crafted message.

---

### Likelihood Explanation

CKB mainnet has been live since 2019 and its block height is well into the millions. The attack requires only a TCP connection to a node running the light-client protocol server and a single crafted `GetLastStateProof` message. It is trivially reproducible on any synced node.

---

### Recommendation

After computing `last_n_numbers` (and `reorg_last_n_numbers`, `sampled_numbers`), assert that the total count does not exceed `GET_LAST_STATE_PROOF_LIMIT` before calling `complete_headers`:

```rust
let block_numbers = reorg_last_n_numbers
    .into_iter()
    .chain(sampled_numbers)
    .chain(last_n_numbers)
    .collect::<Vec<_>>();

if block_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage
        .with_context("too many blocks to prove");
}
```

This ensures the invariant `|block_numbers| ≤ GET_LAST_STATE_PROOF_LIMIT` holds regardless of how `difficulty_boundary` is placed. [6](#0-5) 

---

### Proof of Concept

```
Preconditions:
  - Node is synced to chain height H >> 1 (e.g., H = 2_000_000)
  - Light-client protocol server is enabled

Craft GetLastStateProof:
  last_n_blocks       = 0
  difficulties        = []
  difficulty_boundary = U256::from(1)   // ≤ total_difficulty[start_block_number]
  start_number        = 1               // any low block
  start_hash          = hash(block #1)
  last_hash           = hash(tip block) // valid main-chain tip

Expected execution path:
  1. Limit check: 0 + 0*2 = 0 ≤ 1000  → passes
  2. is_main_chain(last_hash)           → true
  3. last_block_number - start_block_number = ~2M > 0 = last_n_blocks → else branch
  4. get_first_block_total_difficulty_is_not_less_than(1, 2M, 1)
       → returns Some((1, td[1]))       → difficulty_boundary_block_number = 1
  5. last_n_numbers = (1..2_000_000)   → Vec of ~2M entries
  6. complete_headers iterates 2M times → OOM crash
``` [7](#0-6) [8](#0-7)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-177)
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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();

                headers.push(header);
            } else {
                let errmsg = format!("failed to find ancestor header ({number})");
                return Err(errmsg);
            }
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-366)
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

            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }

            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();

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
        };

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
