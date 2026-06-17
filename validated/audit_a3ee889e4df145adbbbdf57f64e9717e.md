### Title
Cross-Chain Signature Replay in `PythLazer.verifyUpdate` Due to Missing Domain Separation — (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate` computes the signature hash as `keccak256(payload)` with no chain ID, contract address, or any other domain-binding context. Because the same trusted signer key is registered across all EVM deployments of `PythLazer`, a valid signed Lazer update captured on one chain passes `verifyUpdate` on every other EVM chain where the same signer is trusted. This is a direct analog to the reported vulnerability class: just as AES-CBC provides no ciphertext authenticity (enabling bit-flipping without detection), the Lazer EVM signature provides no chain-context authenticity (enabling cross-chain replay without detection).

### Finding Description

In `PythLazer.verifyUpdate`, the verification hash is computed as:

```solidity
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` bytes contain only: a format magic, a timestamp, a channel byte, and feed data. There is no chain ID, no contract address, and no nonce anywhere in the signed material. [2](#0-1) 

The same `PythLazer` contract is deployed at the same address (`0xACeA761c27A909d4D3895128EBe6370FDE2dF481`) across dozens of EVM chains (Ethereum, Arbitrum, Base, BSC, Polygon, Monad, etc.), all configured with the same trusted signer key.



Because `keccak256(payload)` is identical regardless of which chain the transaction is submitted to, a signature that is valid on chain A is cryptographically indistinguishable from a valid signature on chain B. The `isValidSigner` check only verifies key expiry, not chain binding. [3](#0-2) 

### Impact Explanation

An unprivileged attacker who observes any valid Lazer update transaction on one EVM chain (all calldata is public) can immediately replay the identical `update` bytes to `verifyUpdate` on any other EVM chain. The function returns `(payload, signer)` as if the update were freshly signed for that chain. Consumer contracts that call `verifyUpdate` and trust the returned payload receive a "verified" price update that was never intended for their chain. This breaks the integrity guarantee that `verifyUpdate` is supposed to provide — that the signed data was produced for this specific deployment context. If Pyth Lazer ever introduces chain-specific feed configurations, precision differences, or chain-targeted payloads, this replay path directly enables price manipulation on consumer protocols.

Additionally, since there is no nonce or sequence number, the same update can be submitted an unlimited number of times on the same chain, allowing an attacker to force consumer contracts to re-process stale price data within the freshness window.

**Impact: Medium** — Price integrity failure on consumer contracts; cross-chain replay of signed price updates passes on-chain verification.

### Likelihood Explanation

**Likelihood: Medium** — All Lazer update calldata is publicly observable on-chain. No privileged access is required. The attacker only needs to copy a valid `update` bytes value from one chain's transaction history and submit it to `verifyUpdate` on another chain. The same signer key is confirmed to be registered across all EVM deployments.

### Recommendation

Include a domain separator in the signed hash, following EIP-712 or a simpler approach:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

This binds each signature to a specific chain and contract instance, making cross-chain replay cryptographically impossible. Additionally, consider adding a monotonic sequence number or nonce to the payload to prevent same-chain temporal replay.

### Proof of Concept

1. On Ethereum mainnet, call `verifyUpdate{value: fee}(update)` with a valid Lazer update — it succeeds and returns `(payload, signer)`.
2. Copy the identical `update` bytes from the Ethereum transaction calldata.
3. On Arbitrum (or any other chain where `PythLazer` is deployed at `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` with the same trusted signer), call `verifyUpdate{value: fee}(update)` with the copied bytes.
4. The call succeeds and returns the same `(payload, signer)` — the Arbitrum contract accepts a payload that was never signed for Arbitrum.

The root cause is confirmed at: [4](#0-3) 

No chain ID or contract address is mixed into the hash at any point in the verification path.

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L92-105)
```text
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
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L122-135)
```text
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
```
