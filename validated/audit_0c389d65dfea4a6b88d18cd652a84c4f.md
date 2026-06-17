### Title
Pyth Lazer `verifyUpdate()` Accepts Arbitrarily Old Signed Price Updates Without Payload Timestamp Staleness Check — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

The Pyth Lazer on-chain verification contracts (`PythLazer.sol` on EVM, `pyth_lazer.move` on Sui and Aptos) verify the ECDSA/Ed25519 signature and that the signer key has not expired, but **never check whether the timestamp embedded in the signed payload is recent relative to the current block time**. Any unprivileged caller can replay a validly signed but arbitrarily old Lazer price update, causing consumer protocols to act on stale prices that are out of sync with current market conditions.

---

### Finding Description

The `verifyUpdate()` function in `PythLazer.sol` performs three checks:

1. Fee payment
2. EVM format magic number
3. ECDSA signature recovery + `isValidSigner()` (only checks `block.timestamp < trustedSignerToExpiresAtMapping[signer]`) [1](#0-0) 

The function returns the raw `payload` bytes to the caller without any check that the timestamp encoded inside the payload is recent. The payload header contains a `uint64 timestamp` (in microseconds) that is parsed by `PythLazerLib.parsePayloadHeader()`: [2](#0-1) 

This timestamp is never compared against `block.timestamp` inside `verifyUpdate()`. The `isValidSigner()` check only guards against a *signer key* that has expired — it says nothing about the *age of the price data* in the payload: [3](#0-2) 

The same pattern exists on Sui — `parse_and_verify_le_ecdsa_update_v2()` checks signer expiry (`clock.timestamp_ms() < expires_at_ms`) but never compares the parsed `Update.timestamp` against the clock: [4](#0-3) [5](#0-4) 

And on Aptos — `verify_message()` checks `signer_info.expires_at > timestamp::now_seconds()` but performs no check on the message's own timestamp: [6](#0-5) 

By contrast, the Pyth Pulse Scheduler — which wraps the standard Pyth price feed — explicitly enforces `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours` against the price update's publish time: [7](#0-6) [8](#0-7) 

No equivalent protection exists in the Lazer verification contracts.

---

### Impact Explanation

A consumer protocol that calls `pythLazer.verifyUpdate()` and then uses the returned payload's price for a financial operation (swap, liquidation, collateral valuation) can be fed a price from arbitrarily far in the past. Because `verifyUpdate()` is the security boundary — it is the only on-chain check — consumers that rely on it for correctness receive no staleness guarantee from the protocol layer.

Concrete adverse effects:
- **Favorable price replay for trades**: An attacker saves a Lazer update from a time when an asset price was favorable (e.g., BTC at $100k). When the price drops to $90k, the attacker submits the old update to a consumer DEX or perp protocol, opening a position at the stale $100k price.
- **Liquidation avoidance / triggering**: An attacker replays an old price to prevent their own liquidation or to trigger a competitor's liquidation at a price that no longer reflects market reality.
- **Same-block exploitation**: Because `verifyUpdate()` is stateless (no replay tracking), the attacker can open and close a position in the same block using a stale price, extracting risk-free profit.

---

### Likelihood Explanation

- The attack requires no privileged access. Any EOA can call `verifyUpdate()` with a historical update blob.
- Lazer update blobs are broadcast over WebSocket to all subscribers. Any subscriber can archive them.
- The signer key expiry (`expiresAt`) is set far in the future (e.g., `3000000000000000` in tests), so old updates remain verifiable for years.
- The attack is deterministic and requires no racing or MEV infrastructure beyond standard transaction submission. [9](#0-8) 

---

### Recommendation

Add a maximum payload age check inside `verifyUpdate()` (EVM), `parse_and_verify_le_ecdsa_update_v2()` (Sui), and `verify_message()` (Aptos). After parsing the payload timestamp, assert it is within an acceptable window of the current block time, for example:

```solidity
// In PythLazer.sol verifyUpdate(), after extracting payload:
(uint64 payloadTimestampUs, , , ) = PythLazerLib.parsePayloadHeader(payload);
uint64 payloadTimestampSec = payloadTimestampUs / 1_000_000;
require(
    block.timestamp <= payloadTimestampSec + MAX_PAYLOAD_AGE_SECONDS,
    "price update too old"
);
require(
    payloadTimestampSec <= block.timestamp + MAX_FUTURE_SECONDS,
    "price update from future"
);
```

The appropriate `MAX_PAYLOAD_AGE_SECONDS` should be determined based on the Lazer channel's update rate (e.g., 200 ms for `fixed_rate@200ms`) and the acceptable latency for the target use case — likely in the range of 5–60 seconds for most DeFi applications.

---

### Proof of Concept

1. At time `T`, subscribe to Pyth Lazer and record a signed EVM-format update blob `U_old` for BTC/USD at price `P_old = $100,000`.
2. Wait until time `T + Δ` where the current BTC price is `P_new = $90,000` and `Δ` is large enough to represent a meaningful price discrepancy.
3. Call a consumer contract's `updatePrice(U_old)` function. Internally it calls:
   ```solidity
   (bytes calldata payload, ) = pythLazer.verifyUpdate{value: fee}(U_old);
   ```
4. `verifyUpdate()` succeeds: the signer is still trusted (`block.timestamp < expiresAt`), the signature is valid.
5. The consumer parses `payload` and stores `price = $100,000`.
6. The attacker opens a short position against the consumer at the stale $100,000 price, immediately profiting $10,000 per BTC as the protocol settles at the stale price.

The `verifyUpdate()` call at step 4 succeeds unconditionally for any historically valid update, as confirmed by the test: [10](#0-9)

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

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L43-64)
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
}
```

**File:** lazer/contracts/sui/sources/update_v2.move (L42-57)
```text
public(package) fun parse_from_cursor(mut cursor: bcs::BCS): Update {
    let timestamp = cursor.peel_u64();

    let channel_value = cursor.peel_u8();
    let channel = channel_v2::from_u8(channel_value);

    let feed_count = cursor.peel_u8();
    let mut feeds = vector::empty<Feed>();
    let mut feed_i = 0;

    while (feed_i < feed_count) {
        let feed = feed::parse_from_cursor(&mut cursor);
        vector::push_back(&mut feeds, feed);
        feed_i = feed_i + 1;
    };

```

**File:** lazer/contracts/aptos/sources/pyth_lazer.move (L106-141)
```text
    public fun verify_message(
        message: vector<u8>,
        signature: vector<u8>,
        trusted_signer: vector<u8>,
        fee: coin::Coin<AptosCoin>
    ) acquires Storage {
        let storage = borrow_global<Storage>(@pyth_lazer);

        // Verify fee amount
        assert!(coin::value(&fee) >= storage.single_update_fee, EINSUFFICIENT_FEE);

        // Transfer fee to treasury
        coin::deposit(storage.treasury, fee);

        // Verify signer is trusted and not expired
        let i = 0;
        let valid = false;
        while (i < storage.trusted_signers.length()) {
            let signer_info = vector::borrow(&storage.trusted_signers, (i as u64));
            if (&signer_info.pubkey == &trusted_signer
                && signer_info.expires_at > timestamp::now_seconds()) {
                valid = true;
                break
            };
            i = i + 1;
        };
        assert!(valid, EINVALID_SIGNER);

        // Verify signature
        let sig = ed25519::new_signature_from_bytes(signature);
        let pk = ed25519::new_unvalidated_public_key_from_bytes(trusted_signer);
        assert!(
            ed25519::signature_verify_strict(&sig, &pk, message),
            EINVALID_SIGNATURE
        );
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L14-22)
```text
    /// Maximum time in the past (relative to current block timestamp)
    /// for which a price update timestamp is considered valid
    /// when validating the update conditions.
    /// @dev Note: We don't use this when parsing update data from the Pyth contract
    /// because don't want to reject update data if it contains a price from a market
    /// that closed a few days ago, since it will contain a timestamp from the last
    /// trading period. We enforce this value ourselves against the maximum
    /// timestamp in the provided update data.
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L373-386)
```text
        // Calculate the minimum acceptable timestamp (clamped at 0)
        // The maximum acceptable timestamp is enforced by the parsePriceFeedUpdatesWithSlots call
        uint256 minAllowedTimestamp = (block.timestamp >
            PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            : 0;

        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
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
