Audit Report

## Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Bypasses Bound Check, Enabling Full-Chain `complete_headers` Exhaustion — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The size guard in `GetLastStateProofProcess::execute()` uses unchecked integer arithmetic that wraps to zero in Rust release builds when `last_n_blocks = u64::MAX` and `difficulties.len() = 2`, causing the guard to pass silently. This allows an attacker to force the reorg branch to allocate a `Vec` containing every block number from genesis to the chain tip, then drive `complete_headers` to perform tens of millions of synchronous DB lookups and MMR root computations per request, exhausting RAM and I/O and crashing the node.

## Finding Description

**Guard overflow (lines 201–202):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
```

On a 64-bit host in Rust release mode (wrapping arithmetic by default):
- `u64::MAX as usize` → `usize::MAX`
- `usize::MAX * 2` → wraps to `usize::MAX - 1`
- `2 + (usize::MAX - 1)` → wraps to `0`
- `0 > 1000` → **false** → guard passes, no rejection

**Reorg branch allocates unbounded Vec (lines 244–246):**

The attacker sets `start_hash = Byte32::zero()` and `start_number = tip_number`. Since `tip_number > 0` and `get_ancestor(&last_block_hash, tip_number).hash() ≠ Byte32::zero()`, the `else` branch is taken. With `last_n_blocks = u64::MAX`:

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
// min(tip, u64::MAX) = tip → min_block_number = 0
(min_block_number..start_block_number).collect()  // Vec of 0..tip
```

This allocation occurs **before** the difficulties validation block at line 252.

**Difficulties validation is bypassable (lines 254–288):**

With `difficulties = [U256::MAX - 2, U256::MAX - 1]` and `difficulty_boundary = U256::MAX`:
- Sorted check passes: `U256::MAX - 2 < U256::MAX - 1`
- Boundary check passes: `U256::MAX - 1 < U256::MAX`
- Start-difficulty check: actual chain total difficulty is far below `U256::MAX - 2`, so `total_difficulty < start_difficulty` → no rejection

**`last_n_numbers` is empty (lines 291–296):**

With `start_block_number = last_block_number = tip`: `last_block_number - start_block_number = 0 ≤ u64::MAX` → true branch taken, `last_n_numbers = (tip..tip)` = empty.

**`complete_headers` iterates every block (lines 132–177, called at lines 358–364):**

`block_numbers = reorg_last_n_numbers ∪ [] ∪ []` = all block numbers `0..tip`. For each, `complete_headers` calls:
- `snapshot.get_ancestor(...)` — DB lookup
- `snapshot.get_block(...)` — DB lookup
- `snapshot.chain_root_mmr(*number - 1).get_root()` — MMR computation
- Pushes a `packed::VerifiableHeader` to a growing `Vec`

At ~12M mainnet blocks: ~24M DB lookups, ~12M MMR root computations, and multi-GB heap growth per single request.

**Precondition for `last_hash`:** The attacker must supply a valid main-chain block hash (line 210), trivially obtained via a prior `GetLastState` message — a normal, unprivileged protocol operation.

## Impact Explanation

A single crafted `GetLastStateProof` message from any unprivileged peer causes the full node to allocate several GB of heap data and perform tens of millions of synchronous DB and MMR operations, leading to OOM kill or I/O saturation. Multiple concurrent requests amplify the effect. This matches the **High** impact class: *"Vulnerabilities which could easily crash a CKB node."*

## Likelihood Explanation

The attack requires no privileges, no proof-of-work, no keys, and no prior state beyond knowing the current tip hash (freely available via `GetLastState`). The crafted message is trivially constructable. Any peer that can send a `LightClientMessage` can trigger it, and the attack is fully repeatable.

## Recommendation

1. **Fix the overflow** using saturating arithmetic in the guard:
   ```rust
   if self.message.difficulties().len()
       .saturating_add((last_n_blocks as usize).saturating_mul(2))
       > constant::GET_LAST_STATE_PROOF_LIMIT
   ```
2. **Independently cap `last_n_blocks`** before any further processing (e.g., reject if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT / 2`).
3. **Move the guard before all allocations**: `reorg_last_n_numbers` must not be computed until `last_n_blocks` is validated.
4. **Cap `reorg_last_n_numbers` size**: Even in the reorg path, the returned block count must be bounded by `GET_LAST_STATE_PROOF_LIMIT`.

## Proof of Concept

```rust
// 1. Obtain tip hash and number via GetLastState (normal protocol op).
let tip_hash   = /* from GetLastState response */;
let tip_number = /* from GetLastState response */;

// 2. Craft the malicious message.
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)
    .start_hash(Byte32::zero())           // wrong hash → forces reorg branch
    .start_number(tip_number.pack())      // start = tip → reorg covers 0..tip
    .last_n_blocks(u64::MAX.pack())       // triggers guard overflow
    .difficulty_boundary(U256::MAX.pack())
    .difficulties(
        // 2 entries: 2 + (usize::MAX - 1) wraps to 0, bypassing guard
        vec![U256::MAX - 2, U256::MAX - 1].pack()
    )
    .build();

// 3. Send to any light-client-serving full node.
// Server allocates (tip_number * sizeof(VerifiableHeader)) bytes → OOM.
```

To reproduce: run a CKB node in release mode with the light-client protocol enabled, connect as a peer, send the above message, and observe OOM kill or indefinite hang on the serving thread.