The key finding is in `Cargo.toml` at line 318-319:

```toml
[profile.release]
overflow-checks = true
```

CKB explicitly enables overflow checks in its **release profile**. This is not the default Rust behavior. Combined with the arithmetic at line 201, this is a real, production-reachable vulnerability.

---

### Title
Attacker-Controlled `last_n_blocks=u64::MAX` Causes Arithmetic Overflow Panic in `GetLastStateProofProcess::execute` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

### Summary

An unprivileged remote peer can send a `GetLastStateProof` P2P message with `last_n_blocks = u64::MAX`, triggering an integer overflow panic at the bounds-check guard in `GetLastStateProofProcess::execute`. Because CKB's release profile explicitly sets `overflow-checks = true`, this panic fires in production builds, not only in debug mode.

### Finding Description

In `execute`, the guard intended to reject oversized requests is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

On a 64-bit target, `u64::MAX as usize = usize::MAX`. The sub-expression `usize::MAX * 2` overflows. Normally in Rust release builds this would wrap silently, but CKB's workspace `Cargo.toml` explicitly opts into overflow panics for the release profile:

```toml
[profile.release]
overflow-checks = true
``` [2](#0-1) 

The `prod` profile inherits from `release` and therefore also carries `overflow-checks = true`:

```toml
[profile.prod]
inherits = "release"
``` [3](#0-2) 

`GET_LAST_STATE_PROOF_LIMIT` is `1000`, a small constant that is irrelevant because the panic fires before the comparison is evaluated. [4](#0-3) 

The message is dispatched directly from the protocol handler with no prior sanitization of `last_n_blocks`: [5](#0-4) 

### Impact Explanation

The panic unwinds the async task executing `GetLastStateProofProcess::execute`. Depending on how Tokio handles the panic (task abort vs. process abort), the minimum impact is a crash of the light-client protocol handler task, disconnecting all subscribed light clients. If the panic propagates to the Tokio runtime thread, it can crash the entire node process.

### Likelihood Explanation

Any peer that can open a light-client protocol connection can send this message. No authentication, PoW, or privileged role is required. The `last_n_blocks` field is a plain `Uint64` in the molecule schema with no range restriction enforced before `execute` is called. [6](#0-5) 

### Recommendation

Replace the unchecked multiplication with a saturating or checked operation before the comparison:

```rust
let sample_count = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if sample_count > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject `last_n_blocks` values that exceed `GET_LAST_STATE_PROOF_LIMIT / 2` before any arithmetic is performed.

### Proof of Concept

1. Connect to a CKB node with the light-client protocol enabled (compiled with `--release` or `--profile prod`).
2. Craft a `GetLastStateProof` molecule message with `last_n_blocks = 0xFFFFFFFFFFFFFFFF` and any valid-looking `last_hash`, `start_hash`, `start_number`, `difficulty_boundary`, and empty `difficulties`.
3. Send the message over the P2P connection.
4. Observe the node panic at `util/light-client-protocol-server/src/components/get_last_state_proof.rs:201` with `attempt to multiply with overflow`.

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

**File:** Cargo.toml (L318-319)
```text
[profile.release]
overflow-checks = true
```

**File:** Cargo.toml (L327-329)
```text
[profile.prod]
inherits = "release"
lto = true
```

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```

**File:** util/light-client-protocol-server/src/lib.rs (L108-112)
```rust
            packed::LightClientMessageUnionReader::GetLastStateProof(reader) => {
                components::GetLastStateProofProcess::new(reader, self, peer_index, nc)
                    .execute()
                    .await
            }
```

**File:** util/gen-types/schemas/extensions.mol (L324-342)
```text
table GetLastStateProof {
    // The last block hash known by the client.
    // It could be different with the tip hash in the server.
    last_hash:                  Byte32,

    // The hash of the last proved block.
    start_hash:                 Byte32,
    // The block number of the last proved block.
    start_number:               Uint64,

    // How many continuous blocks before the tip block should be included at
    // least, if possible?
    last_n_blocks:              Uint64,
    // All blocks, whose total difficulty is not less than this difficulty
    // boundary, should be included in the proof.
    difficulty_boundary:        Uint256,
    // The sampled difficulties.
    difficulties:               Uint256Vec,
}
```
