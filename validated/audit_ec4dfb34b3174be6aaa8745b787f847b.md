### Title
Cross-Chain Signature Replay in Lazer Price Update Verification — (`File: lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the signed hash as `keccak256(payload)` with no chain ID, contract address, or domain separator bound into the digest. Because the same trusted signer and the same contract address (`0xACeA761c27A909d4D3895128EBe6370FDE2dF481`) are deployed across every supported EVM chain, any valid signed Lazer update accepted on one chain is cryptographically valid on every other chain.

---

### Finding Description

In `verifyUpdate`, the only data hashed before ECDSA recovery is the raw payload bytes:

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);          // ← no chain ID, no contract address
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The payload structure parsed by `parsePayloadHeader` contains only: a 4-byte magic, an 8-byte timestamp, a 1-byte channel, a 1-byte feed count, and feed data — **no chain ID field anywhere**. [2](#0-1) 

The contract is deployed at the identical address `0xACeA761c27A909d4D3895128EBe6370FDE2dF481` on Ethereum, Base, Optimism, Soneium, Polynomial, and others, all sharing the same trusted signer. [3](#0-2) 

The `EVM_FORMAT_MAGIC` constant (`706910618`) is identical across all chains — it provides no chain discrimination. [4](#0-3) 

The Sui contract has the same structural issue: `verify_le_ecdsa_message` recovers the public key from `secp256k1_ecrecover(signature, payload, 0)` where `payload` contains no chain/network identifier. [5](#0-4) 

---

### Impact Explanation

An unprivileged attacker (relayer) who observes a valid signed Lazer update on chain A can submit the identical `update` bytes to `verifyUpdate` on chain B. The function will:
1. Accept the `EVM_FORMAT_MAGIC` (same constant everywhere).
2. Compute the same `keccak256(payload)` hash.
3. Recover the same signer address.
4. Pass `isValidSigner` because the same trusted signer is registered on chain B.

The attacker can therefore inject a price update from chain A into chain B. Consumer contracts that rely on `verifyUpdate` returning a valid `(payload, signer)` pair — without independently enforcing per-chain staleness or freshness — will accept the replayed price. This enables price manipulation on the target chain using a legitimately signed but cross-chain-replayed message, which can be exploited to profit from DeFi protocols (lending, perpetuals, options) that consume Lazer prices.

---

### Likelihood Explanation

- `PythLazer` is live on 10+ EVM chains with the same contract address and the same trusted signer.
- Signed update bytes are publicly observable on-chain (calldata) or via the Lazer WebSocket stream.
- No privileged access is required; any address can call `verifyUpdate`.
- The attacker only needs to pick a signed update from chain A where the price differs from chain B's current price, then submit it to chain B.

---

### Recommendation

Bind the signed digest to the target chain and contract. The simplest fix is to include `block.chainid` and `address(this)` in the hash:

```solidity
bytes32 hash = keccak256(abi.encodePacked(block.chainid, address(this), payload));
```

Alternatively, adopt EIP-712 with a proper domain separator (`chainId` + `verifyingContract`). The Lazer protocol server must produce signatures over the chain-scoped digest for each target chain separately.

---

### Proof of Concept

1. Subscribe to the Lazer WebSocket stream and capture a signed EVM update for Base (chain ID 8453).
2. The raw `update` bytes are: `[EVM_FORMAT_MAGIC (4B)][r (32B)][s (32B)][v (1B)][payload_len (2B)][payload ...]`.
3. Submit the identical `update` bytes to `verifyUpdate` on Optimism (chain ID 10) at `0xACeA761c27A909d4D3895128EBe6370FDE2dF481`.
4. `keccak256(payload)` is identical; the recovered signer matches the trusted signer registered on Optimism; `isValidSigner` returns `true`.
5. `verifyUpdate` returns `(payload, signer)` — the Base-signed price is now accepted as valid on Optimism.
6. Any consumer contract on Optimism that calls `verifyUpdate` and trusts the returned payload will now process the replayed cross-chain price. [6](#0-5)

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

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L43-63)
```text
public(package) fun verify_le_ecdsa_message(
    state: &State,
    clock: &Clock,
    signature: &vector<u8>,
    payload: &vector<u8>,
) {
    let current_cap = state.current_cap();

    // 0 stands for keccak256 hash
    let pubkey = secp256k1_ecrecover(signature, payload, 0);

    // Check if the recovered pubkey is in the trusted signers list
    let trusted_signers = state.trusted_signers(&current_cap);
    let mut maybe_idx = trusted_signers.find_index!(|signer|
        signer.public_key() == &pubkey
    );

    assert!(maybe_idx.is_some(), ESignerNotTrusted);
    let idx = maybe_idx.extract();
    let expires_at_ms = trusted_signers[idx].expires_at_ms();
    assert!(clock.timestamp_ms() < expires_at_ms, ESignerExpired);
```
