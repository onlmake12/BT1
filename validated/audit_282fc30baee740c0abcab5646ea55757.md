### Title
Protocol `verification_fee` Permanently Stuck in `PythLazer` Contract — (`File: lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate()` is a `payable` function that collects a `verification_fee` on every call. The fee accumulates in the contract's ETH balance, but no withdrawal function exists to retrieve it. Over time, all collected protocol fees are permanently locked in the contract.

---

### Finding Description

In `PythLazer.sol`, the `verifyUpdate` function requires callers to pay at least `verification_fee` wei. Excess above the fee is refunded to the caller, but the exact `verification_fee` amount is retained in the contract:

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);
    }
    // ... signature verification ...
}
``` [1](#0-0) 

The entire contract (lines 1–111) exposes only four functions: `updateTrustedSigner`, `isValidSigner`, `verifyUpdate`, and `version`. There is no `withdrawFees`, `withdraw`, or any ETH-recovery function. The `verification_fee` collected on every `verifyUpdate` call has no retrieval path. [2](#0-1) 

Compare this to the Entropy contract, which correctly tracks `accruedPythFeesInWei` and provides `withdrawFee()` in `EntropyGovernance`:

```solidity
function withdrawFee(address targetAddress, uint128 amount) external {
    _authoriseAdminAction();
    _state.accruedPythFeesInWei -= amount;
    (bool success, ) = targetAddress.call{value: amount}("");
    ...
}
``` [3](#0-2) 

`PythLazer` has no equivalent mechanism.

---

### Impact Explanation

Every call to `verifyUpdate` with a non-zero `verification_fee` permanently locks that fee in the contract. There is no function to transfer the accumulated ETH to the protocol treasury or any other address. Protocol revenue from Lazer verification fees is irrecoverably lost.

---

### Likelihood Explanation

`verifyUpdate` is the core function called by every Lazer updater/relayer on every price update submission. With a non-zero `verification_fee`, each call contributes to the stuck balance. This is a continuous, automatic accumulation triggered by normal protocol operation — no special attacker action is required.

---

### Recommendation

Add a fee withdrawal function restricted to the contract owner, analogous to `EntropyGovernance.withdrawFee`:

```solidity
function withdrawFees(address payable recipient, uint256 amount) external onlyOwner {
    require(address(this).balance >= amount, "Insufficient balance");
    recipient.transfer(amount);
}
```

Alternatively, track `accruedFeesInWei` in storage and emit events on withdrawal for auditability.

---

### Proof of Concept

1. Owner sets `verification_fee = 1000 wei` (non-zero).
2. Lazer relayer calls `verifyUpdate{value: 1000}(updateData)` — 1000 wei enters the contract, none leaves.
3. After N calls, `address(PythLazer).balance == N * 1000 wei`.
4. No function exists to transfer this ETH out. Funds are permanently locked. [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L31-111)
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

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L103-116)
```text
    function withdrawFee(address targetAddress, uint128 amount) external {
        require(targetAddress != address(0), "targetAddress is zero address");
        _authoriseAdminAction();

        if (amount > _state.accruedPythFeesInWei)
            revert EntropyErrors.InsufficientFee();

        _state.accruedPythFeesInWei -= amount;

        (bool success, ) = targetAddress.call{value: amount}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(targetAddress, amount);
    }
```
