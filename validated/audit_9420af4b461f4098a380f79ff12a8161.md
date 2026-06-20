Now I have all the information needed to trace the full execution path. Let me analyze the logic carefully.

The vulnerability claim is **valid**. Here is the full analysis:

---

### Title
Unbounded `last_n_numbers` Response via `difficulty_boundary=0` and `difficulties=[]` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary
An unprivileged remote peer can send a `GetLastStateProof` P2P message with `difficulties=[]` and `difficulty_boundary=U256::zero()`. All input validation guards are bypassed because they are conditioned on `difficulties` being non-empty. The boundary resolution function then immediately anchors `difficulty_boundary_block_number` to `start_block_number` (since any total difficulty ≥ 0), causing `last_n_numbers` to span the entire chain from `start_block_number` to `last_block_number`. The size guard at line 201 does not bound this output, so the server performs unbounded database I/O, memory allocation, and MMR proof computation proportional to chain length.

### Finding Description

**Entry point:** Any peer can send a `LightClientMessage::GetLastStateProof` over the P2P light-client protocol. This is dispatched directly in `LightClientProtocol::try_process`: [1](#0-0) 

**Step 1 — Size guard is insufficient.**

The only upfront size check is: [2](#0-1) 

With `difficulties=[]` (len=0) and `last_n_blocks=1`, this evaluates to `0 + 2 = 2 > 1000` → **false** → passes. The guard checks the *input* field sizes, not the *output* `last_n_numbers` size, which is determined later by `difficulty_boundary_block_number`.

**Step 2 — All three validation checks are skipped for empty `difficulties`.**

- Monotonicity check: `difficulties.windows(2).any(...)` on an empty slice → `false` → no error.
- Boundary check: `difficulties.last()` → `None` → `.unwrap_or(false)` → `false` → no error.
- Start-difficulty check: `difficulties.first()` → `None` → `if let Some(...)` does not match → no check. [3](#0-2) 

**Step 3 — `get_first_block_total_difficulty_is_not_less_than` returns `start_block_number` immediately when `min_total_difficulty = 0`.** [4](#0-3) 

Since every block's total difficulty is ≥ 0, the condition `start_total_difficulty >= U256::zero()` is always true, so the function returns `Some((start_block_number, ...))` on the very first check. This sets `difficulty_boundary_block_number = start_block_number`.

**Step 4 — `last_n_numbers` spans the entire chain.** [5](#0-4) 

With `difficulty_boundary_block_number = start_block_number` and `last_block_number` being the chain tip, `last_n_numbers = (start_block_number..last_block_number)` — potentially millions of entries on mainnet.

**Step 5 — The server fetches all headers and builds MMR proofs for all of them.** [6](#0-5) 

`complete_headers` iterates over every block number in `block_numbers`, performing a `get_ancestor` lookup, a `get_block` lookup, and an MMR root computation per block — all unbounded.

### Impact Explanation

A single malicious peer can force the light-client server to:
- Perform O(chain_length) random database reads
- Allocate O(chain_length) memory for block numbers, headers, and MMR positions
- Compute O(chain_length) MMR proofs
- Attempt to send a response of unbounded size

On CKB mainnet (currently >13 million blocks), this is a severe denial-of-service: one crafted message can exhaust server CPU, memory, and I/O for the duration of the computation.

### Likelihood Explanation

The attack requires no privileges, no keys, no hashpower, and no coordination. Any peer that can connect to the light-client P2P port can send this message. The message is valid at the wire-format level (empty `Uint256Vec` is a legal molecule encoding). The exploit is deterministic and reproducible.

### Recommendation

The fix must bound the *output* size, not just the input field sizes. After computing `difficulty_boundary_block_number`, add a check:

```rust
let last_n_count = last_block_number - difficulty_boundary_block_number;
if last_n_count > constant::GET_LAST_STATE_PROOF_LIMIT as u64 {
    return StatusCode::InvalidRequest.with_context("difficulty_boundary too small");
}
```

Additionally, reject `difficulty_boundary = U256::zero()` explicitly, or require that `difficulty_boundary` is strictly greater than the total difficulty of `start_block_number - 1`, so it is always a meaningful chain-anchored value.

### Proof of Concept

```rust
// On a chain mined to 10_000 blocks:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_header.hash())
    .start_hash(genesis_header.hash())
    .start_number(0u64)
    .last_n_blocks(1u64)                    // passes size guard: 0 + 2 <= 1000
    .difficulty_boundary(U256::zero())      // bypasses all validation
    // difficulties omitted → empty Vec
    .build();
// Server responds with all 10_000 headers instead of at most last_n_blocks=1.
```

The server will not ban the peer (no validation error is returned), will fetch all 10,000 headers, build their MMR proofs, and attempt to send the full response — confirming complete bypass of the difficulty boundary guard. [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L30-33)
```rust
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L201-205)
```rust
        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L254-288)
```rust
            if difficulties.windows(2).any(|d| d[0] >= d[1]) {
                let errmsg = "the difficulties should be monotonically increasing";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The maximum difficulty should be less than the difficulty boundary.
            if difficulties
                .last()
                .map(|d| *d >= difficulty_boundary)
                .unwrap_or(false)
            {
                let errmsg = "the difficulty boundary should be greater than all difficulties";
                return StatusCode::InvalidRequest.with_context(errmsg);
            }
            // The first difficulty should be greater than the total difficulty before the start block.
            if let Some(start_difficulty) = difficulties.first()
                && start_block_number > 0
            {
                let previous_block_number = start_block_number - 1;
                if let Some(total_difficulty) =
                    sampler.get_block_total_difficulty(previous_block_number)
                {
                    if total_difficulty >= *start_difficulty {
                        let errmsg = format!(
                            "the start difficulty is {start_difficulty:#x} too less than \
                                the previous block #{previous_block_number} of the start block"
                        );
                        return StatusCode::InvalidRequest.with_context(errmsg);
                    }
                } else {
                    let errmsg = format!(
                        "the total difficulty for block#{previous_block_number} is not found"
                    );
                    return StatusCode::InternalError.with_context(errmsg);
                };
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
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
