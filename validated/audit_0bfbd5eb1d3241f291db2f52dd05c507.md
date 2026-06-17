Looking at the full `verifyUpdate` implementation in `PythLazer.sol`: [1](#0-0) 

The function performs exactly three checks:
1. `msg.value >= verification_fee` [2](#0-1) 
2. EVM format magic bytes match [3](#0-2) 
3. ECDSA signer is in the trusted set and not expired [4](#0-3) 

There is no nonce, no `usedSignatures` mapping, no payload hash registry, and no timestamp freshness check against `block.timestamp`. The function is entirely stateless with respect to previously seen signatures. The same `update` bytes submitted in block N will pass all three checks identically in block N+M as long as the signer has not expired.

The payload does contain a `timestamp` field (parsed at [5](#0-4) ), but `verifyUpdate` never reads or validates it — it only hashes the raw payload bytes for ECDSA recovery.

---

### Title
Unbounded Replay of Signed Price Updates in `verifyUpdate` — (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate` contains no replay protection. Any valid `(r, s, v, payload)` tuple captured from on-chain calldata can be resubmitted verbatim in any future block and will succeed unconditionally, returning the same stale payload and signer, as long as the trusted signer has not expired.

### Finding Description
`verifyUpdate` is a `payable` function that accepts raw update bytes, recovers the ECDSA signer, and returns `(payload, signer)`. Its entire validation logic is:

```solidity
// PythLazer.sol lines 74–105
require(msg.value >= verification_fee, ...);
// magic check
bytes32 hash = keccak256(payload);
(signer,,) = ECDSA.tryRecover(hash, v, r, s);
if (!isValidSigner(signer)) revert("invalid signer");
```

There is no:
- `mapping(bytes32 => bool) usedHashes` or equivalent
- nonce embedded in the payload and tracked on-chain
- check that `payload.timestamp` is within an acceptable window of `block.timestamp`

`isValidSigner` only checks `block.timestamp < trustedSignerToExpiresAtMapping[signer]` — it says nothing about whether this specific signed message has been consumed before. [6](#0-5) 

### Impact Explanation
Any consumer contract that calls `verifyUpdate` and then uses the returned price data (e.g., to update a lending protocol's collateral price or a DEX's spot price) can be fed an arbitrarily old price. An attacker who observed a favorable price in a past block can replay that exact update bytes to inject the stale price into any consumer that does not independently enforce payload timestamp freshness. The `verifyUpdate` function name implies complete verification, making it likely that integrators will not add a second freshness check.

### Likelihood Explanation
The attack requires no privileged access. The update bytes are public calldata visible to any observer. The only precondition is that the trusted signer has not yet expired, which is the normal operating state. The attacker pays only the `verification_fee` (currently 1 wei).

### Recommendation
Add a `mapping(bytes32 => bool) private _usedPayloadHashes` and mark each `keccak256(payload)` as used on first acceptance, reverting on duplicates:

```solidity
mapping(bytes32 => bool) private _usedPayloadHashes;

// inside verifyUpdate, after signer validation:
bytes32 payloadHash = keccak256(payload);
require(!_usedPayloadHashes[payloadHash], "update already used");
_usedPayloadHashes[payloadHash] = true;
```

Alternatively (or additionally), enforce that the `timestamp` embedded in the payload is within a configurable staleness window (e.g., ≤ 60 seconds of `block.timestamp`).

### Proof of Concept
```solidity
// 1. Block N: legitimate relayer submits update
bytes memory updateBytes = ...; // captured from calldata
(bytes memory payload1, address signer1) =
    pythLazer.verifyUpdate{value: 1 wei}(updateBytes);

// 2. Block N+M (M >> 0): attacker replays identical bytes
(bytes memory payload2, address signer2) =
    pythLazer.verifyUpdate{value: 1 wei}(updateBytes);

// Both calls succeed; payload1 == payload2, signer1 == signer2
// The second call should have reverted with "update already used"
assert(keccak256(payload1) == keccak256(payload2)); // passes — no replay guard
```

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L130-131)
```text
        timestamp = _readBytes8(update, pos);
        pos += 8;
```
