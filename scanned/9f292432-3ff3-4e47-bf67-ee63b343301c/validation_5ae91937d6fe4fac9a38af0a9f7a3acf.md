The vulnerability claim is valid. Here is the full analysis:

---

### Title
Unbounded `last_n_numbers` Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling O(N log N) DB Read Amplification per Request — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

The limit guard in `GetLastStateProofProcess::execute` checks `difficulties.len() + last_n_blocks * 2` against `GET_LAST_STATE_PROOF_LIMIT` (1000), assuming `last_n_numbers.len() ≤ last_n_blocks`. This assumption is false. When an attacker sets `difficulty_boundary` to the total difficulty of block 1 and `last_n_blocks=1`, the computed `last_n_numbers` spans `(1..last_block_number)` — N-1 entries — completely bypassing the limit. Every entry then triggers O(log N) DB reads via `chain_root_mmr`, yielding O(N log N) DB work per single unauthenticated P2P request.

### Finding Description

**Limit check (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

With attacker-supplied `difficulties=[]` and `last_n_blocks=1`: `0 + 1×2 = 2`, which is not `> 1000`. The guard passes. [1](#0-0) 

**Outer branch (lines 291–292):** For a chain of N >> 1 blocks with `start_block_number=0`:

```
N - 0 <= 1  →  false  →  enters the else branch
``` [2](#0-1) 

**`difficulty_boundary_block_number` resolution (lines 299–311):** `get_first_block_total_difficulty_is_not_less_than(0, N, total_difficulty_of_block_1)` returns block 1, so `difficulty_boundary_block_number = 1`. [3](#0-2) 

**Adjustment check (lines 313–316):**

```
N - 1 < 1  →  false  →  no adjustment; difficulty_boundary_block_number stays at 1
``` [4](#0-3) 

**`last_n_numbers` construction (lines 318–319):**

```rust
let last_n_numbers = (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (1..N)  →  N-1 entries
``` [5](#0-4) 

**`complete_headers` cost (lines 150–163):** For every entry in `last_n_numbers`, `chain_root_mmr(number - 1).get_root()` is called, each requiring O(log N) DB reads. Total: O(N log N) DB reads per request. [6](#0-5) 

### Impact Explanation

A single malformed `GetLastStateProof` P2P message causes the server to perform O(N log N) database reads, where N is the current chain height. On mainnet (currently ~13 million blocks), this is catastrophic per request. Multiple concurrent requests from different peers would saturate I/O and stall the node's light-client serving thread entirely, constituting a practical DoS against any node running the light-client protocol server.

### Likelihood Explanation

The attack requires no credentials, no PoW, and no prior state. Any peer that can send a `GetLastStateProof` message can trigger it. The attacker only needs to know the total difficulty of block 1 (publicly available from any block explorer or by querying the node itself). The message is structurally valid and passes all format checks.

### Recommendation

Replace the limit check with a post-computation size check on the actual `block_numbers` vector before calling `complete_headers`:

```rust
let block_numbers = reorg_last_n_numbers
    .into_iter()
    .chain(sampled_numbers)
    .chain(last_n_numbers)
    .collect::<Vec<_>>();

if block_numbers.len() > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, the pre-check at line 201 should be tightened: `last_n_numbers.len()` is bounded by `last_block_number - difficulty_boundary_block_number`, not by `last_n_blocks`. The pre-check formula is fundamentally wrong and should be removed or replaced with the post-computation check above. [7](#0-6) 

### Proof of Concept

```
1. Spin up a CKB node with light-client protocol enabled, synced to height N (e.g., N=10,000).
2. Query block 1's total_difficulty (call it D1).
3. Send a GetLastStateProof message with:
     - last_hash     = tip block hash
     - start_hash    = genesis hash
     - start_number  = 0
     - difficulty_boundary = D1
     - last_n_blocks = 1
     - difficulties  = []
4. Observe: the server constructs last_n_numbers = (1..N), calls complete_headers
   for all N-1 blocks, each invoking chain_root_mmr(k-1).get_root().
5. Differential test: compare wall-clock response time for difficulty_boundary=D1
   vs difficulty_boundary=D[N-1]. The former should be ~N× slower, confirming
   O(N log N) vs O(log N) DB work.
```

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L299-311)
```rust
            let mut difficulty_boundary_block_number = if let Some((num, _)) = sampler
                .get_first_block_total_difficulty_is_not_less_than(
                    start_block_number,
                    last_block_number,
                    &difficulty_boundary,
                ) {
                num
            } else {
                let errmsg = format!(
                    "the difficulty boundary ({difficulty_boundary:#x}) is not in the block range [{start_block_number}, {last_block_number})"
                );
                return StatusCode::InvaildDifficultyBoundary.with_context(errmsg);
            };
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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L350-366)
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
            (positions, headers)
        };
```
