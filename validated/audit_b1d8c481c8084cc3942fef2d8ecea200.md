Audit Report

## Title
Unbounded `last_n_numbers` allocation bypasses `GET_LAST_STATE_PROOF_LIMIT` — (File: `util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The upfront limit check in `GetLastStateProofProcess::execute` guards only `difficulties.len() + last_n_blocks * 2 > GET_LAST_STATE_PROOF_LIMIT`, implicitly assuming `last_n_numbers.len() ≤ last_n_blocks`. This assumption fails when `difficulty_boundary` resolves to a block near the chain start: the clamping guard at lines 313–316 only fires when there are *too few* trailing blocks, not too many, so `last_n_numbers` grows to `last_block_number − difficulty_boundary_block_number` — bounded only by chain height. A single crafted P2P message with `difficulties = []` and `difficulty_boundary = U256::from(1)` causes O(N) DB reads, O(N) MMR root computations, and O(N) memory allocation, exhausting node resources.

## Finding Description

**Limit check (lines 201–205):** [1](#0-0) 

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
```

With `difficulties = []` and `last_n_blocks = 1`, this evaluates to `0 + 2 = 2 ≤ 1000` and passes. The check never accounts for the actual size of `last_n_numbers`.

**`difficulty_boundary` lower-bound bypass (lines 259–266):** [2](#0-1) 

```rust
if difficulties
    .last()
    .map(|d| *d >= difficulty_boundary)
    .unwrap_or(false)   // ← returns false when difficulties is empty
```

When `difficulties` is empty, `.last()` is `None`, `.unwrap_or(false)` is `false`, so the check is skipped entirely. Any `difficulty_boundary` value — including `U256::from(1)` — is accepted.

**`difficulty_boundary_block_number` resolution (lines 299–311):** [3](#0-2) 

`get_first_block_total_difficulty_is_not_less_than(0, N, &U256::from(1))` returns block 0 because genesis total difficulty ≥ 1 (checked at lines 30–33 of the same file), so `difficulty_boundary_block_number = 0`. [4](#0-3) 

**Clamping guard (lines 313–316):** [5](#0-4) 

```rust
if last_block_number - difficulty_boundary_block_number < last_n_blocks {
    difficulty_boundary_block_number = last_block_number - last_n_blocks;
}
```

With `last_block_number = 1,000,000`, `difficulty_boundary_block_number = 0`, `last_n_blocks = 1`: condition is `1,000,000 < 1` → **false**. Guard does not fire.

**`last_n_numbers` construction (lines 318–319):** [6](#0-5) 

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (0..1_000_000) → 1,000,000 entries
```

**`else` branch at line 345–346:** Since `difficulty_boundary_block_number = 0`, the guard `if difficulty_boundary_block_number > 0` is false, so `sampled_numbers = []` and the full `last_n_numbers` of 1,000,000 entries is returned. [7](#0-6) 

**`complete_headers` loop (lines 132–177):** For each of the 1,000,000 entries, the loop performs a DB read via `snapshot.get_block()` and an MMR root computation via `snapshot.chain_root_mmr(*number - 1).get_root()`, all triggered by a single message. [8](#0-7) 

The `GET_LAST_STATE_PROOF_LIMIT` constant is 1000. [9](#0-8) 

## Impact Explanation

**High (10001–15000 points): Vulnerabilities which could easily crash a CKB node.**

A single crafted `GetLastStateProof` P2P message with chain height N = 1,000,000 forces 1,000,000 DB reads, 1,000,000 MMR root computations, and allocation of 1,000,000 `VerifiableHeader` objects — all without any rate limiting or per-message work cap. This exhausts CPU and memory, hanging or crashing the light-client server thread and effectively taking the node offline. The attack is repeatable with zero cost to the attacker. The O(N²) characterization in the submission is overstated — `store::get_ancestor` takes the `is_main_chain` shortcut (O(1) direct lookup) since `last_hash` is verified to be on the main chain at line 210. The actual complexity is O(N), not O(N²). The core vulnerability is nonetheless real and severe.

## Likelihood Explanation

The attack requires only: (1) a valid `last_hash` on the main chain, which is publicly observable from any synced node; (2) the ability to send a `LightClientMessage::GetLastStateProof` packet. No PoW, no key, no privileged role is needed. Any peer connected to the light-client protocol can trigger this. The crafted message passes all existing validation checks.

## Recommendation

After constructing `last_n_numbers`, enforce the invariant before proceeding:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();

if last_n_numbers.len() > last_n_blocks as usize {
    return StatusCode::MalformedProtocolMessage
        .with_context("last_n_numbers exceeds last_n_blocks");
}
```

Alternatively, clamp `difficulty_boundary_block_number` from below symmetrically with the existing upper clamp:

```rust
let lower_bound = last_block_number.saturating_sub(last_n_blocks);
if difficulty_boundary_block_number < lower_bound {
    difficulty_boundary_block_number = lower_bound;
}
```

Additionally, add a post-assembly assertion before `complete_headers`:

```rust
assert!(block_numbers.len() <= constant::GET_LAST_STATE_PROOF_LIMIT);
```

## Proof of Concept

**Setup:** CKB node with chain height N = 1,000,000.

**Crafted message:**
| Field | Value |
|---|---|
| `last_hash` | tip block hash (public) |
| `start_hash` | genesis hash |
| `start_number` | 0 |
| `last_n_blocks` | 1 |
| `difficulty_boundary` | `U256::from(1)` |
| `difficulties` | `[]` |

**Execution trace:**
1. Limit check: `0 + 1×2 = 2 ≤ 1000` → passes.
2. `start_block_number = 0` → `reorg_last_n_numbers = []`.
3. `last_block_number − start_block_number = 1,000,000 > 1` → enters `else` branch.
4. `get_first_block_total_difficulty_is_not_less_than(0, 1_000_000, &U256::from(1))` → returns `(0, genesis_difficulty)` since genesis total difficulty ≥ 1; `difficulty_boundary_block_number = 0`.
5. Clamping guard: `1,000,000 − 0 = 1,000,000 < 1` → **false**, guard skipped.
6. `last_n_numbers = (0..1_000_000)` → **1,000,000 entries**.
7. `difficulty_boundary_block_number = 0` → `else` branch at line 345: `sampled_numbers = []`.
8. `block_numbers.len() = 1,000,000` >> `GET_LAST_STATE_PROOF_LIMIT (1000)`.
9. `complete_headers` iterates 1,000,000 times, each performing DB reads and MMR root computation → node resource exhaustion.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
