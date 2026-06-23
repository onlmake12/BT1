### Title
Integer Overflow in Guard Check Bypasses Rate Limit, Enabling Unbounded Chain-Wide DB Reads — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

In `GetLastStateProofProcess::execute`, the sole rate-limiting guard uses the expression `(last_n_blocks as usize) * 2`, which wraps to a small value in release mode when `last_n_blocks` exceeds `usize::MAX / 2`. This allows an unprivileged remote peer to bypass the guard entirely and force the server to collect and process every block from `start_block_number` to the chain tip — O(chain length) DB reads and memory allocation — instead of the intended O(1000) limit.

---

### Finding Description

**Guard check (lines 201–205):**

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

On a 64-bit host, `usize` is 64 bits. The cast `last_n_blocks as usize` is a no-op. In Rust **release mode**, integer arithmetic uses wrapping semantics (no panic). Therefore:

```
last_n_blocks = 0x8000_0000_0000_0001
(last_n_blocks as usize) * 2  →  0x0000_0000_0000_0002  (wraps)
difficulties.len() + 2 > 1000  →  false  ← guard silently passes
```

**Downstream allocation (lines 291–297):**

```rust
let (sampled_numbers, last_n_numbers) = if last_block_number - start_block_number
    <= last_n_blocks
{
    let last_n_numbers = (start_block_number..last_block_number).collect::<Vec<_>>();
    ...
``` [2](#0-1) 

Because `last_n_blocks = 0x8000_0000_0000_0001` is astronomically large, the condition `last_block_number - start_block_number <= last_n_blocks` is **always true** for any realistic chain. The `if` branch is always taken, and `last_n_numbers` collects **every block number** from `start_block_number` to `last_block_number`.

**Without the overflow**, a legitimate `last_n_blocks = 499` on a long chain would take the `else` branch, where `last_n_numbers` is bounded to at most `last_n_blocks` (≤ 499) elements. The guard correctly limits work to O(1000). **With the overflow**, the `if` branch is forced, and the server processes the entire chain.

The `complete_headers` call then performs one DB read per collected block number: [3](#0-2) 

`GET_LAST_STATE_PROOF_LIMIT` is defined as 1000: [4](#0-3) 

---

### Impact Explanation

An attacker sets `last_hash` to the current tip (publicly known), `start_number = 0`, `start_hash` to genesis (publicly known), and `last_n_blocks = 0x8000_0000_0000_0001`. The server:

1. Bypasses the only rate-limiting guard.
2. Allocates a `Vec<BlockNumber>` with `last_block_number` entries (e.g., ~10 million on mainnet → ~80 MB per request).
3. Performs `last_block_number` sequential DB reads in `complete_headers`.

This can be repeated by any peer, causing memory exhaustion and I/O saturation on the light client server node.

---

### Likelihood Explanation

- No authentication or privilege is required; any P2P peer can send a `GetLastStateProof` message.
- The entry path `received → try_process → execute` is confirmed in `lib.rs` lines 55–92 and 108–112. [5](#0-4) [6](#0-5) 

- The only prerequisite is knowing a valid main-chain block hash for `last_hash`, which is trivially obtained from any public block explorer or by syncing.
- Rust release builds use wrapping arithmetic; this is not a theoretical concern.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant, or validate `last_n_blocks` against the limit before the combined expression:

```rust
// Option 1: checked arithmetic
let combined = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if combined > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}

// Option 2: early bound check
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

---

### Proof of Concept

```rust
// Attacker constructs this message:
let content = packed::GetLastStateProof::new_builder()
    .last_hash(tip_hash)                          // any known main-chain tip
    .start_hash(genesis_hash)
    .start_number(0u64)
    .last_n_blocks(0x8000_0000_0000_0001u64)      // triggers overflow in guard
    .difficulty_boundary(some_valid_boundary)
    // difficulties: empty vec (len = 0)
    .build();
// Guard: 0 + (0x8000_0000_0000_0001usize * 2) = 2 > 1000 → false → passes
// Server then executes: (0..last_block_number).collect() → millions of elements
// Followed by: complete_headers iterates all, doing one DB read per block
```

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

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```
