Audit Report

## Title
Integer Overflow in `GetLastStateProof` Guard Bypasses Sample Limit, Enabling O(chain_length) DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The guard at line 201 computes `(last_n_blocks as usize) * 2` using Rust's default wrapping multiplication in release builds. A crafted `last_n_blocks` value of `0x8000000000000001_u64` causes the multiplication to wrap to `2`, silently bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. The raw oversized `last_n_blocks` value then drives two downstream range collections that together cover O(chain_length) block numbers, each requiring a full `get_ancestor` + `get_block` + `chain_root_mmr(...).get_root()` call.

## Finding Description

**Root cause — unchecked multiplication in the guard (lines 199–205):** [1](#0-0) 

`last_n_blocks` is a `u64` read directly from the peer message. On a 64-bit host `usize` is also 64 bits, so `0x8000000000000001_u64 as usize = 0x8000000000000001_usize`. Rust's `*` operator wraps in release mode: `0x8000000000000001_usize * 2 = 0x0000000000000002_usize`. The guard evaluates `0 + 2 > 1000 → false` and execution continues with the raw, huge `last_n_blocks` value.

**Path 1 — `reorg_last_n_numbers` (lines 245–246):** [2](#0-1) 

This branch is taken when `start_block_number > 0` and the supplied `start_block_hash` does not match the actual ancestor — a condition the attacker trivially controls by sending a mismatched hash. With `last_n_blocks = 0x8000000000000001` and any `start_block_number` (e.g., 1,000,000), `min(start_block_number, last_n_blocks) = start_block_number`, so `min_block_number = 0` and the range `(0..start_block_number)` collects every block number from genesis to `start_block_number`.

**Path 2 — `last_n_numbers` (lines 291–296):** [3](#0-2) 

For any realistic chain, `last_block_number - start_block_number` is far less than `0x8000000000000001`, so this branch is always taken, collecting all blocks from `start_block_number` to `last_block_number`.

Combined, both paths cover the entire chain from block 0 to `last_block_number`.

**Per-entry cost in `complete_headers` (lines 132–163):** [4](#0-3) 

For every collected block number the function calls `get_ancestor`, `get_block`, and `chain_root_mmr(*number - 1).get_root()`. On a mainnet node with millions of blocks this is millions of MMR root computations and store lookups per single crafted request.

**Bypassed constant:** [5](#0-4) 

**Prerequisite checks that do NOT block the attack:**
- Line 210: `is_main_chain(&last_block_hash)` — attacker supplies the current tip hash, trivially satisfied.
- Line 231: `start_block_number > last_block_number` — attacker sets `start_block_number` to any valid block height.
- Lines 253–288: difficulty validation — attacker sends an empty `difficulties` list, skipping all difficulty checks.

## Impact Explanation

A single crafted `GetLastStateProof` message forces the full node to perform O(chain_length) MMR root computations and O(chain_length) `get_ancestor` + `get_block` store lookups. On a mainnet node with millions of blocks this saturates CPU and memory. Multiple concurrent peers amplify the effect, stalling or crashing the node. This matches **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

The exploit requires only a valid P2P connection and a single crafted `GetLastStateProof` message. No proof-of-work, no keys, and no privileged role are needed. The overflow value `0x8000000000000001` is trivially constructable. The path is fully reachable in production release builds where Rust wrapping arithmetic is the default for `*`. The attack is repeatable and can be parallelized across multiple connections.

## Recommendation

Replace the unchecked multiplication with a saturating variant before the comparison:

```rust
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` (i.e., `> 500`) before the multiplication is attempted.

## Proof of Concept

```rust
// Release-mode wrapping behavior
let last_n_blocks: u64 = 0x8000000000000001;
let guard_value = (last_n_blocks as usize).wrapping_mul(2);
assert_eq!(guard_value, 2);        // overflow → 2
assert!(0 + guard_value <= 1000);  // guard passes — BUG

// reorg_last_n_numbers path (start_block_number=1_000_000, mismatched start_hash)
let start_block_number: u64 = 1_000_000;
let min_block_number = start_block_number - std::cmp::min(start_block_number, last_n_blocks);
assert_eq!(min_block_number, 0);
let reorg_range: Vec<u64> = (min_block_number..start_block_number).collect();
assert_eq!(reorg_range.len(), 1_000_000); // 1M MMR computations triggered

// last_n_numbers path (last_block_number=1_100_000)
let last_block_number: u64 = 1_100_000;
assert!(last_block_number - start_block_number <= last_n_blocks); // always true
let last_n_numbers: Vec<u64> = (start_block_number..last_block_number).collect();
assert_eq!(last_n_numbers.len(), 100_000); // additional 100K MMR computations
```

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-297)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
            (sampled_numbers, last_n_numbers)
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
