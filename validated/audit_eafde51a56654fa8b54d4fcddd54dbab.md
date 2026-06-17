### Title
`PythLazer.verifyUpdate()` Signed Payload Lacks Chain-ID Binding and Replay Protection, Enabling Cross-Chain and Same-Chain Replay of Price Updates — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` verifies a Lazer price-update signature by computing `keccak256(payload)` and recovering the signer. The signed payload contains no chain ID, no contract address, and no nonce. Because the same trusted-signer key is registered on every EVM deployment of `PythLazer`, any valid signed update accepted on one chain is cryptographically valid on every other chain, and can also be submitted multiple times to the same chain. An unprivileged attacker who observes a valid update on-chain can replay it to any other `PythLazer` deployment or replay it repeatedly on the same chain, causing consumer contracts that do not enforce their own timestamp monotonicity to accept stale price data.

---

### Finding Description

**Root cause — `lazer/contracts/evm/src/PythLazer.sol`, `verifyUpdate()` (lines 70–106)**

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    ...
    payload = update[71:71 + payload_len];
    bytes32 hash = keccak256(payload);          // ← hash covers only the payload bytes
    (signer, , ) = ECDSA.tryRecover(
        hash,
        uint8(update[68]) + 27,
        bytes32(update[4:36]),
        bytes32(update[36:68])
    );
    ...
    if (!isValidSigner(signer)) {
        revert("invalid signer");
    }
}
```

The payload structure (parsed by `PythLazerLib.parsePayloadHeader`) is:

```
PAYLOAD_FORMAT_MAGIC (4 bytes) | timestamp (8 bytes) | channel (1 byte) | feedsLen (1 byte) | feeds...
```

There is no `chainId`, no `contractAddress`, and no `nonce` anywhere in the signed bytes. The function is also entirely stateless — it records nothing about which update bytes have already been processed.

**Cross-chain replay path**

`PythLazer` is deployed at the same address (`0xACeA761c27A909d4D3895128EBe6370FDE2dF481`) on Ethereum, Arbitrum, BSC, Polygon, Monad, and many other chains. The same trusted-signer key is registered on all of them. Because the payload contains no chain-specific context, `keccak256(payload)` produces the identical hash on every chain, and `ECDSA.tryRecover` returns the same signer address. A valid update observed on Chain A passes `verifyUpdate` on Chain B without modification.

**Same-chain replay path**

`verifyUpdate` maintains no set of consumed update hashes. Submitting the same `update` bytes twice in the same block, or in different blocks, succeeds both times.

---

### Impact Explanation

An attacker who observes a valid Lazer update on any EVM chain (all transactions are public) can:

1. **Cross-chain replay**: Submit the update to `PythLazer` on a different chain before the Lazer service pushes a fresher update there. A consumer contract on the target chain that does not enforce `_timestamp > lastTimestamp` will accept the replayed (potentially stale) price.

2. **Same-chain replay**: Submit the same update bytes repeatedly to the same chain. A consumer that does not track which update bytes it has already processed will re-execute its price-update logic (e.g., trigger liquidations, update collateral valuations) with the same stale price on each replay.

Concrete harm: if ETH/USD drops from $3 000 to $2 000 and an attacker replays the $3 000 update to a lending protocol that uses `PythLazer` without a timestamp guard, the attacker can borrow against overvalued collateral or avoid liquidation, causing direct fund loss to the protocol.

The `PythLazer` contract itself is the necessary vulnerable step: it is the on-chain authority that certifies a payload as authentic. Because it certifies the same payload as authentic on every chain and on every submission, the trust guarantee it provides is weaker than consumers reasonably expect.

---

### Likelihood Explanation

The attacker requires no privileged access. Every Lazer update submitted to any EVM chain is publicly visible in the mempool and in block history. The attacker only needs to copy the `update` calldata and call `verifyUpdate` (paying 1 wei) on any other `PythLazer` deployment. Consumer contracts that omit a `_timestamp > lastTimestamp` guard — a common implementation mistake — are immediately exploitable.

---

### Recommendation

1. **Bind the signed payload to a chain ID and contract address.** Include `block.chainid` and `address(this)` in the data that the Lazer signer commits to, so that a signature produced for one deployment is cryptographically invalid on any other.

2. **Add on-chain replay protection.** Maintain a `mapping(bytes32 => bool) processedUpdates` keyed on `keccak256(update)` (or on `keccak256(payload)`) and revert if the same bytes are submitted twice.

3. **Enforce timestamp monotonicity inside `verifyUpdate`.** Store the last accepted timestamp per signer and revert if the incoming payload timestamp is not strictly greater, removing the burden from every consumer.

---

### Proof of Concept

**Cross-chain replay (no code changes needed):**

```solidity
// On Chain A (e.g., Ethereum), a valid update is submitted:
bytes memory update = hex"2a22999a..."; // observed from mempool or block explorer
pythLazerChainA.verifyUpdate{value: 1 wei}(update); // succeeds

// Attacker copies `update` and submits to Chain B (e.g., Arbitrum):
// PythLazer is at the same address; same trusted signer is registered.
pythLazerChainB.verifyUpdate{value: 1 wei}(update); // also succeeds — no chain binding
```

**Same-chain replay:**

```solidity
// First submission — succeeds
pythLazer.verifyUpdate{value: 1 wei}(update);

// Second submission of identical bytes — also succeeds
// verifyUpdate() has no state; keccak256(payload) is the same; signer is the same
pythLazer.verifyUpdate{value: 1 wei}(update);
```

Both calls return the same `(payload, signer)` tuple. A consumer contract that calls `verifyUpdate` inside a permissionless `updatePrice(bytes calldata update)` function and does not check `_timestamp > lastTimestamp` will update its price state on every replay, accepting stale data as authentic. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L8-11)
```text
contract PythLazer is OwnableUpgradeable, UUPSUpgradeable {
    TrustedSignerInfo[100] internal trustedSigners;
    uint256 public verification_fee;
    mapping(address => uint256) trustedSignerToExpiresAtMapping;
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
