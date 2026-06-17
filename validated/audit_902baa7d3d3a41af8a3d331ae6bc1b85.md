### Title
Cross-Chain Signature Replay in `PythLazer.verifyUpdate` Due to Missing Domain Separator - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` hashes only the raw payload bytes with no chain ID, contract address, or domain separator before recovering the signer. A valid signed Lazer update accepted on one EVM chain can be replayed verbatim on any other EVM chain where the same trusted signer is registered, causing consumer contracts to accept cross-chain-replayed price data.

---

### Finding Description

In `lazer/contracts/evm/src/PythLazer.sol`, the `verifyUpdate` function recovers the signer from:

```solidity
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `hash` is purely `keccak256(payload)`. The payload structure, as parsed by `PythLazerLib.parsePayloadHeader`, contains: a 4-byte format magic, an 8-byte timestamp, a 1-byte channel, a 1-byte feed count, and feed data. [2](#0-1) 

None of these fields contain a chain ID, a contract address, or any other chain-specific binding. The `PythLazerStructs.Update` struct confirms there is no chain identifier in the data model. [3](#0-2) 

Pyth Lazer is deployed on multiple EVM chains simultaneously, and the same trusted signer key is registered on all of them via `updateTrustedSigner`. [4](#0-3) 

Because the signed digest is identical across all EVM deployments, a signature that is valid on chain A is also valid on chain B.

---

### Impact Explanation

Any consumer contract that calls `verifyUpdate` and uses the returned `payload` to update on-chain state (e.g., a lending protocol using Pyth Lazer prices) can be fed a price update that was originally produced for a different EVM chain. The attacker does not need to forge a signature — they only need to observe a valid update on one chain and submit it to another. This can result in stale or contextually incorrect prices being accepted as fresh, enabling price manipulation attacks against downstream DeFi protocols.

---

### Likelihood Explanation

Pyth Lazer is explicitly multi-chain. The same trusted signer keys are registered on every EVM deployment. The `verifyUpdate` function is `external payable` and callable by any unprivileged address. An attacker only needs to monitor one chain's mempool or events, extract a valid update, and submit it to another chain. No special access or key material is required.

---

### Recommendation

Bind the signed hash to the specific chain and contract by incorporating `block.chainid` and `address(this)` into the digest before signature recovery, analogous to EIP-712 domain separation:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    keccak256(payload)
));
```

Alternatively, adopt OpenZeppelin's `EIP712` library to produce a fully standards-compliant domain-separated hash. The Pyth Lazer signing backend must be updated to include the same domain fields when producing signatures.

---

### Proof of Concept

1. Deploy `PythLazer` on chain A (e.g., Ethereum, `chainid=1`) and chain B (e.g., Arbitrum, `chainid=42161`), registering the same trusted signer on both.
2. On chain A, call `verifyUpdate{value: fee}(update)` with a freshly signed update. It succeeds and returns `(payload, signer)`.
3. Take the identical `update` bytes and call `verifyUpdate{value: fee}(update)` on chain B.
4. The call succeeds: `keccak256(payload)` is identical on both chains, `ECDSA.tryRecover` returns the same trusted signer address, and `isValidSigner(signer)` returns `true`.
5. The consumer contract on chain B accepts the cross-chain-replayed price data as legitimate. [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L31-64)
```text
    function updateTrustedSigner(
        address trustedSigner,
        uint256 expiresAt
    ) external onlyOwner {
        if (expiresAt == 0) {
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == trustedSigner) {
                    trustedSigners[i].pubkey = address(0);
                    trustedSigners[i].expiresAt = 0;
                    delete trustedSignerToExpiresAtMapping[trustedSigner];
                    return;
                }
            }
            revert("no such pubkey");
        } else {
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == trustedSigner) {
                    trustedSigners[i].expiresAt = expiresAt;
                    trustedSignerToExpiresAtMapping[trustedSigner] = expiresAt;
                    return;
                }
            }
            // Signer not found - adding a new signer.
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == address(0)) {
                    trustedSigners[i].pubkey = trustedSigner;
                    trustedSigners[i].expiresAt = expiresAt;
                    trustedSignerToExpiresAtMapping[trustedSigner] = expiresAt;
                    return;
                }
            }
            revert("no space for new signer");
        }
    }
```

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

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L74-79)
```text
    struct Update {
        uint64 timestamp;
        Channel channel;
        Feed[] feeds;
    }
}
```
