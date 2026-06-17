### Title
Expired Signers Permanently Occupy Slots in Fixed-Size `trustedSigners[100]` Array, Blocking New Signer Registration — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.sol` stores trusted signers in a fixed-size array of 100 slots (`TrustedSignerInfo[100] internal trustedSigners`). When a signer's `expiresAt` timestamp passes naturally, the slot in the array is **not freed** — only an explicit `updateTrustedSigner(signer, 0)` call zeroes the slot. The slot-availability scan for new signers only looks for `pubkey == address(0)`. Expired signers with non-zero pubkeys are never treated as vacated. If all 100 slots are occupied by expired signers, the owner can never register a new (previously unseen) signer, permanently halting Lazer price-update verification.

---

### Finding Description

`PythLazer.sol` maintains two parallel data structures for signer state:

- `TrustedSignerInfo[100] internal trustedSigners` — a fixed-size array used for slot management.
- `mapping(address => uint256) trustedSignerToExpiresAtMapping` — used exclusively by `isValidSigner`. [1](#0-0) 

`isValidSigner` reads only the mapping: [2](#0-1) 

When adding a new signer, `updateTrustedSigner` first scans the array for an existing entry (to update), then scans for an empty slot (`pubkey == address(0)`): [3](#0-2) 

When a signer is **explicitly removed** (`expiresAt == 0`), both the array slot and the mapping entry are cleared: [4](#0-3) 

However, when a signer's `expiresAt` timestamp passes **naturally** (without an explicit removal call), the mapping correctly causes `isValidSigner` to return `false`, but the array slot remains occupied with a non-zero `pubkey`. The second scan (lines 54–61) never considers such a slot as available. The two data structures diverge: the mapping reflects expiry, the array does not.

This is the direct analog to the HSG `signerCount` fluctuation bug: in HSG, the counter could decrease (signers losing eligibility) and then increase again (signers regaining eligibility), bypassing `maxSigners`. Here, the "effective valid signer count" tracked by the mapping can decrease (expiry), but the "occupied slot count" tracked by the array never decreases automatically — the inverse of the same accounting inconsistency between two state variables that are supposed to represent the same quantity.

---

### Impact Explanation

If the owner has registered 100 distinct signer addresses over the contract's lifetime and all of them have since expired naturally, every slot in `trustedSigners` is occupied by a non-zero `pubkey`. Any attempt to register a new (previously unseen) signer will exhaust both loops and revert with `"no space for new signer"`. With no valid signer registerable, every call to `verifyUpdate` reverts with `"invalid signer"`, completely halting Lazer price-update verification on EVM chains. Downstream integrators relying on `verifyUpdate` for on-chain price attestation lose access to Lazer data with no self-service recovery path. [5](#0-4) 

---

### Likelihood Explanation

The 100-slot ceiling is generous for current operational scale (Pyth runs a small number of Lazer signers). However, the contract is upgradeable and intended for long-term use. Signer rotation — adding new keys, letting old ones expire — is a routine operational pattern. Over a multi-year horizon, 100 distinct signer addresses is reachable. No attacker action is required; the condition arises from normal governance-driven signer lifecycle management. The owner has no in-contract mechanism to reclaim expired slots in bulk; each must be individually removed via `updateTrustedSigner(addr, 0)`, which itself requires knowing every expired address.

---

### Recommendation

In the slot-availability scan, treat a slot as empty if its `pubkey` is `address(0)` **or** if its `expiresAt` is already in the past:

```solidity
// Signer not found - adding a new signer.
for (uint8 i = 0; i < trustedSigners.length; i++) {
    if (
        trustedSigners[i].pubkey == address(0) ||
        block.timestamp >= trustedSigners[i].expiresAt
    ) {
        // Evict stale mapping entry if overwriting an expired slot
        if (trustedSigners[i].pubkey != address(0)) {
            delete trustedSignerToExpiresAtMapping[trustedSigners[i].pubkey];
        }
        trustedSigners[i].pubkey = trustedSigner;
        trustedSigners[i].expiresAt = expiresAt;
        trustedSignerToExpiresAtMapping[trustedSigner] = expiresAt;
        return;
    }
}
revert("no space for new signer");
```

This keeps the two data structures consistent and mirrors the correct fix direction described in the referenced report: compare against the live state (expiry) rather than a stale occupancy marker.

---

### Proof of Concept

1. Deploy `PythLazer`. Owner calls `updateTrustedSigner(A_i, now + 1)` for 100 distinct addresses `A_1 … A_100`. All slots in `trustedSigners[0..99]` are now occupied with non-zero pubkeys.
2. One second passes. All 100 signers are expired: `isValidSigner(A_i)` returns `false` for every `i`.
3. Owner calls `updateTrustedSigner(A_101, now + 3600)` to register a fresh signer.
   - First loop (lines 46–51): `A_101` is not found in the array → no early return.
   - Second loop (lines 54–61): every slot has `pubkey != address(0)` → no empty slot found.
   - Transaction reverts: `"no space for new signer"`.
4. Any call to `verifyUpdate` with a payload signed by `A_101` reverts with `"invalid signer"` because `A_101` was never written to `trustedSignerToExpiresAtMapping`.
5. The only recovery is for the owner to call `updateTrustedSigner(A_i, 0)` for each of the 100 expired addresses individually before a new signer can be registered — a manual, address-by-address remediation with no on-chain enumeration helper. [6](#0-5)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L9-11)
```text
    TrustedSignerInfo[100] internal trustedSigners;
    uint256 public verification_fee;
    mapping(address => uint256) trustedSignerToExpiresAtMapping;
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L31-64)
```text
    function updateTrustedSigner(
        address trustedSigner,
        uint256 expiresAt
    ) external onlyOwner {
        if (expiresAt == 0) {
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == trustedSigner) {
                    trustedSigners[i].pubkey = address(0);
                    trustedSigners[i].expiresAt = 0;
                    delete trustedSignerToExpiresAtMapping[trustedSigner];
                    return;
                }
            }
            revert("no such pubkey");
        } else {
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == trustedSigner) {
                    trustedSigners[i].expiresAt = expiresAt;
                    trustedSignerToExpiresAtMapping[trustedSigner] = expiresAt;
                    return;
                }
            }
            // Signer not found - adding a new signer.
            for (uint8 i = 0; i < trustedSigners.length; i++) {
                if (trustedSigners[i].pubkey == address(0)) {
                    trustedSigners[i].pubkey = trustedSigner;
                    trustedSigners[i].expiresAt = expiresAt;
                    trustedSignerToExpiresAtMapping[trustedSigner] = expiresAt;
                    return;
                }
            }
            revert("no space for new signer");
        }
    }
```

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
