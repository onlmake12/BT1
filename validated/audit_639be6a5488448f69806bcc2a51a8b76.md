### Title
Replay Attack via Missing Timestamp and Uniqueness Validation in `verifyUpdate` — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` verifies the ECDSA signature of a Lazer price update against the trusted-signer registry but performs **no replay protection**: it neither records previously processed update hashes nor validates the payload timestamp against the current block time. Any attacker who observes a valid on-chain call to `verifyUpdate` can re-submit the identical `update` bytes at any future time and receive a successful verification response, allowing stale price data to be injected into any integrating contract that does not independently enforce timestamp freshness.

---

### Finding Description

`verifyUpdate` in `lazer/contracts/evm/src/PythLazer.sol` (lines 70–106) performs three checks:

1. Fee payment (`msg.value >= verification_fee`)
2. EVM format magic number
3. ECDSA signature recovery + trusted-signer expiry check (`isValidSigner`)

```solidity
payload = update[71:71 + payload_len];
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(hash, uint8(update[68]) + 27,
    bytes32(update[4:36]), bytes32(update[36:68]));
if (signer == address(0)) revert("invalid signature");
if (!isValidSigner(signer)) revert("invalid signer");
```

The payload encodes a `uint64 timestamp` as its second field (confirmed by `PythLazerLib.parsePayloadHeader` and the test helper `buildPayload`). However, `verifyUpdate` **never reads or validates this timestamp** and **never stores the payload hash** to prevent re-use. The function is entirely stateless with respect to replay.

The analog to the external report is direct:

| External report | Pyth analog |
|---|---|
| `redis_enabled = False` disables nonce storage | No nonce/hash storage exists at all |
| Nonce becomes `None` (constant) | Payload hash is never recorded |
| Same signed message accepted repeatedly | Same `update` bytes accepted repeatedly |
| Attacker gains access to Redpanda bus | Attacker injects stale price into integrating contract |

---

### Impact Explanation

An attacker who captures a valid `update` blob from a past transaction can replay it to any contract that calls `verifyUpdate`. The function returns the original `payload` and `signer` without error. If the integrating contract does not independently enforce `_timestamp > lastTimestamp`, it will accept the stale price as current. This enables:

- **Price manipulation**: replay a favorable historical price (e.g., pre-crash high) to a lending protocol, enabling under-collateralized borrows or blocking legitimate liquidations.
- **Denial of freshness**: continuously replay an old update to prevent a newer price from being accepted by contracts that use a simple "last-write-wins" pattern.

The `verifyUpdate` function is the sole on-chain security gate for Lazer price data on EVM. Downstream contracts have no way to distinguish a fresh call from a replay because the function itself provides no replay-proof guarantee.

---

### Likelihood Explanation

`verifyUpdate` is `external payable` with no access control — any address can call it with a fee of 1 wei (the initialized `verification_fee`). The attacker needs only to:

1. Monitor the mempool or chain history for a valid `update` blob.
2. Re-submit it at a strategically chosen moment.

No privileged role, leaked key, or governance majority is required. The attack is cheap (1 wei fee) and requires only standard Ethereum transaction submission.

---

### Recommendation

**Short term**: Inside `verifyUpdate`, parse the `uint64 timestamp` from the payload header and require it to be within an acceptable staleness window of `block.timestamp`:

```solidity
uint64 payloadTimestamp = uint64(bytes8(payload[4:12])); // after PAYLOAD_MAGIC
require(block.timestamp <= payloadTimestamp / 1000 + MAX_AGE_SECONDS, "update too old");
```

**Long term**: Maintain a mapping of consumed payload hashes (analogous to Wormhole's `consumed_vaas` set) so that each signed update can only be accepted once, regardless of timestamp:

```solidity
mapping(bytes32 => bool) public consumedUpdates;
// ...
bytes32 payloadHash = keccak256(payload);
require(!consumedUpdates[payloadHash], "update already consumed");
consumedUpdates[payloadHash] = true;
```

---

### Proof of Concept

```solidity
// 1. Alice legitimately calls verifyUpdate at block T with a valid update
//    containing timestamp T_payload (price = $100).
pythLazer.verifyUpdate{value: 1 wei}(validUpdate); // succeeds, price stored as $100

// 2. Price moves to $50 at block T+1000.

// 3. Attacker replays the identical bytes at block T+1000.
//    verifyUpdate performs no staleness or replay check.
(bytes memory payload, address signer) =
    pythLazer.verifyUpdate{value: 1 wei}(validUpdate); // succeeds again

// 4. Attacker passes the returned payload to a vulnerable integrating contract.
//    The contract reads price = $100 from the stale payload and acts on it.
vulnerableProtocol.updatePrice(payload); // accepts $100 instead of $50
```

The root cause is confirmed at [1](#0-0)  — the function hashes and verifies the payload but neither records the hash nor validates the embedded timestamp against `block.timestamp`. The `isValidSigner` check at line 103 only guards against expired signers, not against replay of previously valid messages. [2](#0-1)

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
