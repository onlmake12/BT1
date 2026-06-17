### Title
Replay of Signed Lazer Price Updates Without Per-Message Tracking Enables Stale Price Injection — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.sol::verifyUpdate` performs ECDSA signature verification and signer-expiry checks but maintains **no per-message replay protection**. The same signed Lazer update bytes can be submitted an unlimited number of times. Any integrating DeFi contract that relies on `verifyUpdate` for freshness guarantees — without independently enforcing timestamp monotonicity — can be fed arbitrarily stale prices by an unprivileged attacker who simply replays a previously valid update.

---

### Finding Description

`verifyUpdate` in `PythLazer.sol` accepts a signed price update, verifies the ECDSA signature against the trusted-signer registry, and returns the raw payload and recovered signer address. The function does not:

- Record the `keccak256` hash of any processed update in a consumed-set.
- Enforce that the timestamp embedded in the payload is strictly greater than the last accepted timestamp.
- Assign or validate any sequence number or nonce.

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 70-106
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    ...
    payload = update[71:71 + payload_len];
    bytes32 hash = keccak256(payload);
    (signer, , ) = ECDSA.tryRecover(
        hash,
        uint8(update[68]) + 27,
        bytes32(update[4:36]),
        bytes32(update[36:68])
    );
    if (signer == address(0)) { revert("invalid signature"); }
    if (!isValidSigner(signer))  { revert("invalid signer"); }
    // ← no consumed-hash check, no timestamp enforcement
}
```

The only guard is `isValidSigner`, which checks `block.timestamp < trustedSignerToExpiresAtMapping[signer]`. A signer that was valid at the time the original update was produced remains valid until its expiry, so any update signed by that signer can be replayed for the entire lifetime of the signer key.

The same design gap exists in the Aptos and Sui Lazer contracts (`lazer/contracts/aptos` and `lazer/contracts/sui/sources/pyth_lazer.move`), neither of which tracks consumed message hashes or enforces timestamp ordering.

---

### Impact Explanation

An attacker who captures a legitimately signed Lazer update at time T (when asset price = P_high) can replay it after the market price has fallen to P_low. Any on-chain DeFi protocol (lending, perpetuals, options) that calls `verifyUpdate` and uses the returned payload without independently enforcing timestamp freshness will observe P_high instead of P_low. This enables:

- **Over-borrowing**: attacker deposits collateral, replays stale high-price update, borrows more than the collateral is worth.
- **Liquidation avoidance**: attacker replays a stale high-price update to prevent their own under-collateralised position from being liquidated.
- **Arbitrage against the protocol**: attacker sells the asset at market (P_low) while the protocol still prices it at P_high.

The financial loss is bounded only by the liquidity of the affected protocol and the magnitude of the price discrepancy between the replayed update and the true market price.

---

### Likelihood Explanation

- **No privileged access required.** Any transaction sender can call `verifyUpdate`.
- **Trivially executable.** The attacker only needs to record the raw `update` bytes from a prior on-chain or off-chain call and resubmit them.
- **Signer keys are long-lived.** The trusted-signer expiry is set by governance and is typically far in the future, giving a large replay window.
- **No economic barrier.** The `verification_fee` is a small flat fee; replaying a single update to drain a lending pool is highly profitable relative to the fee cost.

---

### Recommendation

Add a consumed-message set to `PythLazer.sol` (and the equivalent Aptos/Sui contracts) that records the `keccak256` of every accepted payload and reverts on re-submission:

```solidity
mapping(bytes32 => bool) private _consumedUpdates;

function verifyUpdate(...) external payable returns (...) {
    ...
    bytes32 msgHash = keccak256(payload);
    require(!_consumedUpdates[msgHash], "update already consumed");
    _consumedUpdates[msgHash] = true;
    ...
}
```

Alternatively (or additionally), enforce that the timestamp embedded in the payload is strictly greater than the last accepted timestamp for each price feed, mirroring the monotonicity guarantee that Pyth Core enforces for VAA-based price updates.

---

### Proof of Concept

1. At block N, a legitimate caller submits a valid Lazer update signed by `trustedSigner` with payload timestamp T and price P_high. The call succeeds; the integrating lending protocol records collateral value = P_high.

2. At block N+k, the market price drops to P_low. No new Lazer update has been pushed on-chain yet (or the attacker front-runs the push).

3. The attacker calls `verifyUpdate` with the **identical bytes** from step 1:
   - `verification_fee` is paid — passes.
   - ECDSA signature is valid — passes.
   - `isValidSigner(trustedSigner)` — passes (signer not yet expired).
   - No consumed-hash check exists — **no revert**.
   - Returns `payload` containing P_high.

4. The attacker passes the returned payload to the lending protocol's `updatePrice` entry point. The protocol now believes collateral = P_high.

5. The attacker borrows `P_high * LTV` worth of assets against collateral worth only `P_low`, extracting `(P_high - P_low) * LTV` in profit. [1](#0-0) [2](#0-1) [3](#0-2)

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
