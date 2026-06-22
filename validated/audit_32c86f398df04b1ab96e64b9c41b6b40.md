### Title
Network Alert Signatures Not Bound to Chain/Network Context — Cross-Network Replay Attack - (File: `util/network-alert/src/verifier.rs`, `util/gen-types/src/extension/calc_hash.rs`)

### Summary

The CKB network alert system signs and verifies `RawAlert` messages using a multi-signature scheme. The signed message is computed as a plain `blake2b_256` hash of the `RawAlert` bytes with no domain separator, no chain name, and no network identifier. This means a valid alert signature produced for one CKB network (e.g., testnet) is cryptographically indistinguishable from a valid alert for any other CKB network using the same signing keys. An unprivileged P2P peer can replay a legitimately signed alert from one network onto another, causing all receiving nodes to accept and propagate the false alert.

### Finding Description

The `RawAlert` molecule schema contains only application-level fields:

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
``` [1](#0-0) 

There is no `chain_id`, `network_name`, or any domain-separating field. The hash used as the signing message is computed as:

```rust
pub fn calc_alert_hash(&self) -> packed::Byte32 {
    self.calc_hash()   // = blake2b_256(self.as_slice())
}
``` [2](#0-1) 

where `calc_hash` is simply `blake2b_256(self.as_slice())` with no prefix or context tag: [3](#0-2) 

The verifier then uses this context-free hash directly as the ECDSA message:

```rust
let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
``` [4](#0-3) 

and passes it to `verify_m_of_n` against the hardcoded Nervos Foundation public keys: [5](#0-4) 

The default public keys are compiled into the binary from a single `alert_signature.toml`: [6](#0-5) 

The P2P alert relay handler accepts alert messages from any connected peer and, if signatures verify, propagates them network-wide and adds them to the local notifier: [7](#0-6) 

### Impact Explanation

An unprivileged P2P peer who has observed a legitimately signed alert on any CKB network sharing the same alert signing keys can replay that exact alert (raw bytes + signatures) to a mainnet node. The node's `Verifier::verify_signatures` will accept it because the signature is mathematically valid — the signed hash contains no network context. The node then:
1. Adds the false alert to its local notifier (displayed to operators via `get_blockchain_info`)
2. Broadcasts it to all connected peers, propagating the false alert across the entire network

This allows an attacker to inject arbitrary false critical alerts (e.g., "upgrade immediately", "critical bug in your version") into the mainnet, causing operators to take incorrect actions. It also allows cancellation of legitimate alerts by replaying a cancel-alert from another network.

### Likelihood Explanation

The attack requires:
1. The same alert signing keys to be used across CKB networks — the default `alert_signature.toml` is compiled into the binary and used for all networks unless explicitly overridden, making this the common case.
2. A legitimately signed alert to have been issued on any network sharing those keys — once any alert is ever issued on testnet or a private network, it is permanently replayable.
3. P2P connectivity to a target node — any node that accepts inbound connections is reachable.

All three conditions are realistic and require no privileged access. The attacker only needs to be a normal P2P peer.

### Recommendation

**Short term:** Include a network/chain domain separator in the signed message. The simplest fix is to prepend the chain spec name (e.g., `"ckb"` for mainnet, `"ckb_testnet"` for testnet) or the genesis block hash to the data hashed in `calc_alert_hash`. For example:

```rust
pub fn calc_alert_hash(&self, chain_name: &[u8]) -> packed::Byte32 {
    let mut hasher = new_blake2b();
    hasher.update(chain_name);
    hasher.update(self.as_slice());
    // ...
}
```

**Long term:** Adopt a structured signing scheme (analogous to EIP-712) where every signed message explicitly encodes its domain (chain name, version, genesis hash) so that signatures are never transferable across network contexts.

### Proof of Concept

1. Operator on testnet issues a valid alert signed by 2-of-4 Nervos Foundation keys. The alert is broadcast over the testnet P2P network and is publicly observable.

2. Attacker captures the raw `Alert` molecule bytes (containing `RawAlert` + `signatures`) from the testnet P2P stream.

3. Attacker connects as a normal P2P peer to a mainnet node.

4. Attacker sends the captured bytes as a P2P `Alert` protocol message to the mainnet node.

5. The mainnet node's `AlertRelayer::received` parses the alert, calls `self.verifier.verify_signatures(&alert)`, which computes `blake2b_256(raw_alert.as_slice())` — identical to the testnet hash since `RawAlert` contains no network field — and verifies the signatures against the same hardcoded keys. Verification passes.

6. The mainnet node adds the false alert to its notifier and broadcasts it to all its peers. The false alert propagates across mainnet.

### Citations

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

**File:** util/gen-types/src/extension/calc_hash.rs (L15-22)
```rust
impl<'r, R> CalcHash for R
where
    R: Reader<'r>,
{
    fn calc_hash(&self) -> packed::Byte32 {
        blake2b_256(self.as_slice()).into()
    }
}
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

**File:** util/app-config/src/configs/network_alert.rs (L14-18)
```rust
impl Default for Config {
    fn default() -> Self {
        let alert_config = include_bytes!("./alert_signature.toml");
        toml::from_slice(&alert_config[..]).expect("alert system config")
    }
```

**File:** util/network-alert/src/alert_relayer.rs (L150-179)
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
        // mark sender as known
        self.mark_as_known(peer_index, alert_id);
        // broadcast message
        let selected_peers: Vec<PeerIndex> = nc
            .connected_peers()
            .into_iter()
            .filter(|peer| self.mark_as_known(*peer, alert_id))
            .collect();
        if let Err(err) = nc.quick_filter_broadcast(
            TargetSession::Multi(Box::new(selected_peers.into_iter())),
            data,
        ) {
            debug!("alert broadcast error: {:?}", err);
        }
        // add to received alerts
        self.notifier.lock().add(&alert);
    }
```
