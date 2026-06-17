### Title
Cross-Chain Replay of Lazer Price Update Signatures Due to Missing Chain Binding in `verifyUpdate` Hash - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the signature hash as `keccak256(payload)` with no chain ID or contract address included. Because the same contract address and the same trusted signers are deployed across many EVM chains, a valid signed Lazer update for one chain is cryptographically accepted as valid on every other chain.

---

### Finding Description

In `PythLazer.verifyUpdate`, the hash used for ECDSA recovery is:

```solidity
bytes32 hash = keccak256(payload);
``` [1](#0-0) 

The `payload` contains only price data (a timestamp, a channel ID, and feed values). It contains no `block.chainid`, no `address(this)`, and no EIP-712 domain separator. The signature therefore carries no binding to any specific chain or contract instance. [2](#0-1) 

The `PythLazer` contract is deployed at the **identical proxy address** `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` on at least Base, Optimism Sepolia, Soneium, Sonic Blaze, Polynomial, Ethereal, and more chains simultaneously. [3](#0-2) 

The same set of trusted Lazer signers is registered on all of these deployments. Therefore, a signed payload that passes `isValidSigner` on chain A will also pass `isValidSigner` on chain B, because the recovered address is identical (same payload bytes → same hash → same recovered signer) and the signer is trusted on both chains. [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker (any Lazer relayer or transaction sender) who observes a valid `update` blob on chain A can immediately submit the identical bytes to `verifyUpdate` on chain B. The call succeeds and returns `(payload, signer)` as if the update were freshly signed for chain B. Consumer contracts that rely on `verifyUpdate` for price authenticity will accept the replayed payload.

Concrete harm: if asset prices on two chains diverge (e.g., due to bridge delays, liquidity differences, or timing), an attacker can feed the lower-price update from chain A into a lending protocol on chain B to borrow against inflated collateral, or feed the higher-price update to trigger unfair liquidations. The `verifyUpdate` function itself performs no timestamp check, so the consumer contract's own staleness guard is the only defense — and many consumer contracts may not implement one. [5](#0-4) 

---

### Likelihood Explanation

- The same contract address and the same trusted signers exist on many production EVM chains simultaneously, so no special setup is required.
- Any user can call `verifyUpdate` — it is a public, payable function requiring only the small `verification_fee`.
- Capturing a valid `update` blob is trivial: it is broadcast on-chain by the Pyth pusher on every chain and is visible in the mempool or block history.
- The attack requires no privileged access, no leaked keys, and no governance majority.

---

### Recommendation

Include `block.chainid` and `address(this)` in the signed hash, following EIP-712 domain separation:

```solidity
// Before
bytes32 hash = keccak256(payload);

// After
bytes32 hash = keccak256(abi.encodePacked(block.chainid, address(this), payload));
```

This ensures a signature produced for chain A is cryptographically invalid on chain B, directly mirroring the SparkN fix of including the `implementation` address in the digest. [1](#0-0) 

---

### Proof of Concept

1. Pyth's Lazer pusher broadcasts a signed `update` blob on Base (chain ID 8453). The blob is publicly visible on-chain.
2. An attacker copies the raw `update` bytes.
3. The attacker calls `PythLazer.verifyUpdate{value: 1 wei}(update)` on Optimism Sepolia (chain ID 11155420), where the same contract address `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` is deployed with the same trusted signer.
4. Inside `verifyUpdate`:
   - `keccak256(payload)` produces the **same hash** as on Base (payload bytes are identical).
   - `ECDSA.tryRecover` recovers the **same signer address**.
   - `isValidSigner(signer)` returns `true` (same signer is registered on Optimism Sepolia).
5. The function returns `(payload, signer)` — the Optimism Sepolia consumer contract accepts the Base price data as authentic. [5](#0-4) [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-68)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
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

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L74-78)
```text
    struct Update {
        uint64 timestamp;
        Channel channel;
        Feed[] feeds;
    }
```

**File:** contract_manager/src/store/contracts/EvmLazerContracts.json (L1-60)
```json
[
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "sepolia",
    "type": "EvmLazerContract"
  },
  {
    "address": "0x4D4772F06c595F69FB57039599a180536FDE8245",
    "chain": "ethereal_testnet_v2",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "soneium_minato_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "sonic_blaze_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "optimism_sepolia",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "soneium",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "base_sepolia",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "base",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "polynomial_testnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "polynomial",
    "type": "EvmLazerContract"
  },
  {
    "address": "0x4D4772F06c595F69FB57039599a180536FDE8245",
    "chain": "ethereal_devnet",
    "type": "EvmLazerContract"
  },
  {
    "address": "0xACeA761c27A909d4D3895128EBe6370FDE2dF481",
    "chain": "ethereal",
    "type": "EvmLazerContract"
```
