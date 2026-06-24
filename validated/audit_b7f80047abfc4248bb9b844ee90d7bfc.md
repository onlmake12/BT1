Audit Report

## Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses `GET_LAST_STATE_PROOF_LIMIT` Guard — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
`last_n_blocks` is read as `u64` from a peer message and cast to `usize` before being multiplied by `2` in the guard expression. In Rust release builds, this multiplication wraps to `0` when `last_n_blocks >= 2^63`, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check entirely. Downstream code then collects every block number from `start_block_number` to `last_block_number` into a vector and calls `complete_headers` for each entry, performing unbounded per-block MMR and storage work proportional to full chain length.

## Finding Description
**Root cause — guard arithmetic overflow (lines 199–205):**

`last_n_blocks` is a `u64` read directly from the peer message: [1](#0-0) 

The guard expression is: [2](#0-1) 

On a 64-bit target `usize` is 64 bits, so `last_n_blocks as usize` is lossless. The subsequent `* 2` is a plain `usize` multiply. In Rust **release mode** integer overflow wraps silently. With `last_n_blocks = 2^63`:

```
(9223372036854775808usize) * 2  ==  2^64  ==  0  (mod 2^64)
```

The guard reduces to `difficulties.len() + 0 > 1000`, which is `false` for any normal `difficulties` length, so execution continues past the guard.

**Downstream unbounded allocation (lines 291–297):** [3](#0-2) 

Because `last_n_blocks = 2^63` exceeds any real chain length, the condition `last_block_number - start_block_number <= last_n_blocks` is always `true`. The server collects **every block number** from `start_block_number` to `last_block_number` with no cap.

**Unbounded per-block work (lines 356–366):** [4](#0-3) 

`complete_headers` is called for every entry in `block_numbers`, performing per-block MMR root computation and storage lookups. The limit constant itself is correct: [5](#0-4) 

but the guard arithmetic is broken, rendering it ineffective.

## Impact Explanation
A single malicious peer can force the light-client-serving node to iterate over and compute MMR proofs for every block from `start_block_number` to the chain tip. On a mature chain this is millions of blocks, consuming CPU and memory proportional to chain length. This constitutes a targeted, repeatable crash/resource-exhaustion attack against a CKB node, matching the **High** impact class: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

## Likelihood Explanation
Exploitation requires only crafting a single `GetLastStateProof` P2P message with `last_n_blocks = 2^63`. No proof-of-work, no key material, no privileged role is needed. The light client protocol is a production P2P endpoint reachable by any connecting peer. The attack is repeatable at will.

## Recommendation
Replace the plain multiplication with a saturating variant so large values cannot wrap:

```rust
// Before (vulnerable):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Alternatively, reject the message immediately if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64 / 2` before any cast, which is simpler and avoids the cast entirely.

## Proof of Concept
```rust
// Attacker constructs:
let malicious_last_n_blocks: u64 = 1u64 << 63; // 9223372036854775808

// Guard arithmetic in release mode:
let guard_value = (malicious_last_n_blocks as usize).wrapping_mul(2); // == 0
assert_eq!(guard_value, 0); // guard is bypassed

// Server then executes:
// last_n_numbers = (0..last_block_number).collect::<Vec<_>>();
// complete_headers called for every block — O(chain_length) work
```

Send a `GetLastStateProof` message with:
- `last_hash`: any valid tip hash on the main chain
- `start_hash`: genesis hash, `start_number`: 0
- `last_n_blocks`: `9223372036854775808` (2^63)
- `difficulties`: empty, `difficulty_boundary`: any nonzero value

The server will process all blocks from genesis to tip without the 1000-block cap, exhausting CPU and memory until the node crashes or becomes unresponsive.

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-199)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
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
