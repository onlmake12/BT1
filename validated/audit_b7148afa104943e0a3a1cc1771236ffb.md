### Title
Cross-Chain Signature Replay in `PythLazer.verifyUpdate` Due to Missing Chain Binding — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` hashes only the raw payload bytes (`keccak256(payload)`) with no chain ID, contract address, or domain separator included in the signed message. Because `PythLazer` is deployed at the **same deterministic address** on 20+ EVM chains, a valid Lazer price update signed for one chain is cryptographically accepted as valid on every other chain.

---

### Finding Description

In `verifyUpdate`, the hash committed to by the trusted Pyth Lazer signer is:

```solidity
bytes32 hash = keccak256(payload);
``` [1](#0-0) 

The `payload` contains only: a format magic number, a timestamp, a channel byte, feed count, and feed price data. It contains **no chain ID and no contract address**. [2](#0-1) 

The deployment script explicitly uses `CreateX` to deploy `PythLazer` to the **same deterministic address on every EVM chain**: [3](#0-2) 

This is confirmed by `EvmLazerContracts.json`, where the proxy address `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` appears identically across Ethereum, Arbitrum, BSC, Polygon, Monad, Berachain, HyperEVM, and 15+ other chains: [4](#0-3) 

Because the same signer key is registered on all chains and the signed hash contains no chain-specific context, a signature produced for chain A is byte-for-byte valid on chain B.

---

### Impact Explanation

Any unprivileged relayer can take a valid `(update, signature)` pair from chain A and submit it to `verifyUpdate` on chain B. The call will pass all checks:
- The `EVM_FORMAT_MAGIC` check passes (it is a constant, not chain-specific).
- The `ECDSA.tryRecover` recovers the same signer address regardless of chain.
- `isValidSigner` passes because the same signer is registered on all chains.

Consumer DeFi protocols (lending, perpetuals, options) that call `verifyUpdate` to obtain prices will accept the replayed, chain-mismatched price data as authentic. This can be used to inject stale or incorrect prices from one chain into another, enabling price manipulation attacks against any consumer contract on any supported chain.

---

### Likelihood Explanation

- `PythLazer` is live on 20+ EVM mainnets and testnets at the same address.
- The same trusted signer key is used across all deployments.
- No privileged access is required — any address can call `verifyUpdate` with a fee of 1 wei.
- Lazer is a high-frequency oracle; valid signed payloads are continuously available to any observer of any supported chain.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed hash, following EIP-712:

```solidity
bytes32 hash = keccak256(abi.encodePacked(
    block.chainid,
    address(this),
    payload
));
```

Or adopt a full EIP-712 domain separator. The Pyth Lazer signing infrastructure must be updated to produce signatures over the chain-bound hash.

---

### Proof of Concept

1. Observe a valid `update` bytes submitted to `verifyUpdate` on Arbitrum (chain ID 42161). The call succeeds and returns a valid `signer`.
2. Take the identical `update` bytes and submit them to `verifyUpdate` on BSC (chain ID 56) at the same contract address `0xACeA761c27A909d4D3895128EBe6370FDE2dF481`.
3. The call succeeds identically — `keccak256(payload)` is the same on both chains, the recovered signer is the same trusted address, and `isValidSigner` returns `true`.
4. A consumer contract on BSC that calls `verifyUpdate` and trusts the returned `payload` now holds price data that was signed for Arbitrum, not BSC. [5](#0-4)

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

**File:** lazer/contracts/evm/script/PythLazerDeploy.s.sol (L11-24)
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
// Below you will find more explanation on what these methods.
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
