### Title
Remote Peer Panic via Integer Overflow in `GetLastStateProofProcess::execute` Limit Check — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

---

### Summary

The limit guard at line 201 of `GetLastStateProofProcess::execute` computes `(last_n_blocks as usize) * 2` without overflow protection. Because the workspace `Cargo.toml` sets `overflow-checks = true` for the release profile, this multiplication panics — not just in debug builds but in every production binary — whenever a remote peer sends a `GetLastStateProof` message with `last_n_blocks > usize::MAX / 2`. The LightClient protocol is included in the default `support_protocols` list, so any node running the default configuration is reachable.

---

### Finding Description

`GetLastStateProofProcess::execute` reads `last_n_blocks` as a `u64` from the peer-supplied message and immediately uses it in an arithmetic expression meant to enforce a size limit:

```rust
let last_n_blocks: u64 = self.message.last_n_blocks().into();

if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // 1000
{
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

On a 64-bit target `usize` is 64 bits, so `last_n_blocks as usize` is a no-op cast. If the peer sets `last_n_blocks` to any value greater than `usize::MAX / 2` (i.e., ≥ `9_223_372_036_854_775_808`), the multiplication `* 2` overflows a `usize`.

The workspace-level release profile explicitly enables overflow checks:

```toml
[profile.release]
overflow-checks = true
``` [2](#0-1) 

The `prod` profile inherits from `release` and therefore also has overflow checks enabled:

```toml
[profile.prod]
inherits = "release"
``` [3](#0-2) 

With `overflow-checks = true`, Rust turns integer overflow into a `panic!` in all build profiles. The panic is unhandled and propagates up through the async task, crashing the node process.

---

### Impact Explanation

A single crafted P2P message causes an immediate, unrecoverable node crash. No chain state, PoW, or authentication is required. The crash occurs before any chain lookup or allocation, so there is no secondary resource exhaustion — the panic itself is the impact.

---

### Likelihood Explanation

The LightClient protocol is included in the default `support_protocols` list shipped with CKB:

```toml
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage",
                     "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
``` [4](#0-3) 

`default_support_all_protocols()` also includes `LightClient`: [5](#0-4) 

The launcher registers the handler for any node where `LightClient` is in the configured protocol list: [6](#0-5) 

The `received` handler dispatches to `execute` with no prior authentication or rate-limiting specific to this message type: [7](#0-6) 

Any peer that can open a TCP connection to the node's P2P port can trigger the crash.

---

### Recommendation

Replace the unchecked multiplication with a saturating or checked variant before the comparison:

```rust
// Option A – saturating (safe, always fires the guard on overflow)
let total = self.message.difficulties().len()
    .saturating_add((last_n_blocks as usize).saturating_mul(2));
if total > constant::GET_LAST_STATE_PROOF_LIMIT { … }

// Option B – reject immediately if last_n_blocks itself exceeds the limit
if last_n_blocks as usize > constant::GET_LAST_STATE_PROOF_LIMIT / 2 {
    return StatusCode::MalformedProtocolMessage.with_context("too many samples");
}
``` [1](#0-0) 

---

### Proof of Concept

```rust
// Pseudocode unit test (no chain state needed)
let last_n_blocks: u64 = (usize::MAX / 2 + 1) as u64;
// In any build with overflow-checks=true (including CKB's release profile):
// (last_n_blocks as usize) * 2  →  panic: attempt to multiply with overflow
let _ = (last_n_blocks as usize) * 2;  // panics here
```

Network-level reproduction:
1. Start a CKB node with default config (LightClient enabled).
2. Connect a peer via the `/ckb/lightclient` protocol.
3. Send a `GetLastStateProof` molecule message with:
   - `last_n_blocks = 9_223_372_036_854_775_808` (`usize::MAX/2 + 1`)
   - `difficulties = []`
   - `last_hash` = any valid main-chain block hash
4. The node panics at line 201 and terminates. [8](#0-7) [9](#0-8)

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

**File:** resource/ckb.toml (L112-112)
```text
support_protocols = ["Ping", "Discovery", "Identify", "Feeler", "DisconnectMessage", "Sync", "Relay", "Time", "Alert", "LightClient", "Filter", "HolePunching"]
```

**File:** util/app-config/src/configs/network.rs (L236-250)
```rust
pub fn default_support_all_protocols() -> Vec<SupportProtocol> {
    vec![
        SupportProtocol::Ping,
        SupportProtocol::Discovery,
        SupportProtocol::Identify,
        SupportProtocol::Feeler,
        SupportProtocol::DisconnectMessage,
        SupportProtocol::Sync,
        SupportProtocol::Relay,
        SupportProtocol::Time,
        SupportProtocol::Alert,
        SupportProtocol::LightClient,
        SupportProtocol::Filter,
        SupportProtocol::HolePunching,
    ]
```

**File:** util/launcher/src/lib.rs (L467-475)
```rust
        if support_protocols.contains(&SupportProtocol::LightClient) {
            let light_client = LightClientProtocol::new(shared.clone());
            protocols.push(CKBProtocol::new_with_support_protocol(
                SupportProtocols::LightClient,
                Box::new(light_client),
                Arc::clone(&network_state),
            ));
        } else {
            flags.remove(Flags::LIGHT_CLIENT);
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

**File:** util/light-client-protocol-server/src/constant.rs (L6-6)
```rust
pub const GET_LAST_STATE_PROOF_LIMIT: usize = 1000;
```
