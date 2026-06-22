### Title
`RawAlert` Signature Lacks Network/Chain Identifier, Enabling Cross-Network Alert Replay — (File: `util/network-alert/src/verifier.rs`)

### Summary
The CKB network alert system signs `RawAlert` messages using a multi-signature scheme controlled by the Nervos Foundation. The signed message hash (`calc_alert_hash`) covers only the alert payload fields and includes no network or chain identifier. Because the same Foundation signing keys govern both mainnet and testnet, a valid signed alert from one network is cryptographically indistinguishable from a valid alert on the other network. Any RPC caller can submit a captured mainnet alert to testnet nodes (or vice versa) and have it accepted and displayed.

### Finding Description

**Root cause — missing domain separator in `calc_alert_hash`**

`Verifier::verify_signatures` derives the message to verify as:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [1](#0-0) 

`calc_alert_hash` is implemented as a plain hash of the `RawAlert` molecule-encoded bytes:

```rust
impl<'r> packed::RawAlertReader<'r> {
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
``` [2](#0-1) 

The `RawAlert` schema contains only application-level fields — `notice_until`, `id`, `cancel`, `priority`, `message`, `min_version`, `max_version` — with no network name, genesis hash, or chain ID:

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
``` [3](#0-2) 

**Attacker-controlled entry path**

The `send_alert` RPC is the public entry point. Any RPC caller can submit an `Alert` struct (raw alert + signatures). The verifier checks only that the recovered public keys match the configured Foundation keys and that the threshold is met — it does not check whether the alert was intended for this network.

**Exploit flow**

1. Attacker observes a Foundation-signed alert broadcast on mainnet (alerts propagate over the P2P Alert protocol, protocol id `110`).
2. Attacker submits the identical `Alert` struct (unchanged raw bytes + signatures) to a testnet node via `send_alert` RPC.
3. `Verifier::verify_signatures` recomputes `calc_alert_hash` over the same `RawAlert` bytes, recovers the same Foundation public keys, and accepts the alert.
4. Testnet nodes store and display the mainnet alert to operators.

The reverse direction (testnet alert replayed on mainnet) is equally valid and more impactful: a testnet-specific deprecation or emergency notice could be injected into mainnet nodes, causing mainnet operators to take incorrect actions.

### Impact Explanation

Node operators on the target network receive and act on alerts that were never intended for them. A testnet "network reset" or "emergency upgrade" alert replayed on mainnet could cause mainnet operators to halt operations, downgrade software, or take other disruptive actions based on false information. The alert system is specifically designed as a trusted, authenticated channel for urgent network-wide announcements; cross-network replay undermines that trust guarantee entirely.

### Likelihood Explanation

Alerts are broadcast over the P2P network and are therefore publicly observable by any connected peer. The `send_alert` RPC endpoint is reachable by any local or (if configured) remote RPC caller. No privileged access, key material, or cryptographic break is required — only a captured alert from the other network. The precondition (same Foundation keys on mainnet and testnet) is the standard operational assumption, as the Foundation controls both networks with the same key set.

### Recommendation

Include a network/chain domain separator in the signed message. The natural choice is the genesis block hash (already used in `identify_name`):

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])
}
``` [4](#0-3) 

Concretely, change `calc_alert_hash` to hash `blake2b(genesis_hash || raw_alert_bytes)` so that a signature produced for one network's genesis is cryptographically invalid on any other network.

### Proof of Concept

```
# 1. On a mainnet-connected node, observe a broadcast alert via P2P or RPC:
curl -X POST mainnet-node:8114 -d '{"id":1,"jsonrpc":"2.0","method":"get_blockchain_info","params":[]}'
# -> alerts[] contains a Foundation-signed Alert with raw + signatures

# 2. Submit the identical Alert to a testnet node:
curl -X POST testnet-node:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_alert","params":[<copied_alert_json>]}'
# -> {"result": null}  (accepted)

# 3. Verify testnet node now displays the mainnet alert:
curl -X POST testnet-node:8114 -d '{"id":1,"jsonrpc":"2.0","method":"get_blockchain_info","params":[]}'
# -> alerts[] now contains the mainnet alert, accepted as valid
```

The `verify_signatures` call succeeds because `calc_alert_hash` produces the same 32-byte digest on both networks for the same `RawAlert` bytes, and the Foundation public keys are identical across networks. [5](#0-4)

### Citations

**File:** util/network-alert/src/verifier.rs (L33-35)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
```

**File:** util/network-alert/src/verifier.rs (L56-63)
```rust
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
```

**File:** util/gen-types/src/extension/calc_hash.rs (L292-300)
```rust
impl<'r> packed::RawAlertReader<'r> {
    /// Calculates the hash for [self.as_slice()] as the alert hash.
    ///
    /// [self.as_slice()]: ../prelude/trait.Reader.html#tymethod.as_slice
    pub fn calc_alert_hash(&self) -> packed::Byte32 {
        self.calc_hash()
    }
}
impl_calc_special_hash_for_entity!(RawAlert, calc_alert_hash);
```

**File:** util/gen-types/schemas/extensions.mol (L445-453)
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
```

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```
