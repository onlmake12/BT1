### Title
Lack of Replay Protection in `verifyUpdate` Allows Stale Price Injection — (`File: lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate` performs ECDSA signature verification over a Lazer price payload but never records used update hashes. Any previously broadcast, validly-signed update can be re-submitted an unlimited number of times. Consumer contracts that do not independently enforce timestamp monotonicity will accept stale prices as fresh.

### Finding Description
`verifyUpdate` in `PythLazer.sol` verifies the ECDSA signature and checks that the signer has not expired, but it performs no replay tracking:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 70-106
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    ...
    bytes32 hash = keccak256(payload);          // hash computed …
    (signer, , ) = ECDSA.tryRecover(            // … only for sig recovery
        hash,
        uint8(update[68]) + 27,
        bytes32(update[4:36]),
        bytes32(update[36:68])
    );
    if (signer == address(0)) revert("invalid signature");
    if (!isValidSigner(signer)) revert("invalid signer");
    // ← hash is never stored; no used-update set; no sequence check
}
``` [1](#0-0) 

The payload contains a `timestamp` field (parsed by `PythLazerLib.parsePayloadHeader`), but `verifyUpdate` never reads or enforces it. [2](#0-1) 

The contract stores no `usedUpdates` mapping, no monotonic sequence counter, and no per-signer nonce. Once a Lazer signer produces a valid signature over a payload, that exact `update` blob remains replayable for the entire lifetime of the signer key (until `expiresAt`). [3](#0-2) 

The Sui implementation (`pyth_lazer.move`) has the same structure — `verify_le_ecdsa_message` checks only signer trust and expiry, with no replay guard. [4](#0-3) 

### Impact Explanation
An attacker who captures any past Lazer price update (all updates are broadcast over a public WebSocket) can replay it against `verifyUpdate` at any future time while the signer key is still valid. Consumer contracts that rely on `verifyUpdate`'s return value without independently enforcing timestamp monotonicity will accept the stale price as authentic. In DeFi contexts (lending, perpetuals, options) this allows the attacker to trade against an artificially stale price, causing direct financial loss to counterparties or the protocol.

**Impact: Medium** — financial loss to consumers of the Lazer price feed.

### Likelihood Explanation
Lazer updates are publicly streamed. Capturing a past update requires no special access. Replaying it costs only the `verification_fee` (currently 1 wei). The documentation example shows consumers *should* check `_timestamp > timestamp`, but this is advisory, not enforced by the contract. Many integrators will omit the check or implement it incorrectly.

**Likelihood: Medium** — trivially easy to execute; depends on consumer implementation quality.

### Recommendation
Add a `usedUpdates` mapping keyed on the payload hash and revert on re-use:

```solidity
mapping(bytes32 => bool) private usedUpdates;

function verifyUpdate(...) external payable returns (...) {
    ...
    bytes32 hash = keccak256(payload);
    require(!usedUpdates[hash], "update already used");
    usedUpdates[hash] = true;
    ...
}
```

Alternatively, enforce that the payload timestamp is strictly greater than the last accepted timestamp per signer, making the contract itself the source of freshness truth rather than delegating it to every consumer.

### Proof of Concept
1. Subscribe to the Lazer WebSocket and capture any valid `update` blob (e.g., BTC at $50,000).
2. Wait for the price to move significantly (e.g., to $60,000) and for a newer update to be applied to a target consumer contract.
3. Call `PythLazer.verifyUpdate{value: 1 wei}(oldUpdate)` — the call succeeds and returns the stale $50,000 payload with a valid signer address.
4. Pass the returned payload to any consumer contract that calls `verifyUpdate` and updates its stored price without checking the embedded timestamp against its last-seen timestamp.
5. The consumer now holds a stale price; exploit the discrepancy (e.g., borrow against an inflated collateral value or liquidate a position that should not be liquidatable).

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
