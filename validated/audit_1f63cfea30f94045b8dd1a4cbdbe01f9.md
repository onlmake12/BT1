The code is confirmed. Let me verify one more detail about what happens after identify passes for inbound connections, and whether there are any additional checks.

Audit Report

## Title
Truncated 4-Byte Genesis Hash in `identify_name()` Allows Rogue Nodes to Pass Network Identity Check — (File: `spec/src/consensus.rs`)

## Summary
`identify_name()` constructs the P2P network identifier using only the first 8 hex characters (4 bytes) of the 32-byte genesis hash. An attacker can brute-force a custom genesis block whose hash shares the same 4-byte prefix as the mainnet genesis hash in approximately 2^32 blake2b operations — feasible on a GPU in seconds. The resulting rogue node passes the `Identify::verify()` check and is accepted as a valid mainnet peer, occupying bounded connection slots and consuming sync/relay processing resources on victim nodes.

## Finding Description
`identify_name()` in `spec/src/consensus.rs` at line 967 produces the string `"/{id}/{genesis_hash[..8]}"`, discarding 28 of the 32 hash bytes. This string is stored as `self.name` in `IdentifyCallback` and compared in `verify()` at `network/src/protocols/identify/mod.rs:545`: `if self.name != name { return None; }`. This string equality check is the sole network-membership gate. When `verify()` returns `Some`, `received_identify()` at line 452 returns `MisbehaveResult::Continue`, keeping the session alive; for outbound sessions it additionally calls `open_protocols` to open all non-Feeler protocols (sync, relay, light-client, etc.) at lines 436–443. No subsequent protocol handler re-checks chain identity — sync and relay handlers validate PoW and consensus rules on individual messages, but the session itself remains open and consuming a slot regardless. The `PeerRegistry` enforces hard caps (`max_inbound`, `max_outbound`) on accepted sessions, so rogue sessions directly displace legitimate peers.

## Impact Explanation
This maps to **High**: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs." A rogue node that passes identify occupies a permanent inbound or outbound connection slot on the victim. Because `max_inbound` and `max_outbound` are bounded, a fleet of rogue nodes can exhaust all available slots on targeted mainnet nodes, preventing legitimate peers from connecting and degrading or severing their participation in block/transaction propagation. Additionally, the rogue node can flood the victim with structurally valid but consensus-invalid compact blocks or transactions that enter the async verification pipeline before being rejected, wasting CPU and memory. The rogue node address can also be propagated via the discovery protocol to other mainnet nodes, amplifying the reach of the attack.

## Likelihood Explanation
The attacker controls all fields of their custom genesis block (timestamp, nonce, genesis cell message, system cell data). Finding a genesis block whose `blake2b_256` hash shares the same leading 4 bytes (32 bits) as the mainnet genesis hash is a preimage search over 2^32 ≈ 4 billion candidates. Modern GPUs achieve multi-billion blake2b hashes per second, making this a one-time offline computation requiring seconds to minutes. The attacker then runs a permanent rogue node with that spec. No privileged access, no key material, and no majority hashpower is required. The attack is repeatable and scalable — multiple rogue identities can be generated independently.

## Recommendation
Use the full 64-character genesis hash in `identify_name()`:

```rust
pub fn identify_name(&self) -> String {
    let genesis_hash = format!("{:x}", Into::<H256>::into(&self.genesis_hash));
    format!("/{}/{}", self.id, &genesis_hash)
}
```

This makes the network identifier cryptographically unique to the exact genesis block, raising the brute-force cost from 2^32 to 2^256 — computationally infeasible.

## Proof of Concept
1. Read the mainnet genesis hash from `resource/specs/mainnet.toml` (field `genesis.hash`); extract the first 8 hex characters.
2. Write a script that iterates over values of `genesis.genesis_cell.message` (or `genesis.nonce`) in a custom spec with `name = "ckb"`, computes the genesis block hash via `blake2b_256`, and stops when the first 8 hex characters match the mainnet prefix. With GPU acceleration this completes in seconds.
3. Start a CKB node with the crafted spec file.
4. Connect it to a mainnet node (e.g., via `ckb run --config-file crafted.toml` with a mainnet seed peer in `bootnodes`).
5. Observe in the mainnet node's logs: the identify exchange succeeds (name strings match), the session is accepted with `MisbehaveResult::Continue`, and sync/relay protocols are opened — despite the node operating on a completely different chain. The mainnet node's inbound slot count increments and is not released until the rogue node disconnects.