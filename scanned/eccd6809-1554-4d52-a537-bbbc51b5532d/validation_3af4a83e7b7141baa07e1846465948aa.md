### Title
Integer Overflow in `last_n_blocks` Limit Check Enables O(chain-height) Unbounded Allocation — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit guard in `GetLastStateProofProcess::execute` uses an unchecked `usize` multiplication that wraps to zero in release builds for specific `last_n_blocks` values (e.g., `2^63`). This allows an unprivileged light-client peer to bypass the 1000-sample cap and force the server to allocate a `Vec` proportional to the entire chain height, causing an OOM crash.

---

### Finding Description

**Correction to the question's arithmetic:** The question claims `(u64::MAX as usize) * 2` wraps to `0`. This is incorrect. On a 64-bit host:

```
u64::MAX as usize = 18446744073709551615
18446744073709551615 * 2 (mod 2^64) = 18446744073709551614
```

`18446744073709551614 > 1000` → the guard **fires** for `u64::MAX`. That specific value is safe.

**The real overflow value is `last_n_blocks = 2^63 = 9223372036854775808`:**

```
(2^63 as usize) * 2 = 2^64 (mod 2^64) = 0
```

The guard at line 201:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
```

evaluates to `0 + 0 > 1000` → **false** → the check is silently bypassed in release mode. [1](#0-0) [2](#0-1) 

**Downstream unbounded allocation — two sites:**

**Site 1 (reorg path, line 245–246):** With `start_block_number=1` and `start_hash` not matching the canonical block at height 1, the reorg branch executes:

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
// = 1 - min(1, 2^63) = 1 - 1 = 0
(min_block_number..start_block_number).collect()  // (0..1) → 1 element, harmless
``` [3](#0-2) 

**Site 2 (main path, lines 291–296):** The "not enough blocks" branch:

```rust
if last_block_number - start_block_number <= last_n_blocks  // e.g. 9999 <= 2^63 → always true
{
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
```

With `start_block_number=1` and a chain of N blocks, this allocates a `Vec<u64>` of `N-1` entries. At 10 million blocks that is ~80 MB just for the index vector. [4](#0-3) 

**Site 3 — `complete_headers` amplification:** Every entry in `block_numbers` triggers a `get_ancestor` DB walk plus an MMR root computation:

```rust
for number in numbers {
    if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
        let mmr = self.snapshot.chain_root_mmr(*number - 1);
``` [5](#0-4) 

This turns the O(N) allocation into O(N) blocking DB reads on the async executor thread.

---

### Impact Explanation

A single malformed `GetLastStateProof` message with `last_n_blocks = 2^63` causes the full-node light-client server to:
1. Allocate a `Vec<u64>` of size equal to the chain height (unbounded).
2. Perform one `get_ancestor` + one MMR root computation per block — O(chain_height) DB I/O.

On mainnet (millions of blocks) this exhausts heap memory and crashes the node process (OOM). The crash is local to the targeted node; no key material or consensus state is affected, but node availability is lost.

---

### Likelihood Explanation

- Light-client protocol is an opt-in feature but is enabled in production deployments.
- The attacker needs only a valid `last_hash` on the main chain (trivially obtained from `SendLastState`) and any `start_hash` that is not the canonical block at `start_number`.
- No PoW, no stake, no privileged role required — any peer that completes the light-client handshake can send this message.
- The overflow value `2^63` is a single fixed constant; no brute-force is needed.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Before (vulnerable in release):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
let sample_count = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if sample_count > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add an explicit upper-bound check on `last_n_blocks` itself (e.g., `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64`) before any arithmetic to reject obviously oversized values early.

---

### Proof of Concept

```rust
// Pseudocode — run against a node with light-client enabled and N blocks
let msg = GetLastStateProof {
    last_hash:   <any valid tip hash on main chain>,
    start_hash:  <any hash NOT equal to canonical block at height 1>,
    start_number: 1u64,
    last_n_blocks: 1u64 << 63,   // 2^63 — wraps limit check to 0
    difficulty_boundary: U256::MAX,
    difficulties: vec![],
};
// Expected (buggy release build): server allocates Vec of N-1 u64s → OOM
// Expected (fixed build): returns MalformedProtocolMessage immediately
```

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-135)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L244-247)
```rust
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
