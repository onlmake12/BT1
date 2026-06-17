### Title
Lazer Price Update Signed Data Lacks Chain ID, Enabling Cross-EVM-Chain Replay — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` computes the signature hash as `keccak256(payload)` over the raw payload bytes only. No EVM chain ID is included in the signed data. Because the same trusted signers and the same `EVM_FORMAT_MAGIC` are used across every EVM deployment of PythLazer, a valid signed Lazer update accepted on one EVM chain (e.g., Ethereum mainnet) is cryptographically indistinguishable from a valid update on any other EVM chain (e.g., Arbitrum, Optimism, Base). An unprivileged relayer can capture a signed update from chain A and submit it verbatim to chain B.

---

### Finding Description

In `verifyUpdate`, the hash committed to by the Pyth Lazer signer is:

```solidity
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(
    hash,
    uint8(update[68]) + 27,
    bytes32(update[4:36]),
    bytes32(update[36:68])
);
``` [1](#0-0) 

The `payload` is parsed by `PythLazerLib.parsePayloadHeader` and contains only: a 4-byte format magic, an 8-byte timestamp, a 1-byte channel, a 1-byte feed count, and the feed data. [2](#0-1) 

None of these fields carry an EVM chain ID. The `EVM_FORMAT_MAGIC = 706910618` is a constant shared across all EVM deployments. [3](#0-2) 

The `PythLazerStructs.Update` struct (`timestamp`, `channel`, `feeds`) likewise contains no chain-specific field. [4](#0-3) 

---

### Impact Explanation

Any unprivileged actor who observes a valid signed Lazer update on one EVM chain can replay it on any other EVM chain where `PythLazer` is deployed. The `verifyUpdate` call on the target chain will succeed: the recovered `signer` will match a trusted signer, and the returned `payload` will be accepted as authentic. Consumers that do not independently enforce chain-specific context or tight timestamp windows will process the replayed data as if it were freshly signed for their chain. In the worst case — e.g., a signer trusted on a testnet deployment is also trusted on mainnet — testnet-signed prices can be injected into mainnet consumers.

---

### Likelihood Explanation

PythLazer is deployed on multiple EVM chains. The same trusted signer keys are registered across deployments. Any Lazer relayer or observer can capture a signed binary update from one chain's mempool or event log and resubmit it to another chain's `verifyUpdate` with no modification. No privileged access is required.

---

### Recommendation

Include the EVM chain ID in the data committed to by the signature. For example, hash the chain ID together with the payload before recovery:

```solidity
bytes32 hash = keccak256(abi.encodePacked(block.chainid, payload));
```

This ensures a signature produced for chain A is invalid on chain B. The Pyth Lazer signer infrastructure must correspondingly include `block.chainid` (or the equivalent chain identifier) when producing the ECDSA signature over each EVM-formatted update.

---

### Proof of Concept

1. Deploy `PythLazer` on two EVM chains (e.g., Ethereum Sepolia and Arbitrum Sepolia) with the same trusted signer.
2. On chain A, call `verifyUpdate{value: fee}(update)` with a freshly signed Lazer update. Observe it succeeds and emits the payload.
3. On chain B, call `verifyUpdate{value: fee}(update)` with the **identical** `update` bytes from step 2.
4. Observe that `verifyUpdate` succeeds on chain B: `signer != address(0)` and `isValidSigner(signer) == true`, even though the update was signed exclusively for chain A.

The root cause is confirmed at: [5](#0-4)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L82-86)
```text
        uint32 EVM_FORMAT_MAGIC = 706910618;

        uint32 evm_magic = uint32(bytes4(update[0:4]));
        if (evm_magic != EVM_FORMAT_MAGIC) {
            revert("invalid evm magic");
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L93-105)
```text
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

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L74-78)
```text
    struct Update {
        uint64 timestamp;
        Channel channel;
        Feed[] feeds;
    }
```
