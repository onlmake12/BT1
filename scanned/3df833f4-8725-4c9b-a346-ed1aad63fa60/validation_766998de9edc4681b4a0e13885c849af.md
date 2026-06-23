### Title
Integer Overflow in `GetLastStateProof` Limit Guard Enables Unbounded O(chain_length) CPU Work — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can craft a `GetLastStateProof` P2P message with `last_n_blocks = 0x8000000000000000` (i.e., `usize::MAX/2 + 1` on 64-bit). In Rust release mode, the guard expression `(last_n_blocks as usize) * 2` wraps to `0`, bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` check. The original `u64` value then flows into the reorg path, causing `reorg_last_n_numbers` to span the entire chain. `complete_headers` is subsequently called with O(chain_length) block numbers, each requiring an MMR root computation, with no further bound.

---

### Finding Description

**Root cause — integer overflow at the limit guard:** [1](#0-0) 

`last_n_blocks` is decoded as `u64` from the wire message. On a 64-bit target, `usize` is also 64 bits. When `last_n_blocks = 0x8000000000000000`, the expression `(last_n_blocks as usize) * 2` overflows to `0` in Rust release mode (two's-complement wrapping; no panic). With `difficulties.len() = 0`, the condition `0 + 0 > 1000` is `false`, so the guard returns without banning and execution continues. [2](#0-1) 

**Unbounded `reorg_last_n_numbers` range:** [3](#0-2) 

The attacker sets `start_hash` to a value that does not match the ancestor of `last_hash` at `start_block_number`, forcing the `else` branch. Here `last_n_blocks` is still the original `u64` value `0x8000000000000000`. Because `min(start_block_number, 0x8000000000000000) = start_block_number` for any realistic chain height, `min_block_number` becomes `0`, and `reorg_last_n_numbers = (0..start_block_number)` — the entire chain history.

**O(chain_length) MMR work in `complete_headers`:** [4](#0-3) 

For every block number in `reorg_last_n_numbers`, `complete_headers` calls `self.snapshot.chain_root_mmr(*number - 1).get_root()`, computing a full MMR root. With a chain of height N, this is N MMR root computations per single crafted message.

---

### Impact Explanation

A single crafted `GetLastStateProof` message causes the full node to perform O(chain_length) MMR root computations synchronously inside the light-client protocol handler. Multiple peers sending this message concurrently exhaust CPU and memory, stalling or crashing the full node and all connected light clients. No authentication or PoW is required.

---

### Likelihood Explanation

The attack requires only a valid `last_hash` on the main chain (trivially obtained from any public node) and a crafted `last_n_blocks` value. The overflow value `0x8000000000000000` is a single fixed constant. Any peer that can open a light-client protocol connection can trigger this.

---

### Recommendation

Replace the plain `*` and `+` with overflow-safe arithmetic at the guard:

```rust
// Instead of:
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT

// Use:
if (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len())
    > constant::GET_LAST_STATE_PROOF_LIMIT
```

Additionally, `reorg_last_n_numbers` should be independently bounded before allocation:

```rust
let reorg_count = start_block_number.saturating_sub(min_block_number);
if reorg_count > constant::GET_LAST_STATE_PROOF_LIMIT as u64 {
    return StatusCode::MalformedProtocolMessage.with_context("reorg range too large");
}
```

---

### Proof of Concept

```
last_hash        = <any valid chain-tip hash>
start_hash       = <any 32-byte value NOT equal to the ancestor at start_number>
start_number     = <chain_tip_number>          // maximises reorg_last_n_numbers
last_n_blocks    = 0x8000000000000000          // (last_n_blocks as usize) * 2 == 0
difficulty_boundary = 0x01                     // arbitrary non-zero
difficulties     = []                          // empty; len() == 0
```

Guard evaluation: `0 + 0 > 1000` → `false` → no ban, execution continues.

`reorg_last_n_numbers = (0..chain_tip_number)` → N entries → N MMR root computations in `complete_headers`.

Unit fuzz assertion: for all `last_n_blocks` in `[usize::MAX/2, usize::MAX]`, `(last_n_blocks as usize).saturating_mul(2) > 1000` must hold; the current plain `* 2` fails this for `last_n_blocks = 0x8000000000000000`.

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
        let last_n_blocks: u64 = self.message.last_n_blocks().into();

        if self.message.difficulties().len() + (last_n_blocks as usize) * 2
            > constant::GET_LAST_STATE_PROOF_LIMIT
        {
            return StatusCode::MalformedProtocolMessage.with_context("too many samples");
        }
```

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L237-247)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
