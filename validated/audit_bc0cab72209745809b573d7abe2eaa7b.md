The key facts from the code are:

1. Both `sui_testnet` and `sui_mainnet` share `wormholeChainName: "sui"` → same Wormhole chain ID (21) → same `meta::receiver_chain_id()` constant in both deployments.
2. `governance::parse_header` checks `chain == meta::receiver_chain_id()` — passes for both.
3. `governance::process_incoming` checks emitter chain/address — same governance emitter is used for both deployments.
4. `seen_sequence` is per-`State` object — independent counters, no cross-State replay protection.

---

### Title
Cross-Deployment Governance VAA Replay: Testnet VAA Accepted by Mainnet State Due to Shared Wormhole Chain ID — (`lazer/contracts/sui/sources/governance.move`, `lazer/contracts/sui/sources/meta.move`)

### Summary

Sui mainnet and Sui testnet share the same Wormhole chain ID (21). Because `meta::receiver_chain_id()` is a compile-time constant derived from `chain.getWormholeChainId()`, and both deployments resolve to the same value, any governance VAA accepted by the testnet `State` object is structurally identical to one accepted by the mainnet `State` object. An unprivileged relayer can take a VAA that was submitted to testnet and replay it against the mainnet `State`, causing a testnet-only trusted signer key to be registered on mainnet.

### Finding Description

**Deployment configuration** (`SuiChains.json`):

Both `sui_testnet` and `sui_mainnet` are configured with `"wormholeChainName": "sui"`. [1](#0-0) 

This means `chain.getWormholeChainId()` returns the same value (Wormhole chain ID 21) for both, and the deployment script writes the same constant into `meta.move` for both: [2](#0-1) 

**Compile-time constant** (`meta.move`):

```move
public(package) fun receiver_chain_id(): u16 {
    21  // same for both mainnet and testnet
}
``` [3](#0-2) 

**`parse_header` check** (`governance.move` line 95):

```move
assert!(chain == meta::receiver_chain_id(), EMismatchedReceiverChainID);
``` [4](#0-3) 

Since both deployments have `receiver_chain_id() == 21`, a VAA with `chain = 21` in its governance payload passes this check on **both** State objects.

**`process_incoming` checks** (`governance.move` lines 61–63):

```move
assert!(self.chain_id == chain_id, EMismatchedEmitterChainID);
assert!(self.address == address, EMismatchedAddress);
assert!(self.seen_sequence < sequence, EOldSequenceNumber);
``` [5](#0-4) 

Both State objects are initialized with the same governance emitter (Pyth's Solana vault), so the first two assertions pass for both. The `seen_sequence` is per-`State` object — it prevents replaying the same VAA on the **same** State, but provides no protection against replaying a VAA from testnet's State onto mainnet's State (they have independent counters).

**`unwrap_ptgm` call chain** (`state.move`):

```move
let payload = self.governance.process_incoming(vaa);
let mut parser = parser::new(payload);
let header = governance::parse_header(&mut parser);
``` [6](#0-5) 

### Impact Explanation

An unprivileged relayer who observes a governance VAA submitted to the testnet `State` (e.g., adding a testnet-only trusted signer key) can submit the identical VAA to the mainnet `State`. All guards pass:

- Emitter chain/address: same governance emitter on both.
- `receiver_chain_id`: both return 21.
- Sequence: mainnet's `seen_sequence` is independent; if it is lower than the testnet VAA's sequence, the check passes.

Result: a testnet-controlled key is registered as a trusted signer on mainnet. Any Lazer price update signed by that testnet key will be accepted as authentic on mainnet.

### Likelihood Explanation

- Requires no privileged access — any observer of the testnet transaction can extract the VAA bytes and submit them on mainnet.
- Governance operations on testnet (key rotation, testing new signers) are routine.
- The sequence number gap between testnet and mainnet is likely to exist in practice, since testnet governance operations are more frequent.

### Recommendation

Assign distinct Wormhole chain IDs to Sui mainnet and Sui testnet, or introduce a separate deployment-environment discriminator (e.g., a `network_id` field stored in `State` at init time and checked against a field in the governance payload). The `receiver_chain_id` check is the intended isolation mechanism, but it is defeated when both deployments share the same Wormhole chain name.

### Proof of Concept

1. Deploy two `State` objects with identical `meta::receiver_chain_id() = 21` and the same governance emitter (mainnet vault).
2. Construct a governance VAA (signed by the governance emitter) with `chain = 21`, `sequence = 5`, adding a testnet-only public key as a trusted signer.
3. Submit the VAA to the testnet `State` (sequence 5 > 0 → accepted; testnet `seen_sequence` becomes 5).
4. Submit the **same** VAA bytes to the mainnet `State` (sequence 5 > 0 → accepted; mainnet `seen_sequence` becomes 5).
5. Assert that the testnet public key is now present in mainnet's `trusted_signers` — the invariant is broken.

### Citations

**File:** contract_manager/src/store/chains/SuiChains.json (L1-15)
```json
[
  {
    "id": "sui_testnet",
    "mainnet": false,
    "rpcUrl": "https://fullnode.testnet.sui.io:443",
    "type": "SuiChain",
    "wormholeChainName": "sui"
  },
  {
    "id": "sui_mainnet",
    "mainnet": true,
    "rpcUrl": "https://fullnode.mainnet.sui.io:443",
    "type": "SuiChain",
    "wormholeChainName": "sui"
  },
```

**File:** contract_manager/scripts/manage_sui_lazer_contract.ts (L190-194)
```typescript
      const meta = {
        receiver_chain_id: chain.getWormholeChainId(),
        version: "1",
      };
      await chain.updateLazerMeta(packagePath, meta);
```

**File:** lazer/contracts/sui/sources/meta.move (L18-20)
```text
public(package) fun receiver_chain_id(): u16 {
    21
}
```

**File:** lazer/contracts/sui/sources/governance.move (L61-64)
```text
    assert!(self.chain_id == chain_id, EMismatchedEmitterChainID);
    assert!(self.address == address, EMismatchedAddress);
    assert!(self.seen_sequence < sequence, EOldSequenceNumber);
    self.seen_sequence = sequence;
```

**File:** lazer/contracts/sui/sources/governance.move (L94-96)
```text
    let chain = parser.take_u16_be();
    assert!(chain == meta::receiver_chain_id(), EMismatchedReceiverChainID);
    GovernanceHeader { action }
```

**File:** lazer/contracts/sui/sources/state.move (L74-77)
```text
    let payload = self.governance.process_incoming(vaa);
    let mut parser = parser::new(payload);
    let header = governance::parse_header(&mut parser);
    (header, parser)
```
