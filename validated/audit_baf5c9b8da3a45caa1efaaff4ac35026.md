### Title
Cross-Chain Replay of Signed Lazer Price Updates Due to Missing `chainId` in Signature Hash — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the signature hash as `keccak256(payload)` with no `chainId`, no contract address, and no nonce bound into the digest. Because `PythLazer` is deployed at the **same deterministic address** (`0xACeA761c27A909d4D3895128EBe6370FDE2dF481`) on 20+ EVM chains (Arbitrum, BSC, Polygon, Monad, Berachain, HyperEVM, Sonic, Injective EVM, etc.), a signed update that is valid on one chain is cryptographically valid on every other chain. Any unprivileged relayer can capture a signed update from chain A and submit it to chain B, where it will pass `verifyUpdate` and be returned as a verified payload to the consumer contract.

---

### Finding Description

In `verifyUpdate`:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);          // ← no chainId, no address, no nonce
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The Lazer payload format (parsed by `PythLazerLib.parsePayloadHeader`) contains only: a 4-byte `FORMAT_MAGIC`, an 8-byte timestamp, a 1-byte channel, and feed data. There is no `receiver_chain_id` field anywhere in the payload. [2](#0-1) 

The `EVM_FORMAT_MAGIC` constant (`706910618`) checked at line 84–86 is identical across all EVM deployments and provides no chain-specific binding. [3](#0-2) 

The deployment registry confirms the same proxy address `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` is live on arbitrum, bsc, polygon, monad, berachain, hyperevm, fantom_sonic_mainnet, injective_evm, and more. [4](#0-3) 

The deployment script explicitly uses `create3` to achieve the same address on every EVM chain. [5](#0-4) 

---

### Impact Explanation

An unprivileged attacker who observes a valid signed Lazer update on chain A can immediately submit the identical `update` bytes to `PythLazer.verifyUpdate` on chain B. The call will succeed (fee paid, signature valid, signer not expired) and return the payload to the consumer contract as if it were a freshly signed update for chain B.

Concrete consequences:
- **Stale-price injection**: An attacker captures a signed update from a moment when the price was favorable (e.g., before a large move), then replays it on a different chain after the price has moved, causing consumer DeFi protocols (lending, perpetuals) to use an outdated price.
- **Cross-chain price divergence exploitation**: If the Lazer signer ever produces chain-specific payloads (e.g., different precision or feed sets per chain), the lack of chain binding allows payloads intended for one chain to be accepted on another.
- **Within-chain replay amplification**: Because `verifyUpdate` is stateless (no used-payload tracking), the same signed update can be submitted repeatedly on the same chain within the signer's validity window, compounding the stale-price risk.

---

### Likelihood Explanation

- PythLazer is already live on 20+ EVM mainnets and testnets at the same address.
- The attack requires only reading a transaction from one chain's mempool/history and submitting it to another chain — no special privileges, no key material, no off-chain infrastructure beyond a standard RPC connection.
- Consumer contracts that rely on `verifyUpdate` for price freshness without independently checking the payload timestamp are immediately exploitable.

---

### Recommendation

Bind the signed digest to the chain and contract:

```solidity
bytes32 hash = keccak256(
    abi.encodePacked(block.chainid, address(this), payload)
);
```

Alternatively, adopt EIP-712 with a domain separator that includes `chainId` and `verifyingContract`. The Lazer signer infrastructure must produce per-chain signatures accordingly. This mirrors the fix applied in the referenced TAP contracts (PR #56), which added `chainID` to the proof hash.

---

### Proof of Concept

1. On Arbitrum, call `PythLazer(0xACeA761c27A909d4D3895128EBe6370FDE2dF481).verifyUpdate{value: 1}(update)` with a freshly signed update. Record the returned `payload`.
2. Take the identical `update` bytes and submit them to `PythLazer(0xACeA761c27A909d4D3895128EBe6370FDE2dF481).verifyUpdate{value: 1}(update)` on BSC (chainId 56).
3. The call succeeds and returns the same `payload` and `signer` — the BSC contract accepted a signature that was never produced for BSC.
4. A consumer contract on BSC that calls `verifyUpdate` and trusts the returned payload will process the Arbitrum-origin price data as if it were a valid BSC update.

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L82-87)
```text
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
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

**File:** contract_manager/src/store/contracts/EvmLazerContracts.json (L61-147)
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
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "injective_evm_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "fantom_sonic_mainnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "injective_evm",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "berachain_mainnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "hyperevm",
    "type": "EvmLazerContract"
  }
]
```

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L11-23)
```text
// This script deploys the PythLazer proxy and implementation contract using
// CreateX's contract factory to a deterministic address. Having deterministic
// addresses make it easier for users to access our contracts and also helps in
// making this deployment script idempotent without maintaining any state.
//
// CreateX enables us to deploy the contract deterministically to the same
// address on any EVM chain using various methods. We use the deployer address
// in salt to protect the deployment addresses from being redeployed by other
// wallets so the addresses we use be fully deterministic. We use `create2` to
// deploy the implementation contracts (to have a single address per
// implementation) and `create3` to deploy the proxies (to avoid changing
// addresses if our proxy contract changes, which might sound impossible, but
// can easily happen when you change the optimisation or the solc version!).
```
