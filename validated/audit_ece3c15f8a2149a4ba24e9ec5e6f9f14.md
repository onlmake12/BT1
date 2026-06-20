### Title
Unbounded O(chain_height) Work Per Unauthenticated `GetLastStateProof` Request via `last_n_blocks=0` and Genesis `difficulty_boundary` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The `GET_LAST_STATE_PROOF_LIMIT` guard in `GetLastStateProofProcess::execute` is structurally bypassed when `last_n_blocks=0` and `difficulties` is empty. Under these conditions, setting `difficulty_boundary` to the genesis block's total difficulty causes `last_n_numbers` to expand to the entire chain `(0..chain_height)`, and `complete_headers()` then performs one `get_ancestor` DB read, one `get_block` DB read, and one `chain_root_mmr().get_root()` MMR computation per block — all O(chain_height) — for a single unauthenticated P2P request.

---

### Finding Description

**Entry point:** Any peer connecting via P2P can send a `GetLastStateProof` message. The handler is registered in `LightClientProtocol::received()` → `try_process()` → `GetLastStateProofProcess::execute()`. [1](#0-0) 

**Guard (lines 201–205):**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [2](#0-1) 

With `last_n_blocks=0` and `difficulties=[]`, this evaluates to `0 + 0 > 1000` = **false**. The guard is completely bypassed.

**`last_n_numbers` expansion (lines 291–348):**

The condition `last_block_number - start_block_number <= last_n_blocks` becomes `H <= 0`, which is false for any non-genesis chain. Execution falls into the else branch. With `difficulty_boundary` equal to the genesis block's total difficulty, `get_first_block_total_difficulty_is_not_less_than` returns block 0 as `difficulty_boundary_block_number`. The subsequent check `last_block_number - 0 < 0` is also false, so:

```rust
let last_n_numbers =
    (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
// = (0..H) — the entire chain
``` [3](#0-2) 

Because `difficulty_boundary_block_number == 0`, the `if difficulty_boundary_block_number > 0` branch is skipped, yielding `(Vec::new(), last_n_numbers)` — all H block numbers, no sampling. [4](#0-3) 

**`complete_headers()` cost (lines 132–177):**

For every block number in `block_numbers` (all H of them):

1. `snapshot.get_ancestor(last_hash, *number)` — DB read
2. `snapshot.get_block(&ancestor_header.hash())` — DB read
3. `snapshot.chain_root_mmr(*number - 1).get_root()` — MMR traversal over DB [5](#0-4) 

`chain_root_mmr` constructs an MMR of size `leaf_index_to_mmr_size(number)` backed by the store snapshot, and `get_root()` reads and merges O(log H) nodes from the DB per call. [6](#0-5) 

---

### Impact Explanation

A single malformed but structurally valid P2P message forces the server to perform O(chain_height) DB reads and O(chain_height × log(chain_height)) MMR node reads. On a mainnet node with hundreds of thousands of blocks, this saturates I/O and CPU on the async handler thread. Repeated at high frequency from one or more peers, this constitutes a practical remote DoS against any CKB full node running the light client protocol server.

---

### Likelihood Explanation

The attack requires no authentication, no PoW, no keys, and no special peer status. The attacker only needs to:
1. Connect to the light client P2P port.
2. Obtain the current tip hash (freely available via `GetLastState`).
3. Send a single crafted `GetLastStateProof` message.

The genesis block's total difficulty is a public constant. The message is structurally valid and passes all format checks.

---

### Recommendation

The guard must account for the actual number of blocks that will be processed, not just the input sizes. Specifically:

- Compute `last_n_numbers.len()` before calling `complete_headers()` and reject if it exceeds `GET_LAST_STATE_PROOF_LIMIT`.
- Alternatively, enforce a minimum value for `last_n_blocks` (e.g., ≥ 1) so the guard `difficulties.len() + last_n_blocks * 2` is always meaningful, and separately cap `last_n_numbers` to `last_n_blocks` entries.
- The guard should be: `sampled_numbers.len() + last_n_numbers.len() + reorg_last_n_numbers.len() > GET_LAST_STATE_PROOF_LIMIT`. [7](#0-6) 

---

### Proof of Concept

```
1. Start a CKB node with the light client protocol server enabled.
2. Mine or sync a chain of height H = 10000.
3. Query GetLastState to obtain the current tip hash T.
4. Send GetLastStateProof with:
     last_hash        = T
     start_hash       = genesis_hash
     start_number     = 0
     last_n_blocks    = 0
     difficulties     = []
     difficulty_boundary = genesis_total_difficulty  (e.g., 0x1 or the actual genesis value)
5. Observe: the server calls complete_headers() with block_numbers = [0, 1, ..., 9999].
6. Measure: processing time scales linearly with H.
7. Repeat at 10 req/s; observe node I/O saturation.
```

### Citations

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L132-163)
```rust
        for number in numbers {
            if let Some(ancestor_header) = self.snapshot.get_ancestor(last_hash, *number) {
                let position = leaf_index_to_pos(*number);
                positions.push(position);

                let ancestor_block = self
                    .snapshot
                    .get_block(&ancestor_header.hash())
                    .ok_or_else(|| {
                        format!(
                            "failed to find block for header#{} (hash: {:#x})",
                            number,
                            ancestor_header.hash()
                        )
                    })?;
                let uncles_hash = ancestor_block.calc_uncles_hash();
                let extension = ancestor_block.extension();

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L318-319)
```rust
            let last_n_numbers =
                (difficulty_boundary_block_number..last_block_number).collect::<Vec<_>>();
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L345-347)
```rust
            } else {
                (Vec::new(), last_n_numbers)
            }
```

**File:** util/snapshot/src/lib.rs (L181-184)
```rust
    pub fn chain_root_mmr(&self, block_number: BlockNumber) -> ChainRootMMR<&Self> {
        let mmr_size = leaf_index_to_mmr_size(block_number);
        ChainRootMMR::new(mmr_size, self)
    }
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
