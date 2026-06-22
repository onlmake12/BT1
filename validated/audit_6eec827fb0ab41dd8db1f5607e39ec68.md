I have all the information needed. Let me trace the exact code path.

### Title
Unbounded `last_n_numbers` Vec Allocation via Attacker-Controlled `difficulty_boundary` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

An unprivileged remote peer can send a `GetLastStateProof` message with `difficulty_boundary = 1` (or any value ≤ genesis total difficulty), causing `GetLastStateProofProcess::execute` to allocate a `last_n_numbers` Vec proportional to the entire chain height — completely bypassing the `GET_LAST_STATE_PROOF_LIMIT` guard — and then exhausting server memory and CPU via `complete_headers`.

### Finding Description

The guard in `execute` at lines 201–205 checks:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT  // 1000
``` [1](#0-0) 

With `difficulties = []` and `last_n_blocks = 1`, this evaluates to `0 + 2 = 2 ≤ 1000` and passes. The guard bounds `last_n_blocks` in the message, but it does **not** bound the size of the `last_n_numbers` Vec that is actually allocated later.

When `last_block_number - start_block_number > last_n_blocks` (the `else` branch at line 298), the code calls:

```rust
sampler.get_first_block_total_difficulty_is_not_less_than(
    start_block_number,   // 0
    last_block_number,    // N (tip)
    &difficulty_boundary, // U256::one()
)
``` [2](#0-1) 

Inside `get_first_block_total_difficulty_is_not_less_than`, the very first check is:

```rust
if start_total_difficulty >= *min_total_difficulty {
    return Some((start_block_number, start_total_difficulty));
}
``` [3](#0-2) 

The genesis block's total difficulty is always ≥ 1 on any real chain, so this returns `Some((0, genesis_td))` immediately, setting `difficulty_boundary_block_number = 0`.

The adjustment guard at line 313 only fires when there are **too few** blocks after the boundary:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
``` [4](#0-3) 

With `difficulty_boundary_block_number = 0` and `last_block_number = N` (millions), the condition `N < 1` is false. No adjustment is made. The allocation then proceeds:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (0..N).collect() — N elements
``` [5](#0-4) 

This Vec is then passed to `complete_headers`, which performs `get_ancestor` (O(log N) skip-list traversal) and constructs a `VerifiableHeader` (including MMR root computation) for **every single block number** in the range. [6](#0-5) 

### Impact Explanation

- **Memory**: `(0..N).collect::<Vec<u64>>()` for N = 10,000,000 blocks = ~80 MB for the index Vec alone. The subsequent `Vec<packed::VerifiableHeader>` built in `complete_headers` is far larger (each header includes block data, uncles hash, extension, and MMR root).
- **CPU**: `get_ancestor` is O(log N) per call; N calls = O(N log N) total work, blocking the async task.
- **Effect**: Node OOM crash or sustained CPU exhaustion, rendering the light-client server unresponsive. A single malicious peer can trigger this repeatedly.

### Likelihood Explanation

The P2P light-client protocol is open to any peer. No authentication, PoW, or stake is required to send `GetLastStateProof`. The message is trivially constructable. The exploit requires only that the node is synced to a height significantly greater than `last_n_blocks` (which is always true on mainnet after the first few blocks). The attacker needs no special knowledge beyond the tip block hash (which is publicly broadcast via `SendLastState`).

### Recommendation

After resolving `difficulty_boundary_block_number`, unconditionally cap it so that `last_n_numbers` cannot exceed `last_n_blocks`:

```rust
// Existing: only expands the window when too small
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
// ADD: also shrink the window when too large
if last_block_number - difficulty_boundary_block_number > last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

Or equivalently, replace both with a single clamp. Additionally, the guard at lines 201–205 should be restructured to bound the **effective** `last_n_numbers` size, not just the raw `last_n_blocks` field.

### Proof of Concept

```
Attacker sends GetLastStateProof {
    last_hash:          <current tip hash, obtained from SendLastState>,
    start_hash:         <genesis hash>,
    start_number:       0,
    last_n_blocks:      1,
    difficulty_boundary: 0x0000...0001,  // U256::one()
    difficulties:       [],
}
```

Execution trace:
1. Guard: `0 + 1*2 = 2 ≤ 1000` → **passes**
2. `last_block_number - 0 = N > 1` → enters `else` branch
3. `get_first_block_total_difficulty_is_not_less_than(0, N, 1)` → genesis_td ≥ 1 → returns `Some((0, genesis_td))`
4. `difficulty_boundary_block_number = 0`
5. `N - 0 < 1` → **false**, no adjustment
6. `(0..N).collect::<Vec<_>>()` → **N-element Vec allocated**
7. `complete_headers` loops N times → **OOM / CPU exhaustion** [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L124-180)
```rust
    fn complete_headers(
        &self,
        positions: &mut Vec<u64>,
        last_hash: &packed::Byte32,
        numbers: &[BlockNumber],
    ) -> Result<Vec<packed::VerifiableHeader>, String> {
        let mut headers = Vec::new();

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

        Ok(headers)
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
