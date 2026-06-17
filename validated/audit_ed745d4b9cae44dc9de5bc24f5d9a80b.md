### Title
Permanently Locked Protocol Fees — No Withdrawal Function in `PythLazer` — (File: `lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

The `PythLazer` contract collects a `verification_fee` on every call to `verifyUpdate`, but the contract contains no function to withdraw the accumulated ETH. The fees are permanently locked in the contract with no recovery path.

---

### Finding Description

`PythLazer.verifyUpdate` is a `payable` function that requires `msg.value >= verification_fee` (initialized to `1 wei`) and refunds any excess to the caller. The net `verification_fee` amount is retained by the contract on every successful call.

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);
    }
    ...
}
``` [1](#0-0) 

The complete set of functions in `PythLazer` is: `initialize`, `_authorizeUpgrade`, `updateTrustedSigner`, `isValidSigner`, `verifyUpdate`, and `version`. There is no `withdrawFees`, `sweep`, or any other function that transfers the contract's ETH balance to the owner or any other address. [2](#0-1) 

The `verification_fee` state variable is set once at initialization and has no setter, so it cannot be zeroed out as a workaround. [3](#0-2) 

This is structurally identical to the reported `dumpFees` issue: in that case a fee-transfer function existed but had no reachable caller; here the fee-transfer function does not exist at all. Both result in the same outcome — collected protocol fees are permanently inaccessible.

---

### Impact Explanation

Every call to `verifyUpdate` by any Lazer consumer permanently locks `verification_fee` wei in the contract. The Pyth protocol owner cannot recover this ETH without deploying a new implementation via the UUPS upgrade mechanism. Until an upgrade is performed, all accrued fees are frozen. At scale (high-frequency Lazer usage), the locked amount grows unboundedly.

---

### Likelihood Explanation

`verifyUpdate` is the core entry point for all Lazer price-feed consumers on EVM chains. It is called by any unprivileged address that submits a Lazer update. No special role or condition is required to trigger the fee accumulation — every normal usage of the protocol contributes to the locked balance. Likelihood is **High**.

---

### Recommendation

Add an `onlyOwner`-gated fee withdrawal function to `PythLazer`:

```solidity
function withdrawFees(address payable recipient, uint256 amount) external onlyOwner {
    require(recipient != address(0), "zero address");
    (bool sent, ) = recipient.call{value: amount}("");
    require(sent, "transfer failed");
}
```

Alternatively, emit a `FeesAccrued` event in `verifyUpdate` and track `accruedFees` in storage to make the locked balance auditable before withdrawal.

---

### Proof of Concept

1. Deploy `PythLazer` (or interact with the live proxy).
2. Call `verifyUpdate{value: 1 wei}(validUpdate)` N times from any EOA.
3. Observe that `address(pythLazer).balance` grows by `1 wei` per call.
4. Attempt to call any withdrawal function — none exists; every call reverts with "function not found."
5. The owner has no on-chain path to recover the accumulated ETH without first upgrading the implementation contract. [4](#0-3)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L8-111)
```text
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
