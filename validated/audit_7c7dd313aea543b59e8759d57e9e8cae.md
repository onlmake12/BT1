### Title
Missing Network Domain Separation in Alert Signature Verification Allows Cross-Network Replay - (File: `util/network-alert/src/verifier.rs`)

### Summary

The CKB network alert signature scheme hashes only the `RawAlert` payload fields when computing the message to verify. No chain identifier, genesis hash, or network name is included in the signed message. A valid alert signature produced for CKB mainnet is therefore cryptographically indistinguishable from one intended for CKB testnet (or any other CKB-based network sharing the same foundation public keys), enabling any unprivileged RPC caller to replay a legitimately signed mainnet alert on testnet nodes and vice versa.

### Finding Description

In `util/network-alert/src/verifier.rs`, `verify_signatures` computes the message to verify as:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

`calc_alert_hash()` is defined in `util/gen-types/src/extension/calc_hash.rs` as a plain `blake2b_256` over the raw molecule-encoded bytes of `RawAlert`:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()   // = blake2b_256(self.as_slice())
    }
}
```

The `RawAlert` molecule schema (`util/gen-types/schemas/extensions.mol`) contains only:

```
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}
```

There is no `chain_id`, genesis hash, or network name field. The signed digest is therefore identical for the same alert content regardless of which CKB network it targets.

The `send_alert` RPC endpoint (`rpc/src/module/alert.rs`) is publicly reachable by any RPC caller. When a peer relays an alert over P2P, `alert_relayer.rs` calls `self.verifier.verify_signatures(&alert)` with no network context check either.

An attacker who observes a valid 2-of-4 foundation-signed alert on CKB mainnet can submit the identical byte-for-byte alert to a CKB testnet node via `send_alert`. Because the public keys configured in `NetworkAlertConfig` are the same foundation keys on both networks, the signature check passes, the alert is accepted, stored, and re-broadcast to all connected testnet peers.

### Impact Explanation

A replayed mainnet alert propagates across the entire testnet P2P network (or vice versa), causing every testnet node to display a mainnet-targeted emergency message (e.g., "upgrade immediately due to critical bug in v0.105.*"). This can:

- Cause operators of testnet nodes to take unnecessary emergency actions (halting nodes, emergency upgrades) based on a message not intended for their network.
- Undermine trust in the alert system's integrity, since the alert mechanism is the primary out-of-band emergency communication channel for the Nervos foundation.
- In the reverse direction, suppress or confuse a real testnet-targeted alert if a stale mainnet alert with the same `id` has already been received and deduplicated.

### Likelihood Explanation

The preconditions are minimal for an unprivileged attacker:

1. Observe any valid alert broadcast on CKB mainnet (public P2P network, or via any public RPC node).
2. Submit the identical serialized alert bytes to a testnet node's `send_alert` RPC endpoint (no authentication required beyond RPC access).

The foundation has issued real alerts in the past (e.g., alert `20230001` visible in `util/network-alert/src/tests/generate_alert_signature.rs`). Any such historical alert with a future `notice_until` timestamp is immediately replayable. Even expired alerts can be replayed if `notice_until` is set far in the future, which real alerts typically are.

### Recommendation

Include a network domain separator in the signed message. The simplest fix is to prefix the `RawAlert` bytes with the genesis block hash (already available as `consensus.genesis_hash`) before hashing, analogous to EIP-712's `DOMAIN_SEPARATOR`:

```rust
// In verify_signatures:
let mut hasher = new_blake2b();
hasher.update(genesis_hash.as_slice());   // network domain separator
hasher.update(alert.raw().as_slice());
let mut message_bytes = [0u8; 32];
hasher.finalize(&mut message_bytes);
let message = Message::from_slice(&message_bytes)?;
```

Alternatively, add a `chain_id` or `network_name` field to `RawAlert` and include it in the hash. Either approach ensures a mainnet signature is invalid on testnet and vice versa.

### Proof of Concept

1. Connect to a CKB mainnet node and retrieve a valid alert (e.g., alert `20230001` with its two known signatures from `generate_alert_signature.rs`).
2. Submit the identical alert to a CKB testnet node:
   ```json
   {
     "jsonrpc": "2.0",
     "method": "send_alert",
     "params": [{ "id": "0x1343b1", "cancel": "0x0", "priority": "0x14",
                  "message": "CKB v0.105.* have bugs...",
                  "notice_until": "0x...",
                  "signatures": ["0x8dca28...", "0x4554b3..."] }],
     "id": 1
   }
   ```
3. The testnet node accepts the alert (signatures verify against the same foundation public keys), stores it, and broadcasts it to all connected testnet peers.
4. All testnet nodes display the mainnet-targeted emergency message.

Root cause confirmed at: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** util/gen-types/src/extension/calc_hash.rs (L292-299)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
```

**File:** util/gen-types/schemas/extensions.mol (L445-458)
```text
table RawAlert {
    notice_until:   Uint64,
    id:             Uint32,
    cancel:         Uint32,
    priority:       Uint32,
    message:        Bytes,
    min_version:    BytesOpt,
    max_version:    BytesOpt,
}

table Alert {
    raw:                        RawAlert,
    signatures:                 BytesVec,
}
```

**File:** util/network-alert/src/alert_relayer.rs (L150-162)
```rust
        // verify
        if let Err(err) = self.verifier.verify_signatures(&alert) {
            debug!(
                "An alert from peer {} with invalid signatures, error {:?}",
                peer_index, err
            );
            nc.ban_peer(
                peer_index,
                BAD_MESSAGE_BAN_TIME,
                String::from("send us an alert with invalid signatures"),
            );
            return;
        }
```
