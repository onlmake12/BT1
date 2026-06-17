### Title
Accumulated `verification_fee` ETH Permanently Locked in `PythLazer` Contract — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

The `PythLazer` contract collects a `verification_fee` on every call to `verifyUpdate`, but provides no function to withdraw the accumulated ETH. All fees paid by Lazer consumers are permanently locked in the contract.

---

### Finding Description

`PythLazer.verifyUpdate` is a `payable` function that requires `msg.value >= verification_fee` and refunds any excess to the caller, retaining exactly `verification_fee` wei per call:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  lines 70-106
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    // Require fee and refund excess
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);
    }
    // ... signature verification ...
}
``` [1](#0-0) 

The contract's complete function set is: `initialize`, `updateTrustedSigner`, `isValidSigner`, `verifyUpdate`, `version`, and `_authorizeUpgrade`. None of these withdraw ETH from the contract. The inherited `OwnableUpgradeable` and `UUPSUpgradeable` bases also provide no ETH withdrawal path. [2](#0-1) 

`verification_fee` is initialized to `1 wei` and there is no setter function, so the fee is fixed at deployment time with no on-chain mechanism to change it or recover it. [3](#0-2) 

By contrast, every other Pyth fee-collecting contract provides a withdrawal path: `Entropy.sol` has `withdraw` / `withdrawAsFeeManager`, `Echo.sol` has `withdrawFees` / `withdrawAsFeeManager`, `PythGovernance` has a `WithdrawFee` governance action, and the Stylus receiver has `withdraw_fee`.

---

### Impact Explanation

Every call to `verifyUpdate` by any Lazer consumer permanently locks `verification_fee` wei in the `PythLazer` contract. The Pyth protocol has no mechanism to recover these funds. As Lazer adoption grows and the number of `verifyUpdate` calls increases, the total locked ETH grows proportionally. The owner cannot set a higher fee to compensate for the loss, nor can they recover already-locked funds.

---

### Likelihood Explanation

This is triggered by normal, unprivileged usage. Any Lazer consumer (relayer, on-chain integrator) calling `verifyUpdate` with a valid signed update is sufficient. No special conditions, privileged access, or adversarial behavior is required. The accumulation is continuous and automatic.

---

### Recommendation

Add an `onlyOwner` withdrawal function to `PythLazer`:

```solidity
function withdrawFees(address payable recipient, uint256 amount) external onlyOwner {
    require(address(this).balance >= amount, "Insufficient balance");
    recipient.transfer(amount);
}
```

Additionally, consider adding a `setVerificationFee` function so the owner can adjust the fee without a contract upgrade.

---

### Proof of Concept

1. Deploy `PythLazer` (or use the live proxy). `verification_fee` = 1 wei.
2. Any caller invokes `verifyUpdate{value: 1}(validUpdate)`. The contract retains 1 wei.
3. After N calls, the contract holds N wei.
4. Attempt to call any function to withdraw — none exist. The ETH is permanently locked.
5. Even the `owner` calling `updateTrustedSigner` or `_authorizeUpgrade` cannot move ETH out. [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L1-111)
```text
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.13;

import "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";
import "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

contract PythLazer is OwnableUpgradeable, UUPSUpgradeable {
    TrustedSignerInfo[100] internal trustedSigners;
    uint256 public verification_fee;
    mapping(address => uint256) trustedSignerToExpiresAtMapping;

    constructor() {
        _disableInitializers();
    }

    struct TrustedSignerInfo {
        address pubkey;
        uint256 expiresAt;
    }

    function initialize(address _topAuthority) public initializer {
        __Ownable_init(_topAuthority);
        __UUPSUpgradeable_init();

        verification_fee = 1 wei;
    }

    function _authorizeUpgrade(address) internal override onlyOwner {}

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

    function version() public pure returns (string memory) {
        return "0.1.1";
    }
}
```
