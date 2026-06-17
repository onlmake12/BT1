### Title
Lazer `verifyUpdate()` Does Not Enforce Payload Timestamp Staleness, Allowing Indefinite Replay of Old Signed Price Updates - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` verifies the ECDSA signature and checks that the signer key has not expired, but it never compares the `timestamp` embedded in the signed payload against `block.timestamp`. Any validly-signed Lazer price update can be replayed indefinitely — as long as the signer key is still active — regardless of how old the price data is.

---

### Finding Description

`PythLazer.verifyUpdate()` performs two checks:

1. The ECDSA signature recovers to a non-zero address.
2. The recovered signer address satisfies `isValidSigner()`, i.e., `block.timestamp < trustedSignerToExpiresAtMapping[signer]`. [1](#0-0) 

Neither check involves the `timestamp` field that is encoded inside the signed payload. The payload format is well-defined: after the 4-byte magic, the next 8 bytes are the price-capture timestamp (parsed by `parsePayloadHeader`). [2](#0-1) 

The `Update` struct returned after parsing carries this `timestamp`: [3](#0-2) 

Because `verifyUpdate()` returns `(bytes calldata payload, address signer)` without any staleness assertion, an attacker can submit a payload signed hours or days ago and the function will return successfully. The signer key expiry (`expiresAt`) governs only the *signing key's* validity window, not the *data's* freshness.

The analog to the external report is direct: the signed credit struct had no `deadline` field enforced on-chain; here the signed price payload has a `timestamp` field that is never compared to `block.timestamp` inside the verification function.

---

### Impact Explanation

Any DeFi protocol that calls `verifyUpdate()` and treats a non-revert as a freshness guarantee will accept arbitrarily stale prices. An attacker who captured a signed Lazer update when, for example, ETH was priced at $1,000 can replay it later when the real price is $4,000. The verification contract will accept it as valid. This enables:

- Borrowing against inflated collateral using a stale high price.
- Triggering false liquidations using a stale low price.
- Any price-sensitive DeFi action that relies on Lazer for its oracle.

The documentation example shows consumers are expected to check `_timestamp > timestamp` (monotonicity), but this only prevents *older-than-stored* replays; it does not prevent replaying a payload that is, say, 30 minutes old against a contract that has never been updated. [4](#0-3) 

---

### Likelihood Explanation

- Any unprivileged caller can invoke `verifyUpdate()` with a historical payload; no special role is required.
- Signed Lazer payloads are broadcast publicly over WebSocket streams and are trivially captured and stored.
- The signer key expiry is set far in the future (e.g., the test fixture uses `3000000000000000`), so the replay window is effectively unbounded.
- Consumer contracts that do not independently enforce a staleness threshold (e.g., `require(update.timestamp + MAX_AGE > block.timestamp)`) are fully exposed. [5](#0-4) 

---

### Recommendation

Add a `maxAge` parameter (or a hardcoded staleness bound) to `verifyUpdate()` and revert if the payload timestamp is too old:

```solidity
// After recovering payload:
uint64 payloadTimestamp = uint64(bytes8(payload[4:12])); // skip 4-byte magic
require(
    block.timestamp <= uint256(payloadTimestamp) + maxAge,
    "price update too stale"
);
```

Alternatively, expose a `verifyUpdateWithMaxAge(bytes calldata update, uint256 maxAge)` variant so callers can enforce freshness at the protocol level rather than relying on each consumer to do so correctly.

---

### Proof of Concept

1. At time T, a Lazer publisher signs a payload with `timestamp = T` and `ETH price = $1,000`. The payload is captured from the public WebSocket stream.
2. At time T + 2 hours, the real ETH price is $4,000.
3. Attacker calls `pythLazer.verifyUpdate{value: fee}(oldPayload)`.
4. `isValidSigner` passes because the signer key has not expired.
5. No staleness check exists; `verifyUpdate` returns `(payload, signer)` without reverting.
6. Attacker passes the returned payload to a lending protocol that calls `PythLazerLib.parseUpdateFromPayload(payload)` and reads `update.timestamp = T` and `price = $1,000`.
7. The lending protocol, seeing a "verified" price of $1,000, allows the attacker to borrow against ETH collateral at the stale valuation. [1](#0-0) [6](#0-5)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L66-106)
```text
    function isValidSigner(address signer) public view returns (bool) {
        return block.timestamp < trustedSignerToExpiresAtMapping[signer];
    }

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L217-226)
```text
    function parseUpdateFromPayload(
        bytes memory payload
    ) public pure returns (PythLazerStructs.Update memory update) {
        // Parse payload header
        uint16 pos;
        uint8 feedsLen;
        (update.timestamp, update.channel, feedsLen, pos) = parsePayloadHeader(
            payload
        );

```

**File:** lazer/contracts/evm/src/PythLazerStructs.sol (L74-78)
```text
    struct Update {
        uint64 timestamp;
        Channel channel;
        Feed[] feeds;
    }
```

**File:** lazer/contracts/evm/test/PythLazer.t.sol (L45-75)
```text
    function test_verify() public {
        // Prepare dummy update and signer
        address trustedSigner = 0xb8d50f0bAE75BF6E03c104903d7C3aFc4a6596Da;
        vm.prank(owner);
        pythLazer.updateTrustedSigner(trustedSigner, 3000000000000000);
        bytes
            memory update = hex"2a22999a9ee4e2a3df5affd0ad8c7c46c96d3b5ef197dd653bedd8f44a4b6b69b767fbc66341e80b80acb09ead98c60d169b9a99657ebada101f447378f227bffbc69d3d01003493c7d37500062cf28659c1e801010000000605000000000005f5e10002000000000000000001000000000000000003000104fff8";

        uint256 fee = pythLazer.verification_fee();

        address alice = makeAddr("alice");
        vm.deal(alice, 1 ether);
        address bob = makeAddr("bob");
        vm.deal(bob, 1 ether);

        // Alice provides appropriate fee
        vm.prank(alice);
        pythLazer.verifyUpdate{value: fee}(update);
        assertEq(alice.balance, 1 ether - fee);

        // Alice overpays and is refunded
        vm.prank(alice);
        pythLazer.verifyUpdate{value: 0.5 ether}(update);
        assertEq(alice.balance, 1 ether - fee - fee);

        // Bob does not attach a fee
        vm.prank(bob);
        vm.expectRevert("Insufficient fee provided");
        pythLazer.verifyUpdate(update);
        assertEq(bob.balance, 1 ether);
    }
```
