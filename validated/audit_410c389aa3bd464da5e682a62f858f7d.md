### Title
Unbounded `last_n_numbers` via `difficulty_boundary=0` + `difficulties=[]` Bypasses Server-Side Size Guard — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can send a `GetLastStateProof` message with `difficulties=[]`, `difficulty_boundary=U256::zero()`, and `last_n_blocks=0`. The existing size guard only checks request-parameter counts, not the actual number of blocks that will be fetched and serialized. With a zero difficulty boundary, `get_first_block_total_difficulty_is_not_less_than` returns `start_block_number` immediately (every block's total difficulty is ≥ 0), setting `difficulty_boundary_block_number = start_block_number` and causing `last_n_numbers` to span the entire chain. The server then iterates over every block, computing MMR roots and fetching full block data for each, with no secondary size cap.

---

### Finding Description

**Step 1 — Size guard is bypassed**

The only upfront limit is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
``` [1](#0-0) [2](#0-1) 

With `difficulties=[]` (len = 0) and `last_n_blocks = 0`, the expression evaluates to `0`, which is ≤ 1000. The check passes.

**Step 2 — All per-field validation checks pass on empty difficulties**

- `difficulties.windows(2).any(...)` — empty slice, `any()` returns `false`. No error.
- `difficulties.last().map(...).unwrap_or(false)` — `last()` returns `None`, `unwrap_or(false)` returns `false`. The boundary check is skipped entirely.
- `difficulties.first()` — `None`, so the start-difficulty check is also skipped. [3](#0-2) 

**Step 3 — Zero boundary causes `difficulty_boundary_block_number = start_block_number`**

`get_first_block_total_difficulty_is_not_less_than` is called with `min_total_difficulty = U256::zero()`. Its very first check is:

```rust
if start_total_difficulty >= *min_total_difficulty {
    return Some((start_block_number, start_total_difficulty));
}
``` [4](#0-3) 

Since `U256` is unsigned, every block's total difficulty satisfies `>= 0`. The function returns `start_block_number` immediately.

**Step 4 — `last_n_numbers` covers the entire chain**

Back in `execute`, with `difficulty_boundary_block_number = start_block_number` and `last_n_blocks = 0`:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks { ... }
// false: entire chain length is not < 0

let last_n_numbers = (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
``` [5](#0-4) 

`last_n_numbers` now contains every block number from `start_block_number` to `last_block_number` — potentially millions of entries on mainnet.

**Step 5 — `complete_headers` iterates over all of them with no cap**

For each block number, `complete_headers` calls `snapshot.get_ancestor`, `snapshot.get_block`, and `snapshot.chain_root_mmr(...).get_root()`: [6](#0-5) 

There is no secondary size check on `block_numbers` before this loop executes. [7](#0-6) 

---

### Impact Explanation

A single malicious peer can force the light-client server to:
- Perform O(N) database lookups (header + block + MMR root) for every block in the chain
- Allocate O(N) memory for the resulting `Vec<VerifiableHeader>`
- Attempt to serialize and transmit a response proportional to the full chain length

On CKB mainnet (millions of blocks), this exhausts CPU, memory, and I/O on the server process. Multiple concurrent requests from different peer connections amplify the effect. The attack requires no credentials, no PoW, and no prior state — just a valid `last_hash` that is on the main chain.

---

### Likelihood Explanation

The attack is trivially constructable: the `GetLastStateProof` message is a standard light-client P2P message accepted from any peer. The attacker only needs to know a valid tip hash (publicly observable). The crafted message is small (empty difficulties, zero boundary). No special timing or chain state is required.

---

### Recommendation

Add a secondary size check on the computed `block_numbers` (or on `last_n_numbers` alone) before calling `complete_headers`, capping it at `GET_LAST_STATE_PROOF_LIMIT`. Additionally, validate that `difficulty_boundary > U256::zero()` is required when `last_block_number - start_block_number > last_n_blocks`, so a zero boundary is rejected before any computation occurs.

---

### Proof of Concept

```
Chain: 10,000 blocks, start_block_number=0, start_hash=genesis_hash
Message fields:
  last_hash        = tip_hash (valid, on main chain)
  start_hash       = genesis_hash
  start_number     = 0
  last_n_blocks    = 0
  difficulty_boundary = U256::zero()
  difficulties     = []

Expected (correct): server rejects with InvalidRequest or returns at most last_n_blocks headers.
Actual: server iterates all 10,000 blocks, computes 10,000 MMR roots, allocates a 10,000-entry
        VerifiableHeader vector, and attempts to send it — all from a single unauthenticated peer message.
```

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L254-266)
```rust
            if difficulties.windows(2).any(|d| d[0] >= d[1]) {
                let errmsg = "the difficulties should be monotonically increasing";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The maximum difficulty should be less than the difficulty boundary.
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-366)
```rust
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
