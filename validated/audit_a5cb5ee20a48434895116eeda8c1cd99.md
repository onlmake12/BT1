### Title
Integer Overflow Bypasses `GET_LAST_STATE_PROOF_LIMIT`, Enabling Unbounded Chain Scan DoS — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged P2P peer can send a crafted `GetLastStateProof` message with `last_n_blocks = 2^63` to trigger a Rust integer overflow in the guard expression at line 201, wrapping the computed limit to `0` in release mode and bypassing the `GET_LAST_STATE_PROOF_LIMIT = 1000` cap. The server then iterates over the entire chain history — up to millions of blocks on mainnet — performing per-block DB lookups and MMR root computations, with no per-peer rate limiting.

---

### Finding Description

The guard in `GetLastStateProofProcess::execute()` is:

```rust
// get_last_state_proof.rs, line 199-205
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

In Rust's release profile, integer overflow wraps silently (overflow-checks defaults to `false`). With `last_n_blocks = 0x8000_0000_0000_0000` (2^63):

- `last_n_blocks as usize` = `0x8000_0000_0000_0000` (on 64-bit)
- `* 2` wraps to `0`
- `0 + difficulties.len() > 1000` → **false** if `difficulties` is empty

The guard is bypassed. Execution continues to:

```rust
// lines 291-297
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks   // 2^63 — always true for any real chain
{
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    ...
``` [2](#0-1) 

`last_n_numbers` now spans the entire chain from `start_block_number` to `last_block_number`. `complete_headers` is then called for every block number in that range:

```rust
// lines 356-366
let headers =
    match sampler.complete_headers(&mut positions, &last_block_hash, &block_numbers) {
``` [3](#0-2) 

Each iteration of `complete_headers` performs:
- `snapshot.get_ancestor(last_hash, number)` — chain traversal
- `snapshot.get_block(hash)` — DB read
- `snapshot.chain_root_mmr(number - 1).get_root()` — MMR root computation [4](#0-3) 

There is no per-peer rate limiting anywhere in the protocol handler: [5](#0-4) 

The only ban mechanism fires for unparseable binary messages, not for semantically crafted ones. [6](#0-5) 

---

### Impact Explanation

A single crafted `GetLastStateProof` message causes the full-node server to scan and compute MMR roots for every block from `start_block_number` to the chain tip. On mainnet (millions of blocks), this is O(chain_height) CPU, memory, and storage I/O per request. Multiple peers sending this concurrently can exhaust server resources, causing denial of service to legitimate light clients and potentially to the full node itself.

---

### Likelihood Explanation

Any P2P peer can send this message. The binary packed format is straightforward to craft. No authentication, stake, or special role is required. The overflow is deterministic and reproducible on any 64-bit release build.

---

### Recommendation

Replace the overflow-prone expression with a saturating or checked multiplication:

```rust
// Safe version
let last_n_blocks_doubled = (last_n_blocks as usize).saturating_mul(2);
if self.message.difficulties().len() + last_n_blocks_doubled
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Additionally, add a hard cap on `last_n_blocks` itself before any arithmetic, and consider per-peer request rate limiting in `received()`. [7](#0-6) 

---

### Proof of Concept

1. Connect to a full node as a light-client P2P peer.
2. Send a `GetLastStateProof` packed message with:
   - `last_n_blocks = 0x8000000000000000` (2^63)
   - `difficulties = []` (empty)
   - `last_hash` = any valid main-chain block hash
   - `start_number = 0`
3. Observe: the limit check at line 201 evaluates `0 + 0 > 1000 = false`, passes.
4. The server collects `(0..last_block_number)` as `last_n_numbers` and calls `complete_headers` for every block in the chain.
5. CPU and I/O spike proportional to chain height; repeat from multiple peers to exhaust the node.

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

**File:** util/light-client-protocol-server/src/lib.rs (L55-93)
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
}
```

**File:** util/light-client-protocol-server/src/constant.rs (L1-7)
```rust
use std::time::Duration;

pub const BAD_MESSAGE_BAN_TIME: Duration = Duration::from_secs(5 * 60);

pub const GET_BLOCKS_PROOF_LIMIT: usize = 1000;
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
pub const GET_TRANSACTIONS_PROOF_LIMIT: usize = 1000;
```
