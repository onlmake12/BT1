### Title
Remote Panic via Integer Overflow in `GetLastStateProofProcess::execute` Limit Check — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

An unprivileged remote peer can crash a CKB node with the light-client protocol enabled by sending a single crafted `GetLastStateProof` P2P message with `last_n_blocks` set to any value greater than `usize::MAX / 2`. The limit-check expression `(last_n_blocks as usize) * 2` overflows `usize`, and because the workspace `Cargo.toml` explicitly sets `overflow-checks = true` for the release profile, this overflow panics in **both debug and release builds**, including the production `prod` profile.

---

### Finding Description

In `GetLastStateProofProcess::execute`, the very first guard against oversized requests is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

`last_n_blocks` is a `u64` field decoded directly from the peer-supplied molecule message with no prior bounds check. On a 64-bit host, `last_n_blocks as usize` is a lossless cast, so the multiplication `* 2` overflows `usize` whenever `last_n_blocks > usize::MAX / 2` (i.e., `> 2^63 - 1`). The attacker simply sets `last_n_blocks = 2^63` (a perfectly valid `u64`).

The workspace `Cargo.toml` sets:

```toml
[profile.release]
overflow-checks = true
``` [2](#0-1) 

The `prod` profile inherits from `release` and therefore also carries `overflow-checks = true`. [3](#0-2) 

This means the standard Rust "wrap silently in release" escape hatch does **not** apply here. The overflow is a hard panic in every build configuration shipped by this project.

The constant being guarded is `GET_LAST_STATE_PROOF_LIMIT = 1000`. [4](#0-3) 

---

### Impact Explanation

A panic inside an `async fn` that is directly `.await`-ed (not spawned as an independent task) propagates up the call stack. The `execute()` method is called synchronously within the protocol's `received` handler via `try_process`. A panic here will unwind through the Tokio task servicing that peer connection. Depending on whether the runtime catches the panic, the result is either termination of the connection-handling task or a full process crash. Either way, the node's light-client protocol service is disrupted for all peers, constituting a remote denial-of-service.

---

### Likelihood Explanation

- No authentication or prior state is required; any peer that can open a connection and speak the light-client protocol can send this message.
- The crafted field value (`last_n_blocks = 2^63`) is a single integer in a molecule-encoded message — trivial to construct.
- The node only needs to have the light-client protocol enabled (a supported, documented deployment mode).
- The panic fires at the very first line of `execute()`, before any chain state is consulted, so no specific chain height or history is required.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before comparing against the limit:

```rust
// Option A: saturating_mul prevents overflow, value exceeds limit naturally
if self.message.difficulties().len()
    + (last_n_blocks as usize).saturating_mul(2)
    > constant::GET_LAST_STATE_PROOF_LIMIT
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject the message immediately if `last_n_blocks > GET_LAST_STATE_PROOF_LIMIT as u64` before any arithmetic is performed.

---

### Proof of Concept

```rust
// Pseudocode: attacker constructs and sends this message
let malicious = packed::GetLastStateProof::new_builder()
    .last_hash(any_valid_main_chain_hash)
    .start_hash(genesis_hash)
    .start_number(0u64)
    .last_n_blocks((usize::MAX / 2 + 1) as u64)  // = 2^63 on 64-bit
    .difficulty_boundary(U256::zero())
    .difficulties(vec![])  // empty, so difficulties.len() = 0
    .build();
// Server receives this, enters execute(), evaluates:
//   0 + (2^63 as usize) * 2  →  overflow → panic (overflow-checks = true)
```

The unit-test equivalent:

```rust
#[test]
fn limit_check_does_not_overflow() {
    let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64;
    // This panics today:
    let _ = (last_n_blocks as usize).checked_mul(2).unwrap();
}
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
