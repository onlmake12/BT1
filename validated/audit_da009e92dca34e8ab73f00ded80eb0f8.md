### Title
Pyth Lazer Signed Payloads Lack Chain ID and Contract Address Binding, Enabling Cross-Chain Replay — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signed hash as `keccak256(payload)` over raw price data only. The payload contains no chain ID, no contract address, and no domain separator. Because the same trusted signer keys are registered across every EVM deployment of `PythLazer`, a valid signed update captured from one chain can be replayed verbatim on any other chain where `PythLazer` is deployed, and the signature will verify successfully.

---

### Finding Description

In `PythLazer.sol`, `verifyUpdate` extracts the payload and computes:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(hash, uint8(update[68]) + 27,
    bytes32(update[4:36]), bytes32(update[36:68]));
``` [1](#0-0) 

The payload structure (confirmed in `PythLazerLib.sol`) is:

```
uint32 PAYLOAD_FORMAT_MAGIC | uint64 timestamp | uint8 channel | uint8 feedsLen | feed data...
``` [2](#0-1) 

None of these fields encode the destination chain ID or the `PythLazer` contract address. The `EVM_FORMAT_MAGIC` (`706910618`) is also a static constant identical across all EVM deployments. [3](#0-2) 

The same root cause exists in the Aptos contract, where `ed25519::signature_verify_strict(&sig, &pk, message)` signs the raw `message` bytes with no chain or contract binding: [4](#0-3) 

And in the Sui contract, where `secp256k1_ecrecover(signature, payload, 0)` recovers the signer from the raw payload: [5](#0-4) 

---

### Impact Explanation

An unprivileged attacker who observes a valid `verifyUpdate` call on Chain A (e.g., Ethereum) can extract the `update` bytes and submit them to `PythLazer.verifyUpdate` on Chain B (e.g., Arbitrum, BNB Chain, Optimism). Because the trusted signer set is shared across all EVM deployments and the hash covers only the payload, the signature check passes on Chain B without any modification.

Consumer contracts that call `verifyUpdate` and act on the returned `(payload, signer)` tuple receive no cryptographic guarantee that the update was intended for their chain. Any downstream protocol that relies on `verifyUpdate` for chain-specific authorization is affected. If Pyth Lazer ever introduces chain-specific channels, pricing, or access tiers, this replay path immediately enables price manipulation on the target chain.

---

### Likelihood Explanation

`PythLazer` is already deployed on many EVM mainnets (Ethereum, Arbitrum, Optimism, BNB Chain, Base, and others per the contract address documentation). Every signed update broadcast by the Lazer network is observable on-chain or via the WebSocket stream. Any user of the Lazer SDK receives the raw `evm` bytes that are directly replayable. No special access, leaked key, or privileged role is required — only the ability to submit a transaction on the target chain.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed hash, following EIP-712 domain separator conventions:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

Apply the equivalent fix to the Aptos and Sui contracts by prepending the chain identifier and contract/module address to the message before signing and verifying.

---

### Proof of Concept

1. On Chain A (e.g., Ethereum), call `PythLazer.verifyUpdate{value: fee}(update)` with a valid signed Lazer update. Record the `update` bytes from the calldata.
2. On Chain B (e.g., Arbitrum), submit the identical `update` bytes to `PythLazer.verifyUpdate{value: fee}(update)`.
3. The call succeeds: `isValidSigner(signer)` returns `true` because the same trusted signer is registered on both chains, and `keccak256(payload)` produces the same hash on both chains since neither `block.chainid` nor `address(this)` is included.
4. The consumer contract on Chain B accepts the payload as a legitimately verified Lazer update for that chain. [6](#0-5)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-106)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }

        if (update.length < 71) {
            revert("input too short");
        }
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
        }
        uint16 payload_len = uint16(bytes2(update[69:71]));
        if (update.length < 71 + payload_len) {
            revert("input too short");
        }
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
        if (signer == address(0)) {
            revert("invalid signature");
        }
        if (!isValidSigner(signer)) {
            revert("invalid signer");
        }
    }
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L110-136)
```text
    function parsePayloadHeader(
        bytes memory update
    )
        public
        pure
        returns (
            uint64 timestamp,
            PythLazerStructs.Channel channel,
            uint8 feedsLen,
            uint16 pos
        )
    {
        uint32 FORMAT_MAGIC = 2479346549;

        pos = 0;
        uint32 magic = _readBytes4(update, pos);
        pos += 4;
        if (magic != FORMAT_MAGIC) {
            revert("invalid magic");
        }
        timestamp = _readBytes8(update, pos);
        pos += 8;
        channel = PythLazerStructs.Channel(_readBytes1(update, pos));
        pos += 1;
        feedsLen = uint8(_readBytes1(update, pos));
        pos += 1;
    }
```

**File:** lazer/contracts/aptos/sources/pyth_lazer.move (L134-140)
```text
        // Verify signature
        let sig = ed25519::new_signature_from_bytes(signature);
        let pk = ed25519::new_unvalidated_public_key_from_bytes(trusted_signer);
        assert!(
            ed25519::signature_verify_strict(&sig, &pk, message),
            EINVALID_SIGNATURE
        );
```

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L51-52)
```text
    // 0 stands for keccak256 hash
    let pubkey = secp256k1_ecrecover(signature, payload, 0);
```
