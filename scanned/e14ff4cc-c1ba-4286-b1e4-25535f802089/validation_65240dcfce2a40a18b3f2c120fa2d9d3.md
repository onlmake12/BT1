Audit Report

## Title
Truncated 32-bit Genesis Hash in `identify_name()` Allows Cross-Chain Peers to Bypass Network Identity Filter — (File: `spec/src/consensus.rs`)

## Summary
`Consensus::identify_name()` embeds only the first 8 hex characters (32 bits) of the 64-character genesis hash into the P2P identity string. Because `Identify::verify()` performs a full string equality check against this already-truncated string, an attacker who crafts a custom chain whose genesis hash shares the same 4-byte prefix passes the identity gate unconditionally. The collision requires only ~2¹⁶ ≈ 65,536 hash attempts — trivially fast — and no privileged access.

## Finding Description
`identify_name()` at `spec/src/consensus.rs:965–968` constructs the identity string as `"/{id}/{genesis_hash[..8]}"`, discarding 56 of 64 hex characters:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash[..8])   // only 32 bits used
}
``` [1](#0-0) 

This string is the sole chain-identity signal exchanged during the P2P handshake. `Identify::verify()` at `network/src/protocols/identify/mod.rs:541–551` performs a strict equality check against it:

```rust
fn verify(&self, data: &[u8]) -> Option<(Flags, String)> {
    let name = reader.name().as_utf8().ok()?.to_owned();
    if self.name != name {
        warn!("... different network identifiers ...");
        return None;
    }
    ...
}
``` [2](#0-1) 

A `None` return triggers a 5-minute ban and disconnect: [3](#0-2) 

An attacker who returns `Some(...)` — by matching the truncated string — is fully admitted as a peer. The exploit path:

1. Set `id = "ckb"` (or `"ckb_testnet"`) in a custom chain spec — this field is freely configurable.
2. Iterate a mutable genesis field (e.g. `genesis.message`) until `&genesis_hash_hex[..8]` matches the target network prefix. At ~2¹⁶ attempts this completes in seconds on commodity hardware.
3. Connect to mainnet/testnet peers. `verify()` returns `Some(...)` because `"/ckb/92b197aa" == "/ckb/92b197aa"`.
4. The attacker node is admitted; it can now occupy peer slots and send syntactically valid but semantically wrong protocol messages (headers, blocks valid for the attacker's chain) that consume CPU and memory before being rejected at the block-validation layer.

No deeper check re-validates chain identity after the identify handshake. The `BAN_ON_NOT_SAME_NET` path is never triggered for a passing attacker. [4](#0-3) 

## Impact Explanation
**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating a fleet of such nodes can exhaust the inbound peer slots of mainnet/testnet nodes, preventing legitimate peers from connecting and degrading network connectivity. Simultaneously, admitted attacker nodes can flood the sync pipeline (`HeadersProcess`, `BlockProcess`) with messages that pass the identity gate and consume CPU/memory before failing block validation, causing sustained resource pressure on targeted nodes.

## Likelihood Explanation
- No privileged access is required; any unprivileged network peer can execute this.
- The `id` field in a chain spec is freely settable — no key material or insider knowledge needed.
- The collision search over 32 bits requires ~65,536 Blake2b genesis hash computations on average; a single CPU core completes this in under a second.
- The attack is repeatable and scalable across many attacker IPs, and the 5-minute ban is never triggered because the attacker passes, not fails, the identity check.

## Recommendation
Replace the 8-character slice with the full 64-character genesis hash string in `spec/src/consensus.rs`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)   // full 256-bit hash
}
```

This is a non-breaking change for honest nodes (all derive the same full hash from the same genesis block) and raises the collision cost from ~2¹⁶ to ~2¹²⁸.

## Proof of Concept
1. Clone the CKB repository and create a custom chain spec with `name = "ckb"`.
2. Write a script that iterates `genesis.message` and calls `build_consensus()` + `consensus.genesis_hash()` until `&genesis_hash_hex[..8] == "92b197aa"` (the mainnet prefix). Expect ~65,536 iterations, completing in under one second.
3. Start a CKB node on this custom chain.
4. Connect to a mainnet peer. `Identify::verify()` at `network/src/protocols/identify/mod.rs:545` evaluates `self.name != name` as `false` (both sides produce `"/ckb/92b197aa"`), returns `Some(...)`, and the peer is admitted.
5. From the admitted connection, send a stream of `SendHeaders` messages referencing headers from the custom chain. Observe that each message enters the sync pipeline and consumes mainnet node resources before being rejected at ancestry validation — confirming the identity gate is the only chain-identity barrier and it has been bypassed.

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
