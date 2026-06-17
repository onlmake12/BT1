### Title
`PythLazer.verifyUpdate` Accepts Arbitrarily Old Price Updates — No Payload Timestamp Freshness Check (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` verifies the ECDSA signature and checks that the signer key has not expired, but it never validates that the **timestamp embedded in the payload** is recent relative to `block.timestamp`. Any validly-signed Lazer price update — regardless of age — passes `verifyUpdate` successfully. This is the direct analog of the SYMM-IO nonce-less liquidation signature: the signed message does not bind to current chain state, so a stale signed message can be submitted after conditions have changed.

---

### Finding Description

`verifyUpdate` in `PythLazer.sol` performs three checks:

1. EVM format magic validation
2. ECDSA signature recovery
3. Signer key expiry check (`block.timestamp < trustedSignerToExpiresAtMapping[signer]`) [1](#0-0) 

The payload returned to the caller contains a `timestamp` field (parsed by `PythLazerLib.parsePayloadHeader`), which represents when the Lazer service produced the price update. [2](#0-1) 

`verifyUpdate` never compares this payload timestamp against `block.timestamp`. The signer key expiry (e.g., set to a date years in the future) is the only time-based gate, and it guards the *signer's validity*, not the *data's freshness*. A price update signed 6 hours ago — or 6 months ago — passes `verifyUpdate` identically to one signed 1 second ago.

The Sui implementation has the same gap: `parse_and_verify_le_ecdsa_update_v2` checks `clock.timestamp_ms() < expires_at_ms` for the signer but never checks the payload's own timestamp against the clock. [3](#0-2) 

---

### Impact Explanation

Consumer contracts that call `verifyUpdate` and then use the returned payload to update on-chain prices (e.g., for liquidations, collateral valuation, or settlement) will accept arbitrarily old prices. An attacker who has captured a valid Lazer update from the past (all updates are publicly streamed) can submit it to a consumer contract at a strategically chosen moment — for example, when the real current price has moved significantly — causing the contract to act on a stale price. This can lead to:

- Incorrect liquidations (liquidating solvent positions using an old low price)
- Incorrect collateral valuations (borrowing against an inflated old price)
- Incorrect settlement prices

This is the direct analog of the SYMM-IO finding: the signed message does not bind to current state (no freshness check), so it remains valid and usable after the underlying conditions have changed.

---

### Likelihood Explanation

- All Lazer price updates are publicly broadcast over the WebSocket stream; any observer can capture and store them.
- The attacker only needs to wait for a price move and then replay an old update to a consumer contract.
- Consumer contracts that follow the documentation example check `_timestamp > storedTimestamp` (monotonicity), but this does not prevent using a price that is hours old if the stored timestamp is also old (e.g., on a low-activity chain or after a period of no updates).
- The `verifyUpdate` function is the designated security boundary; consumers reasonably trust that a passing call means the data is legitimate and current. [4](#0-3) 

---

### Recommendation

Add a configurable maximum age check inside `verifyUpdate`. After recovering the signer and confirming it is trusted, parse the payload timestamp and require it to be within an acceptable window of `block.timestamp`:

```solidity
// After confirming isValidSigner(signer):
uint64 payloadTimestamp = uint64(bytes8(payload[4:12])); // after 4-byte magic
require(
    block.timestamp <= payloadTimestamp / 1e6 + MAX_AGE_SECONDS,
    "price update too old"
);
```

Alternatively, expose a `verifyUpdateWithMaxAge(bytes calldata update, uint256 maxAgeSeconds)` variant that enforces freshness, and deprecate the unchecked `verifyUpdate`. This mirrors how Pyth Core's `getPriceNoOlderThan` enforces freshness at the read layer. [5](#0-4) 

---

### Proof of Concept

1. At time T, the Lazer stream publishes a signed update for feed ID 1 with price = 100. An attacker saves this update blob.
2. At time T+3600 (1 hour later), the real price has moved to 80.
3. The attacker calls `pythLazer.verifyUpdate{value: fee}(savedUpdate)`. The call succeeds — the signer key is still valid, and no timestamp check is performed.
4. The attacker passes the returned payload to a consumer contract (e.g., a lending protocol). The consumer contract reads price = 100 and allows the attacker to borrow against inflated collateral, or avoids a liquidation that should have occurred at price 80.

The same `test_verify` test in the Lazer test suite confirms that `verifyUpdate` succeeds with a fixed historical update blob regardless of `block.timestamp`: [6](#0-5)

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

**File:** lazer/contracts/evm/src/PythLazerLib.sol (L211-226)
```text
    }

    /// @notice Parse complete update from payload bytes
    /// @dev This is the main entry point for parsing a verified payload into the Update struct
    /// @param payload The payload bytes (after signature verification)
    /// @return update The parsed Update struct containing all feeds and their properties
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

**File:** apps/developer-hub/content/docs/price-feeds/pro/integrate-as-consumer/evm.mdx (L61-116)
```text

```solidity copy
function updatePrice(bytes calldata priceUpdate) public payable {
  uint256 verification_fee = pythLazer.verification_fee();
  (bytes calldata payload, ) = verifyUpdate{ value: verification_fee }(update);
  //...
}

```

The `verifyUpdate` function will verify the price update and return the payload and the verification fee. This call takes a fee which can be queried from [`verification_fee(){:solidity}`](https://github.com/pyth-network/pyth-crosschain/blob/main/lazer/contracts/evm/src/PythLazer.sol#L9) function and passed to the `verifyUpdate` call. This fee is used to cover the cost of verifying the price update.

This SDK provides [`parsePayloadHeader`](https://github.com/pyth-network/pyth-crosschain/blob/main/lazer/contracts/evm/src/PythLazerLib.sol#L21) method to retrieve the values from the payload header.

```solidity copy
(uint64 _timestamp, Channel channel, uint8 feedsLen, uint16 pos) = parsePayloadHeader(payload);
```

This method returns:

- `_timestamp`: The timestamp of the price update.
- `channel`: The channel of the price update.
- `feedsLen`: The number of feeds in the price update.
- `pos`: The cursor position of the payload.

One can iterate over all the feeds and properties present within the price update, modifying the state variables as necessary.

Here is an example of how to iterate over the feeds and properties:

```solidity copy
for (uint8 i = 0; i < feedsLen; i++) {
    uint32 feedId;
    uint8 num_properties;
    (feedId, num_properties, pos) = parseFeedHeader(payload, pos);
    for (uint8 j = 0; j < num_properties; j++) {
        PriceFeedProperty property;
        (property, pos) = parseFeedProperty(payload, pos);
        if (property == PriceFeedProperty.Price) {
            uint64 _price;
            (_price, pos) = parseFeedValueUint64(payload, pos);
            if (feedId == 2 && _timestamp > timestamp) {
                price = _price;
                timestamp = _timestamp;
            }
        } else if (property == PriceFeedProperty.BestBidPrice) {
            uint64 _price;
            (_price, pos) = parseFeedValueUint64(payload, pos);
        } else if (property == PriceFeedProperty.BestAskPrice) {
            uint64 _price;
            (_price, pos) = parseFeedValueUint64(payload, pos);
        } else {
            revert("unknown property");
        }
    }
}
```
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
