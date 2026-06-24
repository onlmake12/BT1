Audit Report

## Title
Unbounded `last_n_numbers` bypasses `GET_LAST_STATE_PROOF_LIMIT` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The upfront limit check in `execute` bounds only `difficulties.len() + last_n_blocks * 2`, but `last_n_numbers` is computed as `(difficulty_boundary_block_number..last_block_number)` and can span the entire chain when an attacker supplies a `difficulty_boundary` value ≤ the total difficulty at `start_block_number`. The resulting `block_numbers` vector can contain millions of entries, all of which are processed by `complete_headers` with O(n) database reads and MMR root computations per entry, causing unbounded CPU and memory exhaustion on the serving node.

## Finding Description
The guard at lines 201–205 checks:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

This assumes `last_n_numbers.len() ≤ last_n_blocks`, but that invariant is not enforced. The actual size of `last_n_numbers` is determined at lines 318–319:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
``` [2](#0-1) 

The only guard that could limit this is at lines 313–316:

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
``` [3](#0-2) 

This guard only fires when there are **fewer** than `last_n_blocks` blocks after the boundary. When `difficulty_boundary_block_number` equals `start_block_number` (near the beginning of the chain), the condition `last_block_number - start_block_number < last_n_blocks` is false on a long chain, so the guard does not fire.

`difficulty_boundary_block_number` is set to `start_block_number` when `get_first_block_total_difficulty_is_not_less_than` returns immediately at lines 30–32:

```rust
if start_total_difficulty >= *min_total_difficulty {
    return Some((start_block_number, start_total_difficulty));
}
``` [4](#0-3) 

This happens whenever `difficulty_boundary` ≤ total difficulty at `start_block_number`. The validation checks at lines 259–288 that could reject a low `difficulty_boundary` are all gated on `difficulties` being non-empty: [5](#0-4) 

With `difficulties=[]`, none of those checks execute, so the attacker can freely set `difficulty_boundary = U256::from(1)`.

The oversized `block_numbers` vector (lines 350–354) is then passed directly to `complete_headers` with no post-check: [6](#0-5) 

`complete_headers` performs `get_ancestor`, `get_block`, and `chain_root_mmr` for every entry: [7](#0-6) 

`GET_LAST_STATE_PROOF_LIMIT` is 1000: [8](#0-7) 

## Impact Explanation
An unprivileged light-client peer can force the full-node server to allocate a `Vec` of millions of block numbers and perform millions of database reads and MMR root computations inside `complete_headers`. On a mainnet node with millions of blocks, this causes severe CPU and memory exhaustion, leading to OOM kill or an indefinite hang. This matches **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The attack requires only a valid `last_hash` on the main chain (trivially obtained from `SendLastState`) and a `difficulty_boundary` of `1`. No proof-of-work, no key, no privileged role is needed. A single malicious peer can trigger this repeatedly, and the attack is fully deterministic.

## Recommendation
Add a post-computation guard immediately after `block_numbers` is assembled (after line 354):

```rust
if block_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, clamp `difficulty_boundary_block_number` so that `last_n_numbers.len()` is always ≤ `last_n_blocks`, or fix the upfront check to account for the actual upper bound of `last_n_numbers`.

## Proof of Concept
Craft a `GetLastStateProof` message with:
- `last_hash` = current tip hash (from `SendLastState`)
- `start_hash` = any 32-byte value not equal to the ancestor of `last_hash` at `start_number` (or the real ancestor — the `last_n_numbers` overflow occurs regardless)
- `start_number` = 500
- `last_n_blocks` = 499
- `difficulty_boundary` = `U256::from(1u64)`
- `difficulties` = `[]`

On a node with chain height ≥ 1,000,000:
1. Limit check: `0 + 499*2 = 998 ≤ 1000` → passes.
2. `last_block_number - start_block_number = 999,500 > 499` → enters the `else` branch.
3. `get_first_block_total_difficulty_is_not_less_than(500, 1_000_000, &U256::from(1))` returns `Some((500, ...))` immediately.
4. `difficulty_boundary_block_number = 500`.
5. `1,000,000 - 500 = 999,500 ≥ 499` → adjustment guard does not fire.
6. `last_n_numbers = (500..1_000_000)` → 999,500 entries.
7. `block_numbers.len() ≈ 999,999` >> 1000.
8. `complete_headers` performs ~999,999 `get_ancestor` + `get_block` + `chain_root_mmr` calls → OOM or hang.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L259-288)
```rust
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The first difficulty should be greater than the total difficulty before the start block.
            if let Some(start_difficulty) = difficulties.first()
                && start_block_number > 0
            {
                let previous_block_number = start_block_number - 1;
                if let Some(total_difficulty) =
                    sampler.get_block_total_difficulty(previous_block_number)
                {
                    if total_difficulty >= *start_difficulty {
                        let errmsg = format!(
                            "the start difficulty is {start_difficulty:#x} too less than \
                                the previous block #{previous_block_number} of the start block"
                        );
                        return StatusCode::InvalidRequest.with_context(errmsg);
                    }
                } else {
                    let errmsg = format!(
                        "the total difficulty for block#{previous_block_number} is not found"
                    );
                    return StatusCode::InternalError.with_context(errmsg);
                };
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
