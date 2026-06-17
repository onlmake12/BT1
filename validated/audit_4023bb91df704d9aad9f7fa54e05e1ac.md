### Title
Cross-Chain Replay of Lazer Price Update Signatures Due to Missing Chain ID and Contract Address in Hash — (`File: lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` computes the signature hash as `keccak256(payload)` only. Neither `block.chainid` nor `address(this)` is included. Because the same `PythLazer` proxy is deployed at the identical address (`0xACeA761c27A909d4D3895128EBe6370FDE2dF481`) across dozens of EVM chains, and the same trusted signers are registered on all of them, a valid signed update captured on one chain passes signature verification on every other chain.

---

### Finding Description

In `verifyUpdate`, the hash committed to by the trusted Lazer signer is reconstructed on-chain as:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` itself contains only: a format magic, a timestamp, a channel byte, and feed data. It contains **no chain ID and no contract address**. [2](#0-1) 

The contract is deployed at the same proxy address on all supported EVM chains: [3](#0-2) 

The same trusted signer keys are registered across all deployments via `updateTrustedSigner`. Because `isValidSigner` only checks expiry and not chain context, a signature produced for chain A is cryptographically indistinguishable from one produced for chain B. [4](#0-3) 

---

### Impact Explanation

An attacker who observes a valid Lazer price update on chain A (e.g., Ethereum mainnet) can immediately submit the identical `update` bytes to `PythLazer.verifyUpdate()` on chain B (e.g., Arbitrum, BSC, Polygon, Monad). The call succeeds and returns the verified `payload` and `signer`. Any consumer contract on chain B that relies on `verifyUpdate` to authenticate the data will accept a price update that was never intended for that chain. This enables:

- Feeding a price from one chain's market conditions into a DeFi protocol on a different chain.
- Bypassing any chain-specific staleness or sequencing logic that the consumer contract might implement, since the timestamp in the payload is genuine (just from the wrong chain).

---

### Likelihood Explanation

The attack requires no privileged access. Any unprivileged user can:
1. Observe a valid Lazer update on any public EVM chain (the update bytes are submitted in calldata and are publicly visible).
2. Immediately call `verifyUpdate` on a different chain with the same bytes, paying only the `verification_fee` (1 wei).

The `PythLazer` contract is live on many chains simultaneously with the same trusted signers, making this trivially executable. The only practical constraint is the timestamp in the payload becoming stale, but within the same block or within a few seconds the replay is fully valid.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed hash, analogous to EIP-712 domain separation:

```solidity
bytes32 hash = keccak256(abi.encodePacked(block.chainid, address(this), payload));
```

This requires the off-chain Lazer signer to also include these fields when producing signatures, which is the standard practice for cross-chain-safe ECDSA signing.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {PythLazer} from "lazer/contracts/evm/src/PythLazer.sol";

contract LazerCrossChainReplayTest {
    // PythLazer deployed at same address on Ethereum (chainId=1) and Arbitrum (chainId=42161)
    PythLazer constant PYTH_LAZER = PythLazer(0xACeA761c27A909d4D3895128EBe6370FDE2dF481);

    function demonstrateReplay(bytes calldata updateCapturedFromEthereum) external payable {
        // This call is made on Arbitrum (chainId=42161).
        // `updateCapturedFromEthereum` was a valid update on Ethereum (chainId=1).
        // Because hash = keccak256(payload) with no chainid or address(this),
        // the signature is equally valid here.
        (bytes memory payload, address signer) =
            PYTH_LAZER.verifyUpdate{value: PYTH_LAZER.verification_fee()}(updateCapturedFromEthereum);

        // verifyUpdate succeeds — payload and signer are returned as if the update
        // was legitimately produced for Arbitrum.
        // Any consumer contract calling verifyUpdate will now accept Ethereum price data
        // as valid Arbitrum price data.
    }
}
```

The root cause is at: [1](#0-0)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L92-99)
```text
        payload = update[71:71 + payload_len];
        bytes32 hash = keccak256(payload);
        (signer, , ) = ECDSA.tryRecover(
            hash,
            uint8(update[68]) + 27,
            bytes32(update[4:36]),
            bytes32(update[36:68])
        );
```

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L122-136)
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
    }
```

**File:** contract_manager/src/store/contracts/EvmLazerContracts.json (L61-120)
```json
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "arbitrum_sepolia",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "arc_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "tempo_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "tempo",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "megaeth",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "polygon",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "arbitrum",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "bsc_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "bsc",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "fluent",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "monad_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "monad",
    "type": "EvmLazerContract"
```
