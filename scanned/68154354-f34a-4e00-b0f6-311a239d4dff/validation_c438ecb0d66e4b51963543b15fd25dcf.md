Audit Report

## Title
Integer Overflow in `GetLastStateProof` Guard Bypasses Sample Limit, Enabling O(chain_length) DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The guard at line 201 of `get_last_state_proof.rs` computes `(last_n_blocks as usize) * 2` without overflow protection. In Rust release builds, a crafted `last_n_blocks` value of `0x8000000000000001` causes this multiplication to wrap to `2`, silently bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. The raw, unvalidated `last_n_blocks` value is then used in downstream range computations that collect O(chain_length) block numbers, each triggering expensive MMR root computations and store lookups in `complete_headers`.

## Finding Description

**Overflow in the guard (line 199–205):**

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

With `last_n_blocks = 0x8000000000000001_u64` on a 64-bit host, `last_n_blocks as usize` is `0x8000000000000001`. Multiplying by `2` wraps to `0x2` in release mode (Rust's default wrapping semantics for `*`). The guard evaluates `0 + 2 > 1000` → false, and execution continues with the raw, huge `last_n_blocks` value.

**Downstream O(chain_length) allocation — `reorg_last_n_numbers` (line 245–246):**

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
(min_block_number..start_block_number).collect()
``` [2](#0-1) 

This branch is reached when `start_block_hash` does not match the ancestor at `start_block_number` — a condition the attacker controls by supplying a mismatched hash. With `last_n_blocks = 0x8000000000000001` and any `start_block_number` (e.g., 1,000,000), `min(start_block_number, last_n_blocks) = start_block_number`, so `min_block_number = 0` and the range `(0..start_block_number)` collects every block number from genesis.

**Downstream O(chain_length) allocation — `last_n_numbers` (line 291–296):**

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

For any real chain, `last_block_number - start_block_number` is far less than `0x8000000000000001`, so this branch is always taken, collecting all blocks from `start_block_number` to `last_block_number`.

**Per-entry cost in `complete_headers` (line 153–154):**

```rust
let mmr = self.snapshot.chain_root_mmr(*number - 1);
match mmr.get_root() {
``` [4](#0-3) 

For every collected block number, `complete_headers` calls `get_ancestor`, `get_block`, and `chain_root_mmr(*number - 1).get_root()`. On a mainnet node with millions of blocks, this is millions of MMR root computations and store lookups per single crafted request.

The limit constant that is bypassed: [5](#0-4) 

## Impact Explanation

A single crafted `GetLastStateProof` message forces the full node to perform O(chain_length) MMR root computations and O(chain_length) `get_ancestor` + `get_block` store lookups. On a mainnet node with millions of blocks, this saturates CPU and memory. Multiple concurrent peers amplify the effect, stalling or crashing the node. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node (10001–15000 points)**.

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

Alternatively, reject any `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2` (i.e., `> 500`) before the multiplication is attempted, which is simpler and equally correct.

## Proof of Concept

```rust
// Simulates release-mode wrapping behavior
let last_n_blocks: u64 = 0x8000000000000001;
let guard_value = (last_n_blocks as usize).wrapping_mul(2);
assert_eq!(guard_value, 2);           // overflow → 2
assert!(0 + guard_value <= 1000);     // guard passes — BUG

// reorg_last_n_numbers path (start_block_number = 1_000_000, mismatched start_hash)
let start_block_number: u64 = 1_000_000;
let min_block_number = start_block_number - std::cmp::min(start_block_number, last_n_blocks);
assert_eq!(min_block_number, 0);
let reorg_range: Vec<u64> = (min_block_number..start_block_number).collect();
assert_eq!(reorg_range.len(), 1_000_000); // 1M MMR computations triggered

// last_n_numbers path (last_block_number = 1_100_000)
let last_block_number: u64 = 1_100_000;
assert!(last_block_number - start_block_number <= last_n_blocks); // always true
let last_n_numbers: Vec<u64> = (start_block_number..last_block_number).collect();
assert_eq!(last_n_numbers.len(), 100_000); // additional 100K MMR computations
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L153-154)
```rust
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L245-246)
```rust
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L291-296)
```rust
        let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
            <= last_n_blocks
        {
            // There is not enough blocks, so we take all of them; so there is no sampled blocks.
            let sampled_numbers = Vec::new();
            let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
