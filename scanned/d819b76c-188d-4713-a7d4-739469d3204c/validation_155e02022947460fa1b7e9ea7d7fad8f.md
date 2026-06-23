### Title
Truncated Genesis Hash in `identify_name()` Enables Cross-Network Peer Spoofing — (File: `spec/src/consensus.rs`)

---

### Summary

The `identify_name()` function constructs the P2P network identifier using only the first **8 hex characters (4 bytes)** of the 32-byte genesis hash. An unprivileged attacker can brute-force a custom genesis block whose hash shares the same 4-byte prefix as the mainnet genesis hash, allowing their rogue node to pass the P2P identify protocol check and be accepted as a valid mainnet peer with all sync/relay protocols opened.

---

### Finding Description

`identify_name()` in `spec/src/consensus.rs` at line 967 constructs the network identifier as:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])   // only 4 bytes of 32-byte hash
}
``` [1](#0-0) 

This string is passed as the `name` field in the P2P identify message. The `Identify::verify()` function in `network/src/protocols/identify/mod.rs` performs the only network-membership check:

```rust
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    let reader = packed::IdentifyReader::from_slice(data).ok()?;
    let name = reader.name().as_utf8().ok()?.to_owned();
    if self.name != name {
        return None;   // only check: does the name string match?
    }
    ...
    Some((Flags::from_bits_truncate(flag), raw_client_version))
}
``` [2](#0-1) 

If `verify()` returns `Some`, `received_identify()` accepts the peer and opens all protocols (sync, relay, etc.): [3](#0-2) 

The `name` field is set from `identify_announce.0` passed into `NetworkService::new()`, which is populated from `consensus.identify_name()`: [4](#0-3) 

The `Consensus` struct stores the full `genesis_hash` as a `Byte32` (32 bytes), but `identify_name()` discards 28 of those bytes: [5](#0-4) 

The chain spec name for mainnet is `"ckb"`, for testnet `"ckb_testnet"`, for devnet `"ckb_dev"` — all are freely settable in a custom spec file: [6](#0-5) 

---

### Impact Explanation

An attacker whose node passes the identify check is treated as a fully trusted mainnet peer. All CKB protocols (sync, relay, block-filter, light-client) are opened against it. The attacker can:

1. **Exhaust connection slots** on victim nodes — mainnet nodes have bounded inbound/outbound peer limits (`max_inbound`, `max_outbound`). Filling them with rogue peers prevents legitimate peers from connecting, causing a targeted DoS.
2. **Waste CPU/memory** by sending a flood of structurally valid but consensus-invalid blocks or transactions that pass P2P deserialization and enter the async verification pipeline before being rejected.
3. **Bypass peer-trust assumptions** in any future code that gates behavior on "peer passed identify" without re-checking chain membership. [7](#0-6) 

---

### Likelihood Explanation

The attacker controls the entire content of their custom genesis block (timestamp, nonce, genesis cell message, system cell data, etc.). Finding a genesis block whose `blake2b_256` hash shares the same leading 4 bytes as the mainnet genesis hash is a **birthday/preimage search over 2^32 ≈ 4 billion candidates**. On a modern GPU this takes seconds to minutes — a one-time offline computation. The attacker then runs a permanent rogue node with that spec. No privileged access, no key material, and no majority hashpower is required.

---

### Recommendation

Use the **full 64-character genesis hash** in `identify_name()` to make the network identifier cryptographically unique:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)   // full 32-byte hash
}
``` [8](#0-7) 

This is the direct analog of the Gnosis fix: just as `_verifySender()` needed to add `messageSourceChainId() == MIRROR_DOMAIN` to check the full source identity, `identify_name()` needs to include the full genesis hash to check the full chain identity.

---

### Proof of Concept

1. Read the mainnet genesis hash prefix: first 8 hex chars of `0x92b197aa...` (from `docs/hashes.toml`).
2. Write a script that varies the `genesis.genesis_cell.message` field of a custom spec with `name = "ckb"` and hashes the resulting genesis block until the first 8 hex chars match.
3. With GPU acceleration, this search completes in seconds (~2^32 blake2b operations).
4. Start a CKB node with the crafted spec. Connect it to a mainnet node.
5. Observe: the identify protocol exchange succeeds (`name` strings match), the session is accepted, and sync/relay protocols are opened — despite the node being on a completely different chain. [9](#0-8)

### Citations

**File:** spec/src/consensus.rs (L512-519)
```rust
pub struct Consensus {
    /// Names the network.
    pub id: String,
    /// The genesis block
    pub genesis_block: BlockView,
    /// The genesis block hash
    pub genesis_hash: Byte32,
    /// The dao type hash
```

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```

**File:** network/src/protocols/identify/mod.rs (L384-454)
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
            }
            Some((flags, client_version)) => {
                let registry_client_version = |version: String| {
                    self.network_state.with_peer_registry_mut(|registry| {
                        if let Some(peer) = registry.get_peer_mut(context.session.id) {
                            peer.identify_info = Some(PeerIdentifyInfo {
                                client_version: version,
                                flags,
                            })
                        }
                    });
                };

                registry_client_version(client_version);

                let required_flags = self.network_state.required_flags;

                if context.session.ty.is_outbound() {
                    // why don't set inbound here?
                    // because inbound address can't feeler during staying connected
                    // and if set it to peer store, it will be broadcast to the entire network,
                    // but this is an unverified address

                    self.network_state.with_peer_store_mut(|peer_store| {
                        peer_store.add_outbound_addr(context.session.address.clone(), flags);
                    });

                    if self.network_state.with_peer_registry_mut(|reg| {
                        reg.change_feeler_flags(&context.session.address, flags)
                    }) {
                        let _ = context
                            .open_protocols(
                                context.session.id,
                                TargetProtocol::Single(SupportProtocols::Feeler.protocol_id()),
                            )
                            .await;
                    } else if required_flags_filter(required_flags, flags) {
                        // The remote end can support all local protocols.
                        let _ = context
                            .open_protocols(
                                context.session.id,
                                TargetProtocol::Filter(Box::new(move |id| {
                                    id != &SupportProtocols::Feeler.protocol_id()
                                })),
                            )
                            .await;
                    } else {
                        // The remote end cannot support all local protocols.
                        warn!(
                            "Session closed from IdentifyProtocol due to peer's flag not meeting the requirements"
                        );
                        return MisbehaveResult::Disconnect;
                    }
                }
                MisbehaveResult::Continue
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

**File:** resource/specs/mainnet.toml (L1-1)
```text
name = "ckb"
```

**File:** network/src/peer_registry.rs (L22-36)
```rust
pub struct PeerRegistry {
    peers: HashMap<SessionId, Peer>,
    // max inbound limitation
    max_inbound: u32,
    // max outbound limitation
    max_outbound: u32,
    // max block-relay only outbound limitation
    // We do not relay tx or addr messages with these peers
    max_outbound_block_relay: u32,
    // Only whitelist peers or allow all peers.
    whitelist_only: bool,
    whitelist_peers: HashSet<PeerId>,
    feeler_peers: HashMap<PeerId, Flags>,
    disable_block_relay_only_connection: bool,
}
```
