### Title
Integer Overflow in `GetLastStateProof` Guard Bypasses 1000-Sample Limit — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

A remote light-client peer can send a `GetLastStateProof` message with `last_n_blocks` set to `2^63` (a valid `u64` value). On a 64-bit platform in release mode, the expression `(last_n_blocks as usize) * 2` wraps to `0`, causing the guard at line 201 to evaluate to `false` and allowing the handler to proceed. The server then performs block sampling and per-block MMR root computation over the entire chain (from `start_block_number` to the tip), far exceeding the intended 1000-block limit.

---

### Finding Description

**Guard code** [1](#0-0) 

`last_n_blocks` is decoded directly from the P2P message as a `u64` with no prior bounds check. [2](#0-1) 

The constant being guarded against is `1000`. [3](#0-2) 

**Overflow mechanics (64-bit release mode):**

| `last_n_blocks` (u64) | `as usize` | `* 2` (wraps) | guard result |
|---|---|---|---|
| `2^63` = `9223372036854775808` | `9223372036854775808` | `0` | `0 > 1000` → **false** |
| `2^63 + 1` | `9223372036854775809` | `2` | `2 > 1000` → **false** |

In Rust release mode, integer overflow wraps silently; no panic occurs.

**Post-bypass execution path:**

After the guard is bypassed, the condition at line 292 evaluates `last_block_number - start_block_number <= last_n_blocks`. Since no real chain has `2^63` blocks, this is always `true`, routing into the "not enough blocks" branch: [4](#0-3) 

With `start_block_number = 0` (attacker-controlled), `last_n_numbers` becomes `(0..last_block_number)` — every block in the chain. `complete_headers` then iterates over all of them, calling `get_ancestor`, `get_block`, and `chain_root_mmr` per block: [5](#0-4) 

Each `chain_root_mmr` call computes an MMR root, making this I/O and CPU intensive at scale.

---

### Impact Explanation

A single malformed request causes the server to perform O(chain_length) storage reads and MMR root computations. On a chain with millions of blocks, this is orders of magnitude beyond the intended 1000-block cap. Repeated requests from one or more peers can saturate I/O and CPU, degrading or halting service for legitimate peers.

---

### Likelihood Explanation

The attack requires only a valid tip block hash (public on-chain data) and the ability to connect as a light-client peer. No authentication, key material, or privileged access is needed. The crafted `u64` value `2^63` is trivially encodable in the molecule-serialized message.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before comparing:

```rust
// Option A: saturating_mul — safe, no panic, no wrap
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option B: explicit pre-check on last_n_blocks
if last_n_blocks > (constant::GET_LAST_STATE_PROOF_LIMIT as u64) {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Either fix prevents the wrap-around and correctly rejects any `last_n_blocks` that would exceed the limit.

---

### Proof of Concept

```rust
// In release mode on a 64-bit target:
let last_n_blocks: u64 = (usize::MAX / 2) as u64 + 1; // = 2^63
let difficulties_len: usize = 0;
let limit: usize = 1000;

// Simulates the guard expression as written:
let check = difficulties_len + (last_n_blocks as usize) * 2; // wraps to 0
assert!(check <= limit, "guard bypassed: {} <= {}", check, limit);
// assert passes — handler proceeds past the guard
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L356-366)
```rust
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
