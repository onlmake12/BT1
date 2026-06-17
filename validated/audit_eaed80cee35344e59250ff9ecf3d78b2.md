### Title
Lazer Price Update Replay Attack — No Consumed-Update Tracking in `verifyUpdate` - (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.sol`'s `verifyUpdate` function performs ECDSA signature verification against a trusted-signer list but maintains **no record of which update payloads have already been processed**. Any attacker who observes a valid signed Lazer update over the wire can replay it an unlimited number of times. The contract will accept each replay as a fresh, valid update.

---

### Finding Description

The `verifyUpdate` function in `PythLazer.sol` is the sole on-chain verification primitive for Pyth Lazer price updates on EVM chains. Its entire verification logic is:

1. Check `msg.value >= verification_fee`
2. Validate the EVM format magic bytes
3. Recover the ECDSA signer from `keccak256(payload)`
4. Assert the recovered signer is in the trusted-signer list and not expired [1](#0-0) 

There is no:
- Consumed-update set (no hash tracking)
- Nonce field validated on-chain
- Sequence number monotonicity check
- Timestamp freshness enforcement at the contract level

The payload does contain a `timestamp` field (parsed by `PythLazerLib.parsePayloadHeader`), but `verifyUpdate` never reads or validates it. [2](#0-1) 

The same signed `update` bytes can be submitted to `verifyUpdate` in block N, block N+1000, or any future block, and the function will return `(payload, signer)` successfully each time, charging the verification fee each time.

This is confirmed by the test suite, which calls `verifyUpdate` twice with the **same `update` bytes** and expects both calls to succeed: [3](#0-2) 

No test exists that asserts a second call with the same update reverts.

---

### Impact Explanation

Consumer contracts that call `verifyUpdate` and then use the returned `payload` to update on-chain state (e.g., a DeFi lending protocol's price oracle) can be fed arbitrarily stale price data. An attacker who captured a valid Lazer update at time T (when, say, ETH was at $3,000) can replay it hours later (when ETH is at $2,500) to any consumer contract. If the consumer contract does not implement its own strict timestamp staleness check, it will accept the replayed price as current.

Concrete consequences:
- **Price manipulation**: Replay a favorable old price to trigger incorrect liquidations, borrow more collateral than allowed, or execute trades at stale rates.
- **Fee drain**: Repeatedly replay old updates to drain ETH from consumer contracts that auto-pay the `verification_fee` on each call.

The Lazer system is explicitly designed for latency-sensitive, high-value DeFi use cases (the "pro" tier), making stale-price acceptance particularly damaging.

---

### Likelihood Explanation

- Lazer updates are broadcast publicly over WebSocket to all subscribers. Any observer can capture a valid signed update.
- No privileged access, key material, or special tooling is required — only the ability to call `verifyUpdate` with a previously observed `bytes` payload.
- The attack is fully permissionless and requires only paying the `verification_fee` (currently `1 wei`).
- Consumer contracts that follow the documented integration pattern (checking `_timestamp > storedTimestamp`) are partially protected, but the contract itself provides zero enforcement, and any consumer that omits or implements the check incorrectly is fully exposed.

---

### Recommendation

Add a consumed-update registry to `PythLazer.sol`. The simplest approach is to store a `mapping(bytes32 => bool) public consumedUpdates` and, inside `verifyUpdate`, compute `bytes32 updateHash = keccak256(update)`, assert `!consumedUpdates[updateHash]`, then set `consumedUpdates[updateHash] = true`. Alternatively, enforce a minimum `timestamp` monotonicity check: store `uint64 public lastAcceptedTimestamp` and revert if the payload's timestamp is not strictly greater than the stored value. The same fix should be applied to the analogous Aptos (`verify_message` in `pyth_lazer.move`) and Sui (`verify_le_ecdsa_message` in `pyth_lazer.move`) contracts, which have the identical absence of replay tracking. [4](#0-3) [5](#0-4) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import {PythLazer} from "lazer/contracts/evm/src/PythLazer.sol";
import {PythLazerLib} from "lazer/contracts/evm/src/PythLazerLib.sol";

contract ReplayPoC {
    PythLazer public pythLazer;

    constructor(address _pythLazer) {
        pythLazer = PythLazer(_pythLazer);
    }

    /// @notice Demonstrates that the same signed update is accepted N times.
    /// @param update  A valid signed Lazer update captured from the WebSocket feed.
    /// @param times   Number of times to replay it.
    function replayUpdate(bytes calldata update, uint256 times) external payable {
        uint256 fee = pythLazer.verification_fee();
        for (uint256 i = 0; i < times; i++) {
            // Each call succeeds; the contract never rejects a previously seen update.
            (bytes memory payload, address signer) =
                pythLazer.verifyUpdate{value: fee}(update);
            // payload contains the OLD timestamp on every iteration.
            // A consumer that trusts this payload without checking the timestamp
            // will accept stale prices on every replay.
        }
    }
}
```

**Steps:**
1. Subscribe to the Pyth Lazer WebSocket feed and capture one `evm.data` hex blob (a valid signed update).
2. Deploy `ReplayPoC` pointing at the live `PythLazer` proxy.
3. Call `replayUpdate(capturedUpdate, 100)` with `100 * verification_fee` ETH.
4. All 100 calls succeed. Each returns the same stale payload with the original timestamp.
5. Any consumer contract that calls `verifyUpdate` with this replayed blob will receive the stale price as "verified." [1](#0-0)

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

**File:** lazer/contracts/evm/test/PythLazer.t.sol (L60-68)
```text
        // Alice provides appropriate fee
        vm.prank(alice);
        pythLazer.verifyUpdate{value: fee}(update);
        assertEq(alice.balance, 1 ether - fee);

        // Alice overpays and is refunded
        vm.prank(alice);
        pythLazer.verifyUpdate{value: 0.5 ether}(update);
        assertEq(alice.balance, 1 ether - fee - fee);
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
