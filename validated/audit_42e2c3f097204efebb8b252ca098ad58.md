### Title
`GetBlocksProcess` Uses Wrong Limit Constant — Accepts 62× More Block Hashes Than It Processes - (File: sync/src/synchronizer/get_blocks_process.rs)

### Summary

`GetBlocksProcess::execute()` validates incoming `GetBlocks` P2P messages against `MAX_HEADERS_LEN` (2,000) — a constant defined for *header* messages — while the actual processing loop only consumes `INIT_BLOCKS_IN_TRANSIT_PER_PEER` (32) hashes. The correct block-specific upper bound is `MAX_BLOCKS_IN_TRANSIT_PER_PEER` (128). Any unprivileged sync peer can send a `GetBlocks` message carrying 2,000 block hashes, have it pass validation, and force the node to allocate and parse the full oversized payload while only 32 blocks are ever served.

### Finding Description

In `sync/src/synchronizer/get_blocks_process.rs`, `GetBlocksProcess::execute()` performs two separate limit operations on the incoming block-hash list:

```rust
// use MAX_HEADERS_LEN as limit, we may increase the value of INIT_BLOCKS_IN_TRANSIT_PER_PEER in the future
if block_hashes.len() > MAX_HEADERS_LEN {          // accepts up to 2,000
    return StatusCode::ProtocolMessageIsMalformed…;
}
…
let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);  // processes only 32
```

The three relevant constants are defined in `util/constant/src/sync.rs`:

| Constant | Value | Semantic |
|---|---|---|
| `MAX_HEADERS_LEN` | 2,000 | Max hashes in a *SendHeaders* message |
| `INIT_BLOCKS_IN_TRANSIT_PER_PEER` | 32 | Initial block-download window per peer |
| `MAX_BLOCKS_IN_TRANSIT_PER_PEER` | 128 | Maximum block-download window per peer |

`MAX_HEADERS_LEN` is semantically defined for header messages, not block messages. Using it as the validation ceiling for `GetBlocks` creates a 62.5× gap between what is accepted (2,000) and what is processed (32). The code comment itself acknowledges the mismatch is intentional but deferred ("we may increase the value of `INIT_BLOCKS_IN_TRANSIT_PER_PEER` in the future"), leaving the wrong constant in place indefinitely.

This is structurally identical to the Allora M-25 bug: the wrong parameter/constant is used for one of two related limit checks in the same function, causing the accepted input to be far larger than what the processing path actually handles.

### Impact Explanation

A malicious sync peer sends a `GetBlocks` message containing 2,000 block hashes (the maximum accepted). The node:
1. Allocates and holds the full message payload (~64 KB of 32-byte hashes) in memory.
2. Passes the validation check (`2000 > MAX_HEADERS_LEN` is false).
3. Iterates over only 32 hashes, performing up to 32 database lookups and spawning up to 32 async `SendBlock` tasks.
4. Silently discards the remaining 1,968 hashes.

The attacker achieves a 62.5× amplification of accepted-vs-processed work per message. By flooding the node with back-to-back oversized `GetBlocks` messages, the attacker can sustain elevated memory pressure and unnecessary message-parsing overhead on the victim node. Because the node returns `Status::ok()` for every such message (no ban, no disconnect), the attacker is never penalized.

### Likelihood Explanation

The entry path requires only a TCP connection to the CKB sync port — no authentication, no stake, no special role. Any peer that completes the P2P handshake can immediately send `GetBlocks` messages. The malformed payload (2,000 hashes instead of ≤128) passes all existing checks and produces no error response, so the attacker receives no feedback that would cause them to stop. The attack is trivially scriptable and repeatable.

### Recommendation

Replace `MAX_HEADERS_LEN` with `MAX_BLOCKS_IN_TRANSIT_PER_PEER` as the validation ceiling in `GetBlocksProcess::execute()`:

```rust
if block_hashes.len() > MAX_BLOCKS_IN_TRANSIT_PER_PEER {
    return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
        "BlockHashes count({}) > MAX_BLOCKS_IN_TRANSIT_PER_PEER({})",
        block_hashes.len(),
        MAX_BLOCKS_IN_TRANSIT_PER_PEER,
    ));
}
```

This aligns the validation limit with the actual block-download semantics and closes the 62.5× gap between accepted and processed hashes.

### Proof of Concept

1. Connect to a CKB node's sync port and complete the P2P handshake.
2. Construct a `GetBlocks` message (`packed::GetBlocks`) containing exactly 2,000 distinct block hashes (e.g., all zeros with incrementing last bytes).
3. Send the message.
4. Observe: the node accepts the message (`Status::ok()`), does not ban or disconnect the peer, and responds with at most 32 `SendBlock` messages (for any hashes that correspond to valid stored blocks).
5. Repeat in a tight loop. Each iteration forces the node to parse a ~64 KB payload while only doing 32 units of useful work, sustaining a 62.5× work-amplification ratio at no cost to the attacker.

**Root cause lines:** [1](#0-0) 

**Wrong constant definition (headers, not blocks):** [2](#0-1)

### Citations

**File:** sync/src/synchronizer/get_blocks_process.rs (L35-45)
```rust
        // use MAX_HEADERS_LEN as limit, we may increase the value of INIT_BLOCKS_IN_TRANSIT_PER_PEER in the future
        if block_hashes.len() > MAX_HEADERS_LEN {
            return StatusCode::ProtocolMessageIsMalformed.with_context(format!(
                "BlockHashes count({}) > MAX_HEADERS_LEN({})",
                block_hashes.len(),
                MAX_HEADERS_LEN,
            ));
        }
        let active_chain = self.synchronizer.shared.active_chain();

        let iter = block_hashes.iter().take(INIT_BLOCKS_IN_TRANSIT_PER_PEER);
```

**File:** util/constant/src/sync.rs (L7-16)
```rust
/// Default max get header response length, if it is greater than this value, the message will be ignored
pub const MAX_HEADERS_LEN: usize = 2_000;

// The default number of download blocks that can be requested at one time
/* About Download Scheduler */

/// ckb2021 edition new limit
pub const INIT_BLOCKS_IN_TRANSIT_PER_PEER: usize = 32;
/// Maximum number of download blocks that can be requested at one time
pub const MAX_BLOCKS_IN_TRANSIT_PER_PEER: usize = 128;
```
