### Title
Integer Overflow in `GET_LAST_STATE_PROOF_LIMIT` Guard Enables Unbounded CPU/Memory DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

In `GetLastStateProofProcess::execute`, the guard that enforces `GET_LAST_STATE_PROOF_LIMIT` contains an integer multiplication that wraps to zero in Rust release builds when `last_n_blocks >= 0x8000000000000000`. Any unauthenticated P2P peer can exploit this to force the server to process the entire chain history per request, with no per-request work bound.

---

### Finding Description

The guard at line 201 is:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

`GET_LAST_STATE_PROOF_LIMIT` is 1000: [2](#0-1) 

**The overflow:** `last_n_blocks` is a `u64` field from the wire message, cast to `usize` (64-bit on x86-64), then multiplied by 2. In Rust **release builds**, integer arithmetic wraps on overflow without panicking (only debug builds panic). Setting `last_n_blocks = 0x8000000000000000` makes `(0x8000000000000000usize) * 2 = 0x10000000000000000`, which wraps to `0`. The check then reduces to `difficulties.len() + 0 > 1000`, which passes for any `difficulties.len() <= 1000`.

**After bypass:** With `start_block_number = 0` and a chain of height H, the condition at line 291:

```rust
if last_block_number - start_block_number <= last_n_blocks {
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
``` [3](#0-2) 

evaluates as `H <= 0x8000000000000000` → always true for any realistic chain height. This collects **all H block numbers** into `last_n_numbers`, completely bypassing the 1000-entry limit.

**Work per request:** `complete_headers` then iterates over all H entries, performing for each:
- `get_ancestor` (chain traversal)
- `get_block` (DB read)
- `chain_root_mmr(*number - 1).get_root()` (MMR root computation, O(log n)) [4](#0-3) 

Then `reply_proof` calls `mmr.gen_proof(items_positions)` with all H positions, which is O(H · log H) work. [5](#0-4) 

**No rate limiting exists.** The `received` handler dispatches directly to `execute` with no per-peer throttling, concurrency cap, or request queue depth limit: [6](#0-5) 

---

### Impact Explanation

A single malicious peer can send repeated `GetLastStateProof` messages with `last_n_blocks = 0x8000000000000000` and `difficulties = []`. Each message forces the full node to:
- Allocate a `Vec<BlockNumber>` of size equal to the entire chain height
- Perform O(chain_height · log(chain_height)) MMR computations
- Read every block from the database

On a chain of height 100,000, this is ~1.7M MMR operations per request. Multiple concurrent peers amplify this to network-wide CPU and memory exhaustion on all light-client-serving full nodes.

---

### Likelihood Explanation

The exploit requires only a valid P2P connection to a light-client-serving full node. No PoW, no keys, no privileged role. The crafted message is trivially constructable. The overflow value `0x8000000000000000` fits in the `Uint64` wire field. The attack is repeatable without ban because the message passes the malformed-message check and returns `Status::ok()` or an `InternalError` (neither of which triggers `ban_peer`).

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Option A: saturating_mul prevents wrap-to-zero
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Or add an explicit upper-bound check on `last_n_blocks` before the arithmetic:

```rust
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

---

### Proof of Concept

```rust
// In a unit test (release mode):
let last_n_blocks: u64 = 0x8000000000000000u64;
let overflowed = (last_n_blocks as usize).wrapping_mul(2);
assert_eq!(overflowed, 0); // wraps to 0 in release

// Craft the message:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_header.hash())
    .start_hash(genesis_header.hash())
    .start_number(0u64)
    .last_n_blocks(0x8000000000000000u64)  // triggers overflow
    .difficulty_boundary(some_valid_boundary)
    .difficulties(packed::Uint256Vec::new_builder().build()) // len=0, passes check
    .build();
// Send to a node with chain height 100_000.
// Observe: server allocates Vec of 100_000 entries and performs
// O(100_000 * log(100_000)) MMR operations per message.
```

### Citations

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

**File:** util/light-client-protocol-server/src/components/get_last_state_proof.rs (L199-205)
```rust
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L55-92)
```rust
    async fn received(
        &mut self,
        nc: Arc<dyn CKBProtocolContext + Sync>,
        peer: PeerIndex,
        data: Bytes,
    ) {
        trace!("LightClient.received peer={}", peer);

        let msg = match packed::LightClientMessageReader::from_slice(&data) {
            Ok(msg) => msg.to_enum(),
            _ => {
                warn!(
                    "LightClient.received a malformed message from Peer({})",
                    peer
                );
                nc.ban_peer(
                    peer,
                    constant::BAD_MESSAGE_BAN_TIME,
                    String::from("send us a malformed message"),
                );
                return;
            }
        };

        let item_name = msg.item_name();
        let status = self.try_process(&nc, peer, msg).await;
        if let Some(ban_time) = status.should_ban() {
            error!(
                "process {} from {}; ban {:?} since result is {}",
                item_name, peer, ban_time, status
            );
            nc.ban_peer(peer, ban_time, status.to_string());
        } else if status.should_warn() {
            warn!("process {} from {}; result is {}", item_name, peer, status);
        } else if !status.is_ok() {
            debug!("process {} from {}; result is {}", item_name, peer, status);
        }
    }
```

**File:** util/light-client-protocol-server/src/lib.rs (L207-217)
```rust
            let proof = if items_positions.is_empty() {
                Default::default()
            } else {
                match mmr.gen_proof(items_positions) {
                    Ok(proof) => proof.proof_items().to_owned(),
                    Err(err) => {
                        let errmsg = format!("failed to generate a proof since {err:?}");
                        return StatusCode::InternalError.with_context(errmsg);
                    }
                }
            };
```
