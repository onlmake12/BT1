### Title
Signature Replay in Pyth Lazer `verifyUpdate()` Enables Stale Price Injection Into Consumer Contracts — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.sol`'s `verifyUpdate()` function verifies an ECDSA signature over a price payload but performs no replay protection: it neither tracks consumed payload hashes nor enforces a nonce. Any unprivileged actor who observes a valid on-chain call can re-submit the identical `update` bytes indefinitely — until the trusted signer's certificate expires — and receive a successful verification result each time. Consumer contracts that do not independently enforce timestamp freshness will accept the replayed stale payload as authoritative.

---

### Finding Description

`verifyUpdate()` in `PythLazer.sol` recovers the signer from the ECDSA signature over `keccak256(payload)` and checks only that the recovered address is a currently-valid trusted signer:

```solidity
bytes32 hash = keccak256(payload);
(signer, , ) = ECDSA.tryRecover(hash, uint8(update[68]) + 27,
    bytes32(update[4:36]), bytes32(update[36:68]));
if (!isValidSigner(signer)) { revert("invalid signer"); }
```

There is no mapping of consumed payload hashes, no nonce field in the signed digest, and no maximum-age check on the `timestamp` embedded in the payload. The function is stateless with respect to previously verified updates.

The same design is present in the Sui contract's `verify_le_ecdsa_message()` / `parse_and_verify_le_ecdsa_update_v2()` and the Aptos contract's `verify_message()`: all three verify the signature and signer expiry, then return the payload without recording that it has been consumed.

The signed payload contains a `timestamp` field (parsed by `parsePayloadHeader` in `PythLazerLib.sol`), but the Pyth contract itself never reads or enforces it. Enforcement is explicitly delegated to consumers via documentation, but the contract provides no on-chain guarantee.

---

### Impact Explanation

An attacker who observes a legitimate `verifyUpdate(update)` call on-chain can replay the same `update` bytes to any consumer contract that calls `verifyUpdate()` and does not independently enforce `_timestamp > lastTimestamp`. The consumer will receive a successfully verified payload containing a stale price and may:

- Accept a manipulated (artificially low or high) price for a DeFi protocol's collateral valuation, enabling incorrect liquidations or over-borrowing.
- Accept a stale funding rate, causing incorrect perpetual settlement.

The attacker pays only the `verification_fee` (currently 1 wei) per replay. The signer's certificate may be valid for an extended period (e.g., `expiresAt` set far in the future), giving the attacker a large replay window.

---

### Likelihood Explanation

- **Entry path**: Any unprivileged EVM transaction sender. No privileged key or role is required.
- **Observation**: The `update` bytes are calldata, fully visible on-chain.
- **Cost**: `verification_fee` (1 wei by default) per replay.
- **Window**: The replay window equals the trusted signer's remaining validity period, which can be months or years.
- **Consumer exposure**: Consumer contracts that omit a `require(_timestamp > lastTimestamp)` guard — a common integration mistake — are directly exploitable. The Pyth documentation warns about this but does not enforce it on-chain.

---

### Recommendation

1. **Track consumed payload hashes**: Add a `mapping(bytes32 => bool) public usedPayloads` and revert if `usedPayloads[keccak256(payload)]` is already set. Mark it used before returning.
2. **Enforce maximum payload age**: Add a `maxAgeSeconds` parameter or constant and revert if `block.timestamp > payloadTimestamp + maxAgeSeconds`.
3. Apply the same fix to the Sui (`verify_le_ecdsa_message`) and Aptos (`verify_message`) contracts.

---

### Proof of Concept

1. A legitimate caller submits `pythLazer.verifyUpdate{value: fee}(update)` where `update` encodes a price of `P` at timestamp `T`. The consumer contract stores `price = P, lastTimestamp = T`.
2. Time advances; the real price moves to `P'`. A new signed update for `P'` at `T' > T` is published.
3. An attacker front-runs or ignores the new update and calls `pythLazer.verifyUpdate{value: fee}(update)` with the original bytes (price `P`, timestamp `T`). The call succeeds — the signer is still valid, and no replay check exists.
4. The attacker passes the returned stale payload to the consumer contract. If the consumer does not check `T > lastTimestamp`, it overwrites `price = P` (stale), enabling price manipulation.

**Relevant code locations:** [1](#0-0) 

The hash is computed only over `payload` with no chain ID, nonce, or contract address: [2](#0-1) 

The Sui analog — `verify_le_ecdsa_message` — checks only signer validity and expiry, with no consumed-digest tracking: [3](#0-2) 

The full Sui parse-and-verify entry point that calls it without replay protection: [4](#0-3) 

The Aptos analog — `verify_message` — has the same structure: [5](#0-4) 

The Cardano documentation explicitly acknowledges the absence of replay protection as a consumer responsibility, confirming this is a protocol-level gap rather than an oversight in a single chain's implementation: [6](#0-5)

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

**File:** lazer/contracts/sui/sources/pyth_lazer.move (L87-118)
```text
public fun parse_and_verify_le_ecdsa_update_v2(s: &State, clock: &Clock, update: vector<u8>): Update {
    let mut cursor = bcs::new(update);

    // Parse and validate message magic
    let magic = cursor.peel_u32();
    assert!(magic == UPDATE_MESSAGE_MAGIC, EInvalidUpdateMagic);

    // Parse signature
    let mut signature = vector::empty<u8>();
    let mut sig_i = 0;
    while (sig_i < SECP256K1_SIG_LEN) {
        signature.push_back(cursor.peel_u8());
        sig_i = sig_i + 1;
    };

    // Parse expected payload length and get remaining bytes as payload
    let payload_len = cursor.peel_u16();
    let payload = cursor.into_remainder_bytes();

    // Validate expected payload length
    assert!(payload_len as u64 == payload.length(), EInvalidPayloadLength);

    // Parse payload
    let mut payload_cursor = bcs::new(payload);
    let payload_magic = payload_cursor.peel_u32();
    assert!(payload_magic == PAYLOAD_MAGIC, EInvalidPayloadMagic);

    // Verify the signature against trusted signers
    verify_le_ecdsa_message(s, clock, &signature, &payload);

    update_v2::parse_from_cursor(payload_cursor)
}
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

**File:** apps/developer-hub/content/docs/price-feeds/pro/integrate-as-consumer/cardano.mdx (L19-21)
```text
<Callout type="warning">
  Purpose of the Pyth withdraw script is to verify signature validity of a provided price update payload. It does not enforce freshness of the update, nor does it disallow verifying the same update multiple times. If your contract puts constraints on a validity window of an update, make sure to enforce this directly in your contract implementation, e.g. by checking `timestamp_us` field.
</Callout>
```
