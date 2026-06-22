Based on a careful trace through the production code, this is a real vulnerability.

## Code Trace

**Guard at lines 259–266 — vacuously passes for empty `difficulties`:**

```rust
if difficulties
    .last()
    .map(|d| *d >= difficulty_boundary)
    .unwrap_or(false)   // ← None.unwrap_or(false) = false → check skipped
```

With an empty `difficulties` vec, `difficulties.last()` is `None`, so `.unwrap_or(false)` returns `false` and the guard never fires. [1](#0-0) 

**`get_first_block_total_difficulty_is_not_less_than` with `min_total_difficulty = 0` — immediately returns `start_block_number`:**

```rust
if start_total_difficulty >= *min_total_difficulty {   // any_value >= 0 → always true
    return Some((start_block_number, start_total_difficulty));
}
``` [2](#0-1) 

So `difficulty_boundary_block_number` is set to `start_block_number`.

**Adjustment check at lines 313–315 — does NOT fire:**

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

We are in the `else` branch (line 298), which is only reached when `last_block_number - start_block_number > last_n_blocks`. Since `difficulty_boundary_block_number = start_block_number`, the condition `last_block_number - start_block_number < last_n_blocks` is false, so no adjustment occurs. [3](#0-2) 

**`last_n_numbers` spans the entire chain:**

```rust
let last_n_numbers = (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (start_block_number..last_block_number) — potentially millions of entries
``` [4](#0-3) 

**The upfront size check does NOT bound the actual response size:**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
```

This only checks the attacker-supplied `last_n_blocks` field (max ~500), not the actual number of blocks that will be fetched and returned. With `difficulties.len() = 0` and `last_n_blocks = 1`, the check passes, but the actual response contains `last_block_number - start_block_number` blocks. [5](#0-4) [6](#0-5) 

**`complete_headers` does O(N) database work per block:**

For each entry in `block_numbers`, the server calls `snapshot.get_ancestor(...)`, `snapshot.get_block(...)`, and `snapshot.chain_root_mmr(*number - 1).get_root()`. With millions of entries this is unbounded CPU and memory consumption triggered by a single P2P message. [7](#0-6) 

---

### Title
Unbounded DoS via `difficulty_boundary=0` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetLastStateProof` message with `difficulty_boundary = U256::zero()` and an empty `difficulties` array. The existing size guard checks only the attacker-supplied `last_n_blocks` field, not the actual number of blocks the server will process. With a zero boundary, the server sets `difficulty_boundary_block_number = start_block_number` and builds `last_n_numbers = (start_block_number..last_block_number)`, causing O(chain_length) database reads, MMR root computations, and memory allocation for a single message.

### Finding Description
The root cause is a mismatch between the upfront size check and the actual work performed:

1. The guard at lines 201–205 limits `difficulties.len() + last_n_blocks * 2 ≤ 1000`, but `last_n_blocks` is the attacker-supplied field, not the actual number of blocks returned.
2. The guard at lines 259–266 that enforces `difficulty_boundary > max(difficulties)` is vacuously skipped when `difficulties` is empty.
3. There is no explicit check that `difficulty_boundary > 0` or that it represents a meaningful threshold.
4. `get_first_block_total_difficulty_is_not_less_than` with `min_total_difficulty = 0` always returns `start_block_number` immediately, collapsing the boundary to the start of the requested range.
5. The resulting `last_n_numbers` vector spans the entire requested range with no FlyClient sampling, and `complete_headers` performs one database read + one MMR computation per entry.

### Impact Explanation
A single crafted P2P message causes the light-client protocol server to allocate a `Vec<BlockNumber>` of up to `last_block_number` entries (tens of millions on mainnet), then perform that many sequential disk reads and MMR root computations. This can exhaust server memory and CPU, causing a denial of service. The FlyClient protocol invariant — that only `last_n_blocks` recent blocks plus sparsely sampled older blocks are returned — is completely bypassed.

### Likelihood Explanation
Any peer that can connect to the light-client protocol port can send this message. No authentication, stake, or special role is required. The crafted message is trivial to construct.

### Recommendation
1. Reject requests where `difficulty_boundary == U256::zero()` explicitly before any processing.
2. After computing `last_n_numbers`, enforce that `last_n_numbers.len() + sampled_numbers.len() ≤ GET_LAST_STATE_PROOF_LIMIT` and return an error if exceeded.
3. Alternatively, cap `last_n_numbers` to `last_n_blocks` entries regardless of where `difficulty_boundary_block_number` lands.

### Proof of Concept
Send a `GetLastStateProof` message with:
- `last_n_blocks = 1`
- `start_number = 0`, `start_hash = genesis_hash`
- `last_hash = current_tip_hash`
- `difficulty_boundary = U256::zero()`
- `difficulties = []`

The size check passes (`0 + 1*2 = 2 ≤ 1000`). The server sets `difficulty_boundary_block_number = 0`, builds `last_n_numbers = (0..tip_number)`, and calls `complete_headers` with millions of block numbers, consuming unbounded resources.

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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
