### Title
`PythLazer.verifyUpdate()` Accepts Arbitrarily Old Price Payloads Without Staleness Enforcement — (`File: lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` verifies only the ECDSA signature and signer expiry of a Lazer price update. It performs **no check** that the `timestamp` embedded in the payload is recent relative to `block.timestamp`. Any unprivileged caller can submit a historically valid but arbitrarily old signed Lazer update, receive a verified payload back, and use the stale price for any price-sensitive on-chain operation.

---

### Finding Description

`PythLazer.verifyUpdate()` in `lazer/contracts/evm/src/PythLazer.sol` is the sole on-chain trust anchor for Pyth Lazer price data on EVM chains. Its verification logic is:

1. Check EVM format magic bytes.
2. Recover the ECDSA signer from the payload hash.
3. Call `isValidSigner(signer)` — which only checks `block.timestamp < trustedSignerToExpiresAtMapping[signer]`. [1](#0-0) 

The payload itself contains a `timestamp` field (microseconds since epoch), parsed by `PythLazerLib.parsePayloadHeader()` and exposed as `update.timestamp` in the returned `Update` struct. [2](#0-1) 

`verifyUpdate()` never reads this timestamp field. It never compares it to `block.timestamp`. There is no `maxAge` parameter, no `verifyUpdateNoOlderThan()` variant, and no revert path for a stale payload.

Contrast this with the standard Pyth pull-oracle contract, which provides `getPriceNoOlderThan(id, age)` that explicitly enforces `diff(block.timestamp, price.publishTime) <= age`: [3](#0-2) 

No equivalent guard exists anywhere in `PythLazer.sol`.

---

### Impact Explanation

An unprivileged attacker who observed a Lazer price update signed by a signer whose `expiresAt` has not yet elapsed can replay that update at any future time. The contract will return the verified (but stale) payload. Any protocol that calls `verifyUpdate()` and uses the returned price for settlement, liquidation, collateral valuation, or derivative pricing will operate on the attacker-chosen historical price rather than the current market price.

Trusted signers are registered with very long expiry windows (the test fixture uses `expiresAt = 3000000000000000`, far in the future): [4](#0-3) 

This means the replay window is effectively unbounded for the lifetime of a signer key. An attacker can select any historical price update that is maximally favorable (e.g., a price from hours or days ago) and submit it as a "verified" current price.

---

### Likelihood Explanation

- **Entry path is fully unprivileged**: `verifyUpdate()` is `external payable` with no access control beyond paying `verification_fee` (set to `1 wei` at initialization).
- **Historical signed payloads are observable**: Lazer streams are public; any observer can record signed updates.
- **No replay protection**: There is no nonce, sequence number, or monotonicity check on the payload timestamp within `verifyUpdate()`.
- **Signer keys are long-lived**: The only expiry check is on the signer key, not on the data age.

The attack requires only: (1) a recorded historical Lazer update, (2) 1 wei, and (3) a target protocol that trusts the verified payload.

---

### Recommendation

Add a `maxAge` parameter to `verifyUpdate()` (or provide a `verifyUpdateNoOlderThan(bytes calldata update, uint64 maxAge)` variant) that parses the payload timestamp and enforces:

```solidity
uint64 payloadTimestampSec = uint64(bytes8(payload[4:12])) / 1_000_000; // microseconds → seconds
if (block.timestamp > payloadTimestampSec + maxAge) revert StalePrice();
```

This mirrors the `getPriceNoOlderThan()` pattern already established in the standard Pyth EVM contract: [3](#0-2) 

The `parsePayloadHeader()` function already decodes the timestamp from the payload, so the fix requires only reading that value and comparing it to `block.timestamp` before returning. [2](#0-1) 

---

### Proof of Concept

1. At time `T`, attacker records a valid Lazer update `U_old` signed by trusted signer `S` where the payload timestamp is `T` and the price is `P_old` (favorable to attacker).
2. At time `T + Δ` (e.g., hours later), the current market price is `P_current ≠ P_old`.
3. Attacker calls `pythLazer.verifyUpdate{value: 1 wei}(U_old)`.
4. `verifyUpdate()` checks: magic ✓, signature ✓, `isValidSigner(S)` → `block.timestamp < S.expiresAt` ✓.
5. `verifyUpdate()` returns `(payload, S)` — no staleness check performed.
6. Attacker passes the verified payload to a target protocol (e.g., a perp DEX using Lazer for mark price). The protocol reads `P_old` from the payload and executes a trade at the stale price, profiting from the `P_old - P_current` discrepancy.

The `test_verify` test in `PythLazer.t.sol` confirms that `verifyUpdate()` succeeds unconditionally for any update with a valid signature from a non-expired signer, with no timestamp validation whatsoever: [5](#0-4)

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L217-225)
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

**File:** target_chains/ethereum/sdk/solidity/AbstractPyth.sol (L50-60)
```text
    function getPriceNoOlderThan(
        bytes32 id,
        uint age
    ) public view virtual override returns (PythStructs.Price memory price) {
        price = getPriceUnsafe(id);

        if (diff(block.timestamp, price.publishTime) > age)
            revert PythErrors.StalePrice();

        return price;
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
