Audit Report

## Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded Vec Allocation and Remote OOM — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary
In `GetLastStateProofProcess::execute`, the limit guard at L201 uses plain `(last_n_blocks as usize) * 2`, which wraps to zero in a Rust release build when `last_n_blocks = 2^63`, silently bypassing the 1000-entry cap. With the guard bypassed, the reorg branch at L245–246 allocates a `Vec<u64>` of up to `start_block_number` entries with no secondary cap, followed by O(S) `get_ancestor` database reads in `complete_headers`. Any unprivileged light-client peer can trigger this to crash the full node via OOM or I/O exhaustion.

## Finding Description
`last_n_blocks` is decoded as `u64` at L199. The guard at L201–204 is:

```rust
self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
```

On a 64-bit target, `usize` is 64 bits. Rust release builds use wrapping (two's-complement) semantics for integer overflow. With `last_n_blocks = 2^63 = 0x8000_0000_0000_0000u64`:

```
(2^63 as usize) * 2  ==  0   (wraps to zero)
```

With `difficulties` empty, the check becomes `0 > 1000` → **false**. The guard is bypassed with no error. [1](#0-0) 

The reorg branch at L244–246 then executes:

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
(min_block_number..start_block_number).collect()
```

With `last_n_blocks = 2^63 > start_block_number`, `min(start_block_number, last_n_blocks) = start_block_number`, so `min_block_number = 0`. The `.collect()` allocates a `Vec<u64>` with exactly `start_block_number` entries — no cap, no relation to `GET_LAST_STATE_PROOF_LIMIT`. [2](#0-1) 

`reorg_last_n_numbers` is then chained into `block_numbers` and passed to `complete_headers`, which calls `snapshot.get_ancestor()` for every entry — O(S) database reads. [3](#0-2) 

The constant confirming the intended limit: [4](#0-3) 

## Impact Explanation
A single crafted `GetLastStateProof` P2P message causes the full node to allocate a `Vec<u64>` of up to `chain_height` entries (e.g., ~10 M entries × 8 B = ~80 MB per request) and perform O(chain_height) `get_ancestor` database reads. Repeated requests from one or more peers exhaust heap memory and/or saturate I/O, crashing the full-node process. This matches the **High** impact class: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

## Likelihood Explanation
The light-client protocol server is enabled in production CKB nodes. The overflow value (`2^63`) is a single fixed constant requiring no brute-force. The attacker only needs to observe the current tip hash (publicly available) and supply any `start_hash` that differs from the real ancestor. The attack is trivially scriptable, requires no key or privileged role, and is repeatable at will.

## Recommendation
Replace the plain multiplication with a saturating variant so overflow cannot reduce the guard to zero:

```rust
// Before (vulnerable in release builds):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After:
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Additionally, cap `reorg_last_n_numbers` independently of the limit check:

```rust
let capped_last_n = min(last_n_blocks as usize, constant::GET_LAST_STATE_PROOF_LIMIT);
let min_block_number = start_block_number - min(start_block_number, capped_last_n as u64);
```

This ensures the reorg Vec is always bounded by `GET_LAST_STATE_PROOF_LIMIT` regardless of the supplied `last_n_blocks`.

## Proof of Concept
```rust
// Craft a GetLastStateProof message:
let msg = GetLastStateProof {
    last_hash:           current_main_chain_tip_hash(),  // publicly observable
    last_n_blocks:       1u64 << 63,                     // overflows (usize)*2 to 0
    start_number:        chain_height - 1,               // large S, within bounds
    start_hash:          H256::zero(),                   // wrong hash → reorg branch
    difficulties:        vec![],                         // empty → limit check = 0 > 1000 → false
    difficulty_boundary: U256::max_value(),
};
// Send via P2P light-client protocol connection.
// Server allocates Vec of (chain_height - 1) u64 entries, then performs
// (chain_height - 1) get_ancestor() DB reads → OOM / node hang.
```

Fuzz invariant that immediately fails: `reorg_last_n_numbers.len() <= GET_LAST_STATE_PROOF_LIMIT` with `last_n_blocks = 2^63` and `start_block_number > 1000`.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L244-247)
```rust
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-364)
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
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
