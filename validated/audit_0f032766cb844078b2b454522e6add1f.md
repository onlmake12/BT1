### Title
Entropy `request(useBlockHash=true)` Permanently Locks User Funds After 256 Blocks — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The public `request()` function in `Entropy.sol` accepts a `useBlockHash` flag. When set to `true`, the subsequent `reveal()` call depends on `blockhash(req.blockNumber)`, which the EVM only makes available for the 256 most recent blocks (~51 minutes at 12 s/block). If the provider fails to call `reveal()` within that window, the request is permanently unresolvable and the user's paid fee is irrecoverably locked — no refund or cancellation path exists.

---

### Finding Description

In `requestHelper`, the block number at request time is stored and the provider fee is immediately credited:

```solidity
req.blockNumber = SafeCast.toUint64(block.number);   // line 262
req.useBlockhash = useBlockhash;                      // line 263
...
providerInfo.accruedFeesInWei += providerFee;         // line 237
``` [1](#0-0) [2](#0-1) 

Later, in `revealHelper`, the contract calls `blockhash(req.blockNumber)` and **hard-reverts** if the result is zero:

```solidity
bytes32 _blockHash = blockhash(req.blockNumber);   // line 412
if (_blockHash == bytes32(uint256(0)))
    revert EntropyErrors.BlockhashUnavailable();   // line 421
``` [3](#0-2) 

`blockhash()` returns `bytes32(0)` for any block older than 256 blocks. Once block `N+257` is mined, `blockhash(N)` is permanently zero, so every future call to `reveal()` for that request reverts unconditionally.

The `reveal()` function has no fallback, no cancellation branch, and no refund path:

```solidity
function reveal(...) public override returns (bytes32 randomNumber) {
    ...
    (randomNumber, blockHash) = revealHelper(req, ...);  // always reverts after 256 blocks
    ...
    clearRequest(provider, sequenceNumber);
}
``` [4](#0-3) 

The `EntropyErrors.BlockhashUnavailable` error is defined but no recovery path is wired to it: [5](#0-4) 

---

### Impact Explanation

- **User funds permanently locked.** The fee (provider fee + Pyth protocol fee) is credited to the provider and the Pyth treasury at request time. After 256 blocks, the user can never call `reveal()` successfully, and there is no `refund()` or `cancel()` function anywhere in the contract. The user's ETH is gone.
- **Sequence number consumed.** The provider's `sequenceNumber` was incremented at request time, so the slot is also permanently wasted.
- **Scope:** Direct loss of user funds in a production EVM smart contract.

---

### Likelihood Explanation

- The `request(provider, userCommitment, true)` function is public and callable by any unprivileged user.
- The 256-block window is ~51 minutes on Ethereum mainnet (12 s/block) and far shorter on L2s (e.g., ~4 minutes on Arbitrum at ~0.25 s/block, ~8 minutes on Optimism at ~2 s/block).
- Any provider downtime, network congestion, or deliberate delay beyond this window triggers the permanent lock.
- A malicious provider can intentionally delay past 256 blocks on every request to collect fees without ever fulfilling service.
- The `requestWithCallback` / `requestV2` paths already hardcode `useBlockhash = false` precisely because of collusion risk, but the legacy `request()` path remains exposed. [6](#0-5) 

---

### Recommendation

1. **Add a `refund()` / `cancel()` function** that allows the original requester to reclaim their fee after the 256-block window has passed (i.e., when `block.number > req.blockNumber + 256` and `req.useBlockhash == true`).
2. **Alternatively**, deprecate the `useBlockHash=true` path in `request()` entirely, consistent with the decision already made for `requestWithCallback` and `requestV2`.
3. If the `useBlockHash` path is kept, document the hard 256-block deadline prominently and enforce it at request time (e.g., reject requests that cannot be fulfilled in time, or warn integrators).

---

### Proof of Concept

```solidity
// 1. User requests randomness with useBlockHash = true
uint64 seq = entropy.request{value: fee}(provider, userCommitment, true);
// req.blockNumber = block.number (e.g., 1000)
// providerInfo.accruedFeesInWei += providerFee  ← fee already gone

// 2. Provider is offline / delayed for > 256 blocks
vm.roll(1000 + 257);  // block 1257 — blockhash(1000) == 0

// 3. Any attempt to reveal now permanently reverts
entropy.reveal(provider, seq, userRandomness, providerRandomness);
// → reverts: EntropyErrors.BlockhashUnavailable()

// 4. No refund function exists — user's ETH is permanently locked
// entropy.refund(...)  ← does not exist
```

This matches the test already present in the repo confirming the revert at `block N+257`: [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L237-237)
```text
        providerInfo.accruedFeesInWei += providerFee;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L262-263)
```text
        req.blockNumber = SafeCast.toUint64(block.number);
        req.useBlockhash = useBlockhash;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L366-370)
```text
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L411-421)
```text
        if (req.useBlockhash) {
            bytes32 _blockHash = blockhash(req.blockNumber);

            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L496-530)
```text
    function reveal(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override returns (bytes32 randomNumber) {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            req.callbackStatus != EntropyStatusConstants.CALLBACK_NOT_NECESSARY
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

        if (req.requester != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }
        bytes32 blockHash;
        (randomNumber, blockHash) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
        emit Revealed(
            EntropyStructConverter.toV1Request(req),
            userContribution,
            providerContribution,
            blockHash,
            randomNumber
        );
        clearRequest(provider, sequenceNumber);
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyErrors.sol (L34-36)
```text
    // The blockhash is 0.
    // Signature: 0x92555c0e
    error BlockhashUnavailable();
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L480-511)
```text
    function testCheckOnBlockNumberWhenBlockHashUsed() public {
        vm.roll(1234);
        uint64 sequenceNumber = request(user2, provider1, 42, true);

        vm.roll(1234);
        assertRevealReverts(
            user2,
            provider1,
            sequenceNumber,
            42,
            provider1Proofs[sequenceNumber]
        );

        vm.roll(1234 + 257);
        assertRevealReverts(
            user2,
            provider1,
            sequenceNumber,
            42,
            provider1Proofs[sequenceNumber]
        );

        vm.roll(1235);
        assertRevealSucceeds(
            user2,
            provider1,
            sequenceNumber,
            42,
            provider1Proofs[sequenceNumber],
            blockhash(1234)
        );
    }
```
