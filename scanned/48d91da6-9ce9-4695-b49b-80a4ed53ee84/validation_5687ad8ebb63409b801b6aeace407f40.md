Audit Report

## Title
Truncated 4-Byte Genesis Hash Prefix in `identify_name()` Allows Cross-Chain Peers to Bypass Network Identity Filter — (File: `spec/src/consensus.rs`)

## Summary
`Consensus::identify_name()` embeds only the first 8 hex characters (4 bytes / 32 bits) of the 32-byte genesis hash into the P2P identity string. The `Identify::verify()` function performs a full string equality check against this truncated identifier. An attacker can trivially construct a custom chain whose genesis hash shares the same 4-byte prefix, pass the identity check, and be admitted as a peer on mainnet or testnet nodes — bypassing the sole network-layer chain-identity gate.

## Finding Description
`Consensus::identify_name()` at `spec/src/consensus.rs` lines 965–968 constructs the identity string as:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])  // only 8 of 64 hex chars
}
``` [1](#0-0) 

This produces a string like `"/ckb/92b197aa"`. The `id` component (`"ckb"`, `"ckb_testnet"`) is freely configurable in the chain spec. The `verify()` function in `network/src/protocols/identify/mod.rs` lines 541–551 performs a full string equality check against this truncated identifier:

```rust
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    let reader = packed::IdentifyReader::from_slice(data).ok()?;
    let name = reader.name().as_utf8().ok()?.to_owned();
    if self.name != name {
        warn!("...different network identifiers...");
        return None;
    }
    ...
}
``` [2](#0-1) 

A mismatch triggers a 5-minute ban and disconnect: [3](#0-2) [4](#0-3) 

**Exploit path:**
1. Attacker sets `name = "ckb"` in a custom chain spec (freely configurable, no key material needed).
2. Attacker iterates a mutable genesis field (e.g., `genesis.message` or timestamp) and recomputes the genesis hash until `genesis_hash_hex[..8]` matches the target network's 4-byte prefix (e.g., `"92b197aa"` for mainnet). By the birthday bound this requires ~2¹⁶ ≈ 65,536 attempts — seconds on commodity hardware.
3. Attacker runs a node on this custom chain and connects to legitimate mainnet/testnet peers.
4. `Identify::verify()` passes because `"/ckb/92b197aa" == "/ckb/92b197aa"`.
5. The attacker node is admitted as a peer and can occupy peer slots and inject sync/relay messages that consume CPU and memory before being rejected at the block-validation layer.

There are no additional chain-identity checks between the identify handshake and admission into the peer registry. The `verify_genesis` call in `util/launcher/src/lib.rs` lines 221–223 is a local startup check, not a per-peer network check. [5](#0-4) 

## Impact Explanation
Once admitted, attacker nodes can exhaust inbound peer slots, preventing legitimate peers from connecting, and can flood the sync pipeline with syntactically valid but semantically wrong messages (valid PoW for their own chain, wrong genesis ancestry for the victim). This constitutes a **bad design that could cause CKB network congestion with few costs** — matching the High (10001–15000 points) impact class. With enough attacker nodes, legitimate peer connectivity degrades significantly, and per-message CPU/memory consumption in `HeadersProcess`/`BlockProcess` accumulates before rejection.

## Likelihood Explanation
- No privileged access required; any unprivileged network peer can attempt this.
- Computational cost is negligible: ~65,536 Blake2b genesis hash computations, completable in seconds on a single CPU core.
- The `id` field (`"ckb"`, `"ckb_testnet"`) is freely settable in the chain spec with no key material or insider knowledge.
- The truncated hash is the only chain-identity signal exchanged before peer admission; there is no secondary check.
- The attack is repeatable and automatable.

## Recommendation
Replace the 8-character slice with the full 64-character hex string in `spec/src/consensus.rs`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)  // full 64-char hash
}
```

This is a non-breaking change for honest nodes (all derive the same full hash from the same genesis block) and raises the collision cost from ~2¹⁶ to ~2¹²⁸.

## Proof of Concept
1. Clone the CKB repository and create a custom chain spec with `name = "ckb"`.
2. Write a script that iterates a mutable genesis field (e.g., `genesis.message`) and calls `build_consensus()` + `consensus.genesis_hash()` until `&genesis_hash_hex[..8] == "92b197aa"` (the mainnet prefix). This loop completes in seconds.
3. Start a CKB node on this custom chain.
4. Connect to a mainnet peer. `Identify::verify()` at `network/src/protocols/identify/mod.rs:545` evaluates `self.name != name` as `false` because both sides produce `"/ckb/92b197aa"`.
5. The peer is admitted into the peer registry. The attacker node can now send sync/relay messages that consume mainnet node resources before being rejected at the block-validation layer, and occupies a peer slot that legitimate peers cannot use.

### Citations

**File:** spec/src/consensus.rs (L965-968)
```rust
    pub fn identify_name(&self) -> String {
        let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
        format!("/{}/{}", self.id, &genesis_hash[..8])
    }
```

**File:** network/src/protocols/identify/mod.rs (L24-25)
```rust
const MAX_RETURN_LISTEN_ADDRS: usize = 10;
const BAN_ON_NOT_SAME_NET: Duration = Duration::from_secs(5 * 60);
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

**File:** util/launcher/src/lib.rs (L221-223)
```rust
        // Verify genesis every time starting node
        self.verify_genesis(&shared)?;
        self.check_spec(&shared)?;
```
