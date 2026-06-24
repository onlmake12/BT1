Audit Report

## Title
Truncated 32-bit Genesis Hash in `identify_name()` Allows Cross-Chain Peers to Bypass Network Identity Filter — (File: `spec/src/consensus.rs`)

## Summary
`Consensus::identify_name()` constructs the P2P identity string using only the first 8 hex characters (4 bytes / 32 bits) of the 32-byte genesis hash. Because `Identify::verify()` performs a full string equality check against this truncated identifier, an attacker who crafts a custom chain whose genesis hash shares the same 4-byte prefix can pass the identity gate and establish persistent P2P connections to legitimate mainnet or testnet nodes. This enables peer-slot exhaustion and resource consumption against any reachable CKB node with no privileged access required.

## Finding Description
`Consensus::identify_name()` is the sole source of the chain-identity string used during the P2P handshake:

```rust
// spec/src/consensus.rs  lines 964-968
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])   // only 8 of 64 hex chars
}
```

The `ChainSpec.name` field maps directly to `Consensus.id` via `spec/src/lib.rs` line 573 (`.id(self.name.clone())`). The mainnet spec sets `name = "ckb"` (`resource/specs/mainnet.toml` line 1), and this field is freely configurable in any custom chain spec.

`Identify::verify()` performs a full string equality check against this truncated identifier:

```rust
// network/src/protocols/identify/mod.rs  lines 541-551
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    let name = reader.name().as_utf8().ok()?.to_owned();
    if self.name != name {   // full equality, but string is only 32 bits of entropy
        return None;
    }
    ...
}
```

A `None` return triggers a 5-minute ban and disconnect (`network/src/protocols/identify/mod.rs` lines 389-397). A matching return admits the peer fully into the sync/relay pipeline.

**Exploit path:**
1. Create a custom chain spec with `name = "ckb"` (or `"ckb_testnet"`).
2. Vary any mutable genesis field (e.g., `genesis.timestamp`, `genesis.nonce`, or `genesis.genesis_cell.message`) and recompute the genesis hash until the first 4 bytes match the target network's prefix. This requires on average 2^32 ≈ 4 billion attempts — achievable in seconds to minutes on commodity hardware using CKB's Blake2b genesis hashing.
3. Start a node on this custom chain and connect to a legitimate mainnet peer.
4. `Identify::verify()` passes because `"/ckb/<4-byte-prefix>" == "/ckb/<4-byte-prefix>"`.
5. The attacker node is admitted; it can now occupy peer slots and send syntactically valid but semantically wrong sync/relay messages that consume CPU and memory before being rejected at the block-validation layer.

No existing guard prevents this: the identify protocol is the only network-layer chain-identity gate (`network/src/network.rs` line 867: "Identify is a core protocol, user cannot disable it via config"), and it checks only 32 bits of the genesis hash.

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker running multiple nodes with colliding genesis hash prefixes can:
- Exhaust inbound peer slots on targeted nodes, preventing legitimate peers from connecting.
- Force targeted nodes to process sync/relay messages (e.g., `HeadersProcess`, `BlockProcess`) that pass the identity gate and enter the processing pipeline before being rejected, consuming CPU and memory per message.
- Repeat the attack indefinitely since the 5-minute ban applies per session, not per genesis hash, and the attacker can reconnect with new sessions or rotate IPs.

At scale (many attacker nodes targeting many mainnet nodes simultaneously), this constitutes network-wide congestion with negligible cost to the attacker.

## Likelihood Explanation
- **No privileged access required** — any unprivileged network peer can attempt this.
- **Computational cost is negligible** — a 32-bit prefix match requires ~2^32 Blake2b hash computations on average. At ~100M hashes/second on a single CPU core, this completes in under a minute. The claim's stated "2^16" figure is a birthday-bound miscalculation; the correct preimage cost is 2^32, but this remains trivially fast.
- **Chain ID is freely settable** — the attacker sets `name = "ckb"` in their custom `chainspec.toml`; no key material or insider knowledge is needed.
- **No detection before connection** — the truncated hash is the only chain-identity signal exchanged before the peer is admitted.
- **Repeatable** — the attacker can maintain a pool of pre-computed colliding genesis specs and reconnect after bans expire.

## Recommendation
Replace the 8-character genesis hash slice with the full 64-character hex string in `identify_name()`:

```rust
// spec/src/consensus.rs
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)   // use full 256-bit hash
}
```

This is a non-breaking change for honest nodes (all derive the same full hash from the same genesis block) and raises the collision cost from ~2^32 to ~2^256.

## Proof of Concept
1. Clone the CKB repository and create a custom chain spec with `name = "ckb"`.
2. Write a script that iterates `genesis.nonce` (a `u128` field, providing ample search space) and calls `ChainSpec::build_genesis()` + `consensus.genesis_hash()` until `&genesis_hash_hex[..8]` matches the mainnet prefix (obtainable from any public mainnet node or the bundled spec hash).
3. Start a CKB node on this custom chain.
4. Connect to a mainnet peer. `Identify::verify()` passes because the identity strings are equal.
5. The peer is admitted. Send `GetHeaders` or `SendBlock` messages; they enter `HeadersProcess`/`BlockProcess` and consume CPU/memory before being rejected at the ancestry check, confirming resource consumption past the identity gate. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** spec/src/consensus.rs (L964-968)
```rust
    /// The network identify name, used for network identify protocol
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```

**File:** network/src/protocols/identify/mod.rs (L389-397)
```rust
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

**File:** network/src/protocols/identify/mod.rs (L541-551)
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
```

**File:** spec/src/lib.rs (L572-573)
```rust
        let mut builder = ConsensusBuilder::new(genesis_block, genesis_epoch_ext)
            .id(self.name.clone())
```

**File:** resource/specs/mainnet.toml (L1-1)
```text
name = "ckb"
```
