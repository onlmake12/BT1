Let me read the exact lines of the guard and downstream usage more carefully.

### Title
Integer Overflow in `GetLastStateProofProcess::execute` Bypasses `GET_LAST_STATE_PROOF_LIMIT` Guard — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can send a `GetLastStateProof` P2P message with `last_n_blocks` set to any value ≥ `2^63` (e.g., `9223372036854775808`). The guard expression `(last_n_blocks as usize) * 2` wraps to `0` in Rust release builds due to integer overflow, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. Downstream logic then allocates a `last_n_numbers` vector containing every block from `start_block_number` to `last_block_number`, causing work proportional to the full chain length rather than the intended 1000-block cap.

---

### Finding Description

**Guard expression (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`last_n_blocks` is a `u64` read directly from the peer message: [2](#0-1) 

On a 64-bit target, `usize` is 64 bits, so `last_n_blocks as usize` is lossless. However, the subsequent `* 2` is a plain `usize` multiplication. In Rust **release mode**, integer overflow wraps (two's complement). If the attacker sets `last_n_blocks = 2^63 = 9223372036854775808`:

```
(9223372036854775808usize) * 2  ==  2^64  ==  0  (mod 2^64)
```

The guard becomes `0 + difficulties.len() > 1000`, which is `false` for any reasonable `difficulties` length, so execution continues past the guard.

**Downstream unbounded allocation (lines 291–297):**

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks
{
    let sampled_numbers = Vec::new();
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    (sampled_numbers, last_n_numbers)
``` [3](#0-2) 

Because `last_n_blocks = 2^63` is astronomically larger than any real chain length, the condition `last_block_number - start_block_number <= last_n_blocks` is always `true`. The server collects **every block number** from `start_block_number` to `last_block_number` into `last_n_numbers`, with no cap.

`complete_headers` is then called for each entry, performing per-block MMR root computation and block lookups: [4](#0-3) 

The limit constant itself is correct at 1000, but the guard arithmetic is broken: [5](#0-4) 

---

### Impact Explanation

Any peer reachable via the light client P2P protocol can trigger this. The server will iterate over and compute MMR proofs for every block from `start_block_number` to the chain tip — potentially millions of blocks on a mature chain — consuming CPU and memory proportional to chain length. This is a targeted, repeatable DoS against light-client-serving nodes.

---

### Likelihood Explanation

The exploit requires only crafting a single valid-looking `GetLastStateProof` message with `last_n_blocks = 2^63`. No PoW, no key, no privileged role. The light client protocol is a production P2P endpoint reachable by any connecting peer.

---

### Recommendation

Replace the plain multiplication with a saturating or checked variant so that large values cannot wrap:

```rust
// Before (vulnerable):
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// After (safe):
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Alternatively, reject the message immediately if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64 / 2` before any cast.

---

### Proof of Concept

```rust
// Attacker constructs:
let malicious_last_n_blocks: u64 = 1u64 << 63; // 9223372036854775808

// Guard arithmetic in release mode:
let guard_value = (malicious_last_n_blocks as usize).wrapping_mul(2); // == 0
assert_eq!(guard_value, 0); // passes — guard is bypassed

// Server then executes:
// last_n_numbers = (0..last_block_number).collect::<Vec<_>>();
// complete_headers called for every block — O(chain_length) work
```

Send a `GetLastStateProof` message with:
- `last_hash`: any valid tip hash on the main chain
- `start_hash`: genesis hash, `start_number`: 0
- `last_n_blocks`: `9223372036854775808`
- `difficulties`: empty, `difficulty_boundary`: any nonzero value

The server will process all blocks from genesis to tip without the 1000-block cap.

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
