### Title
Network Identify Domain Separator Fixed at Genesis — Cannot Differentiate Post-Deployment Hard Forks - (File: `spec/src/consensus.rs`, `network/src/protocols/identify/mod.rs`)

---

### Summary

CKB's P2P identify protocol uses a network identifier (`identify_name`) computed from the chain name and the first 8 hex characters (4 bytes) of the genesis hash. This value is encoded once at node startup and never refreshed. Because the genesis block is immutable, any hard fork of CKB mainnet produces the **identical** `identify_name` on both the legacy and new chain. Nodes from both forks will therefore accept each other's P2P connections, defeating the network-isolation guarantee the identify protocol is designed to enforce.

---

### Finding Description

`Consensus::identify_name()` computes the network domain separator as:

```rust
format!("/{}/{}", self.id, &genesis_hash[..8])
``` [1](#0-0) 

This string is passed into `Identify::new()` at node startup, where it is serialized into `encode_data` — a fixed `Bytes` field that is never mutated for the lifetime of the process:

```rust
fn new(name: String, flags: Flags, client_version: String) -> Self {
    Identify {
        encode_data: packed::Identify::new_builder()
            .name(name.as_str())
            ...
            .build()
            .as_bytes(),
        name,
    }
}
``` [2](#0-1) 

During the P2P handshake, `Identify::verify()` accepts a remote peer if and only if the remote's `name` field matches `self.name`:

```rust
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    ...
    if self.name != name {
        return None;   // ban peer
    }
    ...
}
``` [3](#0-2) 

Because the genesis block is immutable, **every fork of CKB mainnet shares the same genesis hash and the same chain name `"ckb"`**, producing the same `identify_name` (e.g., `/ckb/92b197aa`). There is no fork-specific component in the identifier, and no mechanism to recompute or refresh it after a fork activates.

The `IdentifyCallback` is constructed once in `NetworkService::new()` and holds the frozen `Identify` struct for the entire node lifetime: [4](#0-3) 

---

### Impact Explanation

After a CKB hard fork where both chains retain the same genesis block (the normal case), nodes on the legacy chain and nodes on the new chain present identical `identify_name` values. The identify protocol — the **only** P2P-layer mechanism for network isolation — cannot distinguish them. Consequences:

- **Cross-fork peer slot exhaustion**: Nodes on chain A fill their outbound/inbound connection slots with nodes from chain B, reducing effective connectivity to their own chain.
- **Cross-fork transaction/block relay**: Transactions or compact blocks valid only on one fork are relayed to nodes on the other fork, wasting CPU and bandwidth on validation that will always fail.
- **Eclipse attack amplification**: An attacker operating nodes on a minority fork can use the shared `identify_name` to occupy connection slots of mainnet nodes, since the identify check passes unconditionally.

The sync and relay protocols will eventually detect chain divergence at the block level, but the identify-layer isolation — the first and cheapest filter — is completely bypassed.

---

### Likelihood Explanation

Hard forks are a planned, recurring event in any active blockchain. CKB has already undergone hardfork upgrades (RFC0028, RFC0032, etc.) and has a versioned hardfork switch (`HardForks`) in the codebase. Any future contentious fork that results in two live chains with the same genesis block immediately triggers this condition. No attacker capability beyond running a node on the minority fork is required; the identify check passes automatically. [5](#0-4) 

---

### Recommendation

**Short term**: Include a fork-epoch or hardfork-activation identifier in `identify_name`. For example, append the epoch number at which the most recent hardfork activated:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    let fork_id = self.hardfork_switch.latest_active_epoch(); // fork-specific
    format!("/{}/{}/{}", self.id, &genesis_hash[..8], fork_id)
}
```

This ensures that nodes on different forks of the same genesis produce different `identify_name` values and ban each other at the identify layer.

**Long term**: Document the fork-isolation properties of the identify protocol and establish a process for updating the network identifier as part of any hard fork activation procedure, analogous to EIP-155 chain ID management in Ethereum.

---

### Proof of Concept

1. CKB mainnet hard-forks at epoch N. Both chains retain genesis block hash `0x92b197aa...`.
2. Both chains compute `identify_name = "/ckb/92b197aa"` (identical).
3. A node on the legacy chain dials a node on the new chain. `Identify::verify()` compares `"/ckb/92b197aa" == "/ckb/92b197aa"` → passes. The session is accepted.
4. The legacy-chain node now occupies a connection slot on the new-chain node. It relays legacy-chain transactions and compact blocks. The new-chain node wastes CPU verifying them (all fail at the chain-validation layer, not the P2P layer).
5. An attacker running 50 legacy-chain nodes can fill all inbound slots of a new-chain node, eclipsing it from its own network — with zero cryptographic capability required, only a running node on the other fork. [6](#0-5) [1](#0-0)

### Citations

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```

**File:** spec/src/consensus.rs (L999-1002)
```rust
    /// Returns the hardfork switch.
    pub fn hardfork_switch(&self) -> &HardForks {
        &self.hardfork_switch
    }
```

**File:** network/src/protocols/identify/mod.rs (L384-397)
```rust
    async fn received_identify(
        &mut self,
        context: &mut ProtocolContextMutRef<'_>,
        identify: &[u8],
    ) -> MisbehaveResult {
        match self.identify.verify(identify) {
            None => {
                self.network_state.ban_session(
                    &context.control().clone().into(),
                    context.session.id,
                    BAN_ON_NOT_SAME_NET,
                    "The nodes are not on the same network".to_string(),
                );
                MisbehaveResult::Disconnect
```

**File:** network/src/protocols/identify/mod.rs (L524-535)
```rust
impl Identify {
    fn new(name: String, flags: Flags, client_version: String) -> Self {
        Identify {
            encode_data: packed::Identify::new_builder()
                .name(name.as_str())
                .flag(flags.bits())
                .client_version(client_version.as_str())
                .build()
                .as_bytes(),
            name,
        }
    }
```

**File:** network/src/protocols/identify/mod.rs (L541-561)
```rust
    fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
        let reader = packed::IdentifyReader::from_slice(data).ok()?;

        let name = reader.name().as_utf8().ok()?.to_owned();
        if self.name != name {
            warn!(
                "IdentifyProtocol detects peer has different network identifiers, local network id: {}, remote network id: {}",
                self.name, name,
            );
            return None;
        }

        let flag: u64 = reader.flag().into();
        if flag == 0 {
            return None;
        }

        let raw_client_version = reader.client_version().as_utf8().ok()?.to_owned();

        Some((Flags::from_bits_truncate(flag), raw_client_version))
    }
```

**File:** network/src/network.rs (L868-877)
```rust
        let identify_callback = IdentifyCallback::new(
            Arc::clone(&network_state),
            identify_announce.0,
            identify_announce.1.clone(),
            identify_announce.2,
        );
        let identify_meta = SupportProtocols::Identify.build_meta_with_service_handle(move || {
            ProtocolHandle::Callback(Box::new(IdentifyProtocol::new(identify_callback)))
        });
        protocol_metas.push(identify_meta);
```
