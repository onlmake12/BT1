### Title
Cross-Chain Signature Replay in Lazer Price Update Verification — (`File: lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signature hash as `keccak256(payload)` — binding only to the raw price payload bytes. Neither `block.chainid` nor `address(this)` is included. Because the same trusted signer key is registered on every EVM deployment of `PythLazer`, a valid signed update captured on one chain can be replayed verbatim on any other EVM chain where `PythLazer` is deployed.

---

### Finding Description

In `PythLazer.verifyUpdate()`, the hash used for ECDSA recovery is:

```solidity
bytes32 hash = keccak256(payload);          // line 93
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` is the raw Lazer price bytes. It contains a timestamp, channel, and feed data, but **no chain identifier and no contract address**. The hash therefore has the same value on every EVM chain for the same price snapshot.

`PythLazer` is deployed on multiple EVM chains (Ethereum, Arbitrum, BNB Chain, etc.) and the same trusted signer address is registered on each deployment via governance: [2](#0-1) 

Because `isValidSigner` only checks expiry and not chain context, a signature that is valid on Chain A is equally valid on Chain B.

The Aptos variant (`pyth_lazer.move`) has the same structural issue — `ed25519::signature_verify_strict(&sig, &pk, message)` signs the raw message bytes with no chain or contract binding: [3](#0-2) 

---

### Impact Explanation

An unprivileged relayer can:

1. Observe a valid signed Lazer update broadcast for Chain A (e.g., Ethereum).
2. Submit the identical `update` bytes to `PythLazer.verifyUpdate()` on Chain B (e.g., Arbitrum or BNB Chain).
3. The call succeeds — the recovered signer matches a registered trusted signer on Chain B, and the consumer contract receives the verified payload.

Concrete consequences:

- **Selective staleness injection**: If the Lazer pusher stops serving Chain B (network partition, outage), an attacker can keep Chain B "live" by replaying the most recent update from Chain A. Consumer contracts that rely on the Lazer timestamp for freshness will accept the replayed data as current, even though it was never explicitly signed for Chain B.
- **Fee bypass**: The attacker pays only the `verification_fee` on Chain B; the Lazer infrastructure never signed for Chain B, violating the intended per-chain authorization model.
- **Future chain-specific feed risk**: If Pyth introduces chain-specific Lazer feeds (different precision, different assets, or chain-adjusted prices), the absence of chain binding immediately enables cross-chain price injection with no additional attacker effort.

---

### Likelihood Explanation

- `PythLazer` is already deployed on multiple EVM mainnets with the same trusted signer.
- The update bytes are publicly observable on-chain (calldata) or via the Lazer WebSocket stream.
- No privileged access is required — any address can call `verifyUpdate()` with a fee of 1 wei.
- The attack requires only copying calldata from one chain's transaction and submitting it on another.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed hash so that a signature is cryptographically bound to a specific chain and contract instance:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

The Lazer signing infrastructure must correspondingly include these fields when producing signatures. This mirrors the standard EIP-712 domain separator pattern.

For the Aptos contract, the signed `message` bytes should include the Aptos chain ID and the `@pyth_lazer` module address before the payload.

---

### Proof of Concept

1. Deploy `PythLazer` on two local Anvil forks of different chain IDs (e.g., `--chain-id 1` and `--chain-id 42161`), registering the same trusted signer on both.
2. Produce a valid signed update for chain ID 1 (the payload is `keccak256`-hashed and signed off-chain by the trusted signer key).
3. Call `verifyUpdate{value: 1 wei}(update)` on the chain-ID-42161 fork with the identical `update` bytes.
4. Observe that the call succeeds and returns the same `payload` and `signer` — no chain-specific check prevents acceptance.

The root cause is confirmed at: [1](#0-0)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L93-99)
```text
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
```

**File:** lazer/contracts/aptos/sources/pyth_lazer.move (L135-140)
```text
        let sig = ed25519::new_signature_from_bytes(signature);
        let pk = ed25519::new_unvalidated_public_key_from_bytes(trusted_signer);
        assert!(
            ed25519::signature_verify_strict(&sig, &pk, message),
            EINVALID_SIGNATURE
        );
```
