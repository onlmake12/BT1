Audit Report

## Title
Attacker-Controlled `last_n_blocks=u64::MAX` Causes Arithmetic Overflow Panic in `GetLastStateProofProcess::execute` — (File: util/light-client-protocol-server/src/components/get_last_state_proof.rs)

## Summary
An unprivileged remote peer can send a `GetLastStateProof` P2P message with `last_n_blocks = u64::MAX`, triggering an integer overflow panic at line 201 of `get_last_state_proof.rs`. CKB's workspace `Cargo.toml` explicitly sets `overflow-checks = true` for the release profile, meaning this panic fires in production builds. The result is a remotely-triggerable node crash.

## Finding Description
In `GetLastStateProofProcess::execute`, the guard at line 201 is:

```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT
``` [1](#0-0) 

On a 64-bit target, `u64::MAX as usize = usize::MAX`. The sub-expression `usize::MAX * 2` overflows before the comparison is ever evaluated. Normally in Rust release builds this would wrap silently, but the workspace `Cargo.toml` explicitly opts into overflow panics:

```toml
[profile.release]
overflow-checks = true
``` [2](#0-1) 

The `prod` profile inherits from `release` and therefore also carries `overflow-checks = true`: [3](#0-2) 

`GET_LAST_STATE_PROOF_LIMIT` is `1000`, a small constant that is irrelevant because the panic fires before the comparison is evaluated: [4](#0-3) 

The message is dispatched directly from the protocol handler with no prior sanitization of `last_n_blocks`: [5](#0-4) 

## Impact Explanation
The panic unwinds the async task executing `GetLastStateProofProcess::execute`. Depending on Tokio's panic handling configuration, the minimum impact is a crash of the light-client protocol handler task. If the panic propagates to the Tokio runtime thread, it crashes the entire node process. This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.** Any peer with a light-client protocol connection can crash the node repeatedly with a single malformed message.

## Likelihood Explanation
No authentication, proof-of-work, or privileged role is required. The `last_n_blocks` field is a plain `Uint64` in the molecule schema with no range restriction enforced before `execute` is called. Any peer that can open a light-client protocol connection can trigger this. The attack is trivially repeatable.

## Recommendation
Replace the unchecked multiplication with saturating arithmetic before the comparison:

```rust
let sample_count = (last_n_blocks as usize)
    .saturating_mul(2)
    .saturating_add(self.message.difficulties().len());
if sample_count > constant::GET_LAST_STATE_PROOF_LIMIT {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
```

Alternatively, reject `last_n_blocks` values exceeding `GET_LAST_STATE_PROOF_LIMIT / 2` before any arithmetic is performed.

## Proof of Concept
1. Connect to a CKB node with the light-client protocol enabled, compiled with `--release` or `--profile prod`.
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
