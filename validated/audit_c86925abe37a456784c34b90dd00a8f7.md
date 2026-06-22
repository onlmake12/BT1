The code trace confirms this is a real vulnerability. Here is the full analysis:

---

### Title
Unbounded `last_n_numbers` via `difficulty_boundary=0` bypasses `GET_LAST_STATE_PROOF_LIMIT` in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard at the top of `execute` only checks `difficulties.len() + last_n_blocks * 2 > GET_LAST_STATE_PROOF_LIMIT`. When an attacker sends `difficulty_boundary=U256::zero()` and `difficulties=[]`, the binary-search helper immediately returns `start_block_number` as the boundary block, causing `last_n_numbers` to span the entire chain. The actual number of blocks processed by `complete_headers` is completely unbounded by the 1000-entry limit.

---

### Finding Description

**Step 1 — Limit check passes trivially.** [1](#0-0) 

With `difficulties=[]` (len=0) and `last_n_blocks=10`, the expression evaluates to `0 + 20 = 20`, which is well under `GET_LAST_STATE_PROOF_LIMIT = 1000`. [2](#0-1) 

**Step 2 — Short-circuit branch is not taken.** [3](#0-2) 

With `start_block_number=0` and `last_block_number=N` (e.g. 50,000), `N - 0 <= 10` is false, so execution falls into the else branch.

**Step 3 — `difficulty_boundary=0` forces `difficulty_boundary_block_number = start_block_number`.** [4](#0-3) 

`get_first_block_total_difficulty_is_not_less_than` is called with `min_total_difficulty = U256::zero()`. At line 31, it checks `start_total_difficulty >= U256::zero()`. Because `U256` is unsigned, this is **always true** for any valid block. The function returns `Some((start_block_number, ...))` immediately — i.e., block 0.

**Step 4 — The adjustment guard is skipped.** [5](#0-4) 

`last_block_number - difficulty_boundary_block_number < last_n_blocks` → `50000 - 0 < 10` → **false**. The adjustment that would clamp the boundary is skipped.

**Step 5 — `last_n_numbers` collects the entire chain.** [6](#0-5) 

`(0..50000).collect::<Vec<_>>()` produces 50,000 entries.

**Step 6 — `complete_headers` performs O(N) MMR root computations.** [7](#0-6) 

For every one of the N entries, `chain_root_mmr(*number - 1).get_root()` is called, plus `get_ancestor` and `get_block` lookups. This is unbounded I/O and CPU work, proportional to chain length.

---

### Impact Explanation

A single unprivileged light-client peer can send one crafted `GetLastStateProof` message to force the server to iterate and compute MMR roots for every block in the chain. On a mainnet-length chain (hundreds of thousands of blocks), this exhausts server CPU and storage I/O. Multiple concurrent peers amplify the effect. The `GET_LAST_STATE_PROOF_LIMIT=1000` invariant is completely ineffective against this input.

---

### Likelihood Explanation

The attack requires no credentials, no PoW, and no special state — only a valid `last_hash` pointing to a main-chain tip (publicly observable). The malformed field values (`difficulty_boundary=0`, `difficulties=[]`) pass all existing validation checks. Any peer connected to the light-client protocol server can trigger this.

---

### Recommendation

After computing `last_n_numbers` (and `sampled_numbers`), add an explicit length check before calling `complete_headers`:

```rust
if last_n_numbers.len() + sampled_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, reject requests where `difficulty_boundary` is zero or less than the total difficulty of `start_block_number`, since a zero boundary is semantically meaningless and only serves to collapse the sampling range.

---

### Proof of Concept

```
MockChain: 50,000 blocks
Message: GetLastStateProof {
    last_hash:          tip_hash,
    start_number:       0,
    start_hash:         genesis_hash,
    last_n_blocks:      10,
    difficulty_boundary: U256::zero(),
    difficulties:       [],
}
```

Expected (buggy) behavior: `complete_headers` is invoked with a 50,000-entry `block_numbers` slice; execution time and I/O grow linearly with chain length. The limit check at line 201–205 passes with a value of 20, never reaching 1000. [8](#0-7)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L29-36)
```rust
    ) -> Option<(BlockNumber, U256)> {
        if let Some(start_total_difficulty) = self.get_block_total_difficulty(start_block_number) {
            if start_total_difficulty >= *min_total_difficulty {
                return Some((start_block_number, start_total_difficulty));
            }
        } else {
            return None;
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L150-163)
```rust
                let parent_chain_root = if *number == 0 {
                    Default::default()
                } else {
                    let mmr = self.snapshot.chain_root_mmr(*number - 1);
                    match mmr.get_root() {
                        Ok(root) => root,
                        Err(err) => {
                            let errmsg = format!(
                                "failed to generate a root for block#{number} since {err:?}"
                            );
                            return Err(errmsg);
                        }
                    }
                };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-205)
```rust
    pub(crate) async fn execute(self) -> Status {
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L313-316)
```rust
            if last_block_number - difficulty_boundary_block_number < last_n_blocks {
                // There is not enough blocks after the difficulty boundary, so we take more.
                difficulty_boundary_block_number = last_block_number - last_n_blocks;
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
