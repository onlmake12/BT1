I verified the code directly. Here is my assessment:

**Arithmetic overflow (lines 201–205):** Confirmed. `(u64::MAX as usize) * 2` wraps to `usize::MAX - 1` in a standard Rust release build. Adding `difficulties.len() = 2` wraps the total to `0`, which is not `> 1000`, bypassing the guard. [1](#0-0) 

**Reorg allocation (lines 244–247):** Confirmed. With `last_n_blocks = u64::MAX`, `min(start_block_number, u64::MAX) = start_block_number`, so `min_block_number = 0` and `(0..start_block_number).collect()` allocates `start_block_number` entries with no cap. [2](#0-1) 

**Additional allocation (lines 291–297):** With `last_n_blocks = u64::MAX`, the condition `last_block_number - start_block_number <= u64::MAX` is always true, so `last_n_numbers = (start_block_number..last_block_number).collect()` also allocates unboundedly. Combined with `reorg_last_n_numbers`, total allocation reaches `last_block_number * 8` bytes. [3](#0-2) 

**Ordering:** The reorg allocation at line 246 executes *before* the difficulties validation at lines 252–288, so the attacker does not need semantically valid difficulties to trigger the allocation. [4](#0-3) 

**Constant confirmed:** `GET_LAST_STATE_PROOF_LIMIT = 1000`. [5](#0-4) 

---

Audit Report

## Title
Integer Overflow in Size Guard Allows Unbounded Memory Allocation via `last_n_blocks=u64::MAX` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
The size guard at line 201 uses unchecked arithmetic on `usize`, which wraps to a small value in Rust release builds when `last_n_blocks = u64::MAX` and `difficulties.len() >= 2`. This bypasses the `GET_LAST_STATE_PROOF_LIMIT = 1000` check entirely. The reorg branch then allocates a `Vec` of up to `last_block_number` entries of `u64` with no independent bound, enabling OOM or excessive CPU/I/O on any node serving the light-client protocol.

## Finding Description
**Root cause:** The expression `self.message.difficulties().len() + (last_n_blocks as usize) * 2` uses Rust's default wrapping arithmetic in release builds. With `last_n_blocks = u64::MAX`, `(u64::MAX as usize) * 2` wraps to `usize::MAX - 1`. Adding `difficulties.len() = 2` wraps the sum to `0`, which is not `> 1000`, so the guard returns no error.

**Exploit flow:**
1. Attacker connects as a light-client peer (no privilege required).
2. Sends `GetLastStateProof` with: `last_hash` = any valid main-chain tip (publicly known), `start_hash` = any non-canonical hash (triggers reorg branch), `start_number` = any value up to `last_block_number`, `last_n_blocks = u64::MAX`, `difficulties` = any two U256 values.
3. Guard at line 201 wraps to `0`, passes.
4. `snapshot.is_main_chain(last_hash)` passes (valid tip used).
5. `start_block_number <= last_block_number` passes.
6. Reorg branch executes: `(0..start_block_number).collect()` allocates `start_block_number` u64 entries.
7. Condition at line 291 (`last_block_number - start_block_number <= u64::MAX`) is always true, so `last_n_numbers = (start_block_number..last_block_number).collect()` allocates the remainder.
8. Combined `block_numbers` Vec has `last_block_number` entries; `complete_headers` performs one DB lookup per entry.

**Why existing checks fail:** The only size guard is the overflowing expression at line 201. There is no independent cap on `last_n_blocks`, `reorg_last_n_numbers.len()`, or `last_n_numbers.len()`. The difficulties validation (lines 252–288) runs after the allocation and cannot prevent it.

## Impact Explanation
Each malicious request allocates approximately `last_block_number * 8` bytes (e.g., ~80 MB at block 10,000,000) and triggers `last_block_number` database reads. Multiple concurrent requests from different peers can exhaust node memory and I/O, crashing the node. This matches **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The attack requires only a valid P2P connection to a node with the light-client protocol server enabled. The crafted message is trivially small (two U256 values + metadata). The main-chain tip hash is publicly observable. The non-canonical `start_hash` can be any random 32-byte value. No proof-of-work, keys, or privileged role is needed. The attack is repeatable and can be parallelized across multiple connections.

## Recommendation
Add an explicit bound on `last_n_blocks` before any arithmetic, and replace the unchecked expression with saturating operations:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("last_n_blocks too large");
}
if self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2))
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, cap the reorg allocation independently before executing it:

```rust
let reorg_count = (start_block_number - min_block_number) as usize;
if reorg_count > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("reorg window too large");
}
```

## Proof of Concept
```rust
// Craft a GetLastStateProof message:
let msg = GetLastStateProof {
    last_hash:            <valid main-chain tip hash>,   // publicly observable
    start_hash:           <any 32-byte non-canonical hash>,
    start_number:         1_000_000u64,                 // start_block_number
    last_n_blocks:        u64::MAX,                     // triggers overflow
    difficulties:         vec![D1, D2],                 // len=2 causes guard to wrap to 0
    difficulty_boundary:  U256::MAX,
};
// Result: server allocates Vec of ~1,000,000 u64 entries (~8 MB) per request,
// bypassing the GET_LAST_STATE_PROOF_LIMIT = 1000 guard,
// then performs ~1,000,000 DB lookups in complete_headers.
// Sending ~100 concurrent such requests exhausts ~800 MB of heap.
```

### Citations

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
