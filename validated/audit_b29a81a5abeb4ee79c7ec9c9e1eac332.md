### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Check Allows Unbounded Block Processing — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

In `GetLastStateProofProcess::execute`, the guard that enforces `GET_LAST_STATE_PROOF_LIMIT = 1000` contains an unchecked multiplication `(last_n_blocks as usize) * 2`. In Rust release builds, integer overflow wraps silently. An attacker-controlled `last_n_blocks` value of `usize::MAX/2 + 1` causes this expression to wrap to `0`, making the check trivially pass. The server then processes up to `last_block_number − start_block_number` blocks — the entire chain — in a single message.

### Finding Description

The limit check at line 201:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

`last_n_blocks` is a `u64` field from the peer message, cast to `usize` (also 64 bits on x86-64), then multiplied by 2. In Rust release mode, `*` wraps on overflow. Setting `last_n_blocks = 0x8000_0000_0000_0000` (`usize::MAX/2 + 1`) makes `(last_n_blocks as usize) * 2 = 0`. With an empty `difficulties` vec, the check becomes `0 > 1000 = false`, and execution continues past the guard.

After the bypass, the code at lines 291–297 evaluates:

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [2](#0-1) 

Since `last_n_blocks` is enormous, this branch is always taken, and `last_n_numbers` spans the entire range `[start_block_number, last_block_number)`. The attacker sets `start_block_number = 0` and `last_hash` to the current tip, so the server iterates over every block on the chain.

Similarly, `reorg_last_n_numbers` at line 245 uses `min(start_block_number, last_n_blocks)`, which also resolves to `start_block_number` when `last_n_blocks` is huge, producing a range of up to `start_block_number` entries. [3](#0-2) 

All collected block numbers are then passed to `complete_headers`, which performs per-block `get_ancestor`, `get_block`, and `chain_root_mmr` calls — all expensive I/O and CPU operations with no secondary cap. [4](#0-3) 

The constant definitions confirm the intended cap is 1000: [5](#0-4) 

### Impact Explanation

On a mainnet node with millions of blocks, a single crafted `GetLastStateProof` message forces the server to load, hash, and build MMR proofs for every block from genesis to tip. This exhausts CPU, memory, and disk I/O, causing severe degradation or an OOM crash. The attack is repeatable and requires no authentication.

### Likelihood Explanation

Any peer that can connect to the light-client protocol port can send this message. The malicious field is a single `u64` value; no PoW, no key, no prior state is required. The overflow value `0x8000000000000000` is a valid `Uint64` in the molecule encoding.

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
let last_n_blocks_x2 = (last_n_blocks as usize).saturating_mul(2);
if self.message.difficulties().len() + last_n_blocks_x2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject any `last_n_blocks` value that exceeds `GET_LAST_STATE_PROOF_LIMIT / 2` before any arithmetic.

### Proof of Concept

```rust
// last_n_blocks = usize::MAX/2 + 1 = 0x8000_0000_0000_0000 on 64-bit
let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64;

// In release mode (wrapping arithmetic):
let check_value = (last_n_blocks as usize).wrapping_mul(2); // == 0
assert_eq!(check_value, 0);

// difficulties.len() = 0, so:
// 0 + 0 > 1000  =>  false  =>  guard bypassed

// Server then executes:
// last_n_numbers = (0..last_block_number).collect()
// => processes every block on the chain
```

Send a `GetLastStateProof` message with:
- `last_n_blocks = 0x8000000000000000`
- `difficulties = []` (empty)
- `last_hash` = current chain tip hash (obtainable via `GetLastState`)
- `start_number = 0`, `start_hash` = genesis hash
- `difficulty_boundary` = any value above the genesis difficulty

The server will attempt to build verifiable headers for every block from 0 to tip, exhausting resources.

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
