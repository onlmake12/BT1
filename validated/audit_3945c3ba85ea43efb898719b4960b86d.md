### Title
User Can Manipulate Randomness via Selective Reveal in the Non-Callback `request` Flow — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The deprecated-but-still-callable `request` function in Pyth Entropy allows a user to compute the final random number off-chain before deciding whether to call `reveal`. Because `reveal` is gated to `msg.sender == req.requester`, only the original requester can finalize the request. A user who dislikes the computed outcome can simply abandon the request and re-request with a fresh `userContribution`, paying only the per-request fee each time. This is a direct analog to the gold-card "recommit after 255 blocks" vulnerability: the user gains unlimited retries at the cost of one fee per attempt.

---

### Finding Description

**Root cause — `reveal` is exclusively caller-controlled:**

```solidity
// Entropy.sol line 513-515
if (req.requester != msg.sender) {
    revert EntropyErrors.Unauthorized();
}
``` [1](#0-0) 

Only the original requester can call `reveal`. There is no timeout, no penalty, and no third-party that can force finalization. The request simply remains open indefinitely if the user chooses not to reveal.

**The random number is fully computable before `reveal` is called:**

The final random number is:

```solidity
// Entropy.sol lines 950-958
combinedRandomness = keccak256(
    abi.encodePacked(userRandomness, providerRandomness, blockHash)
);
``` [2](#0-1) 

All three inputs are known to the user before they call `reveal`:
- `userRandomness` — the user chose this value themselves.
- `providerRandomness` (`x_i`) — deterministic from the provider's hash chain; publicly retrievable from the Fortuna REST API at `/v1/chains/{chain}/revelations/{sequenceNumber}` after the reveal delay.
- `blockHash` — either `0` (when `useBlockHash = false`) or `blockhash(req.blockNumber)`, which is observable on-chain once the request block is mined.

**The code itself acknowledges the two-chance scenario for `useBlockHash = true`:**

```solidity
// Entropy.sol lines 414-421
// The `blockhash` function will return zero if the req.blockNumber is equal to the current
// block number, or if it is not within the 256 most recent blocks. This allows the user to
// select between two random numbers by executing the reveal function in the same block as the
// request, or after 256 blocks. This gives each user two chances to get a favorable result on
// each request.
// Revert this transaction for when the blockHash is 0;
if (_blockHash == bytes32(uint256(0)))
    revert EntropyErrors.BlockhashUnavailable();
``` [3](#0-2) 

The `BlockhashUnavailable` revert closes the two-chance window for `useBlockHash = true`, but it does **not** close the core issue: the user can still compute the outcome before revealing and choose to abandon the request entirely.

**Why `requestV2` / `revealWithCallback` is NOT affected:**

The newer callback path explicitly sets `useBlockhash = false` and, critically, `revealWithCallback` is callable by **anyone** — the provider (Fortuna) calls it automatically, so the user cannot withhold the reveal.

```solidity
// Entropy.sol lines 366-370
// If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
// If we remove the blockHash from this, the provider would have no choice but to provide its committed
// random number. Hence, useBlockHash is set to false.
false,
true,
``` [4](#0-3) 

```solidity
// IEntropy.sol — "Anyone can call this method to fulfill a request"
function revealWithCallback(...) external;
``` [5](#0-4) 

The vulnerability is isolated to the `request` + `reveal` (non-callback) path.

---

### Impact Explanation

Any on-chain application that uses `request(provider, userCommitment, useBlockHash)` followed by `reveal(...)` is vulnerable to outcome manipulation. A user can:

1. Make a request (pay fee).
2. Query the Fortuna API for the provider's contribution `x_i`.
3. Compute `keccak256(userContribution, x_i, blockHash)` off-chain.
4. If the result is unfavorable (e.g., they lose a lottery, get a bad NFT trait, lose a game), abandon the request and repeat from step 1.
5. Reveal only when the computed outcome is favorable.

The attacker effectively has unlimited retries at the cost of one fee per attempt. For any application where the expected gain from a favorable outcome exceeds the per-request fee, this attack is economically rational. This breaks the core security guarantee of the Entropy protocol for the non-callback flow.

---

### Likelihood Explanation

- The `request` function is deprecated but remains `public` and callable on all deployed Entropy contracts.
- The Fortuna provider API is publicly documented and accessible.
- The user controls their own `userContribution`, so no privileged access is needed.
- The attack requires only: paying the fee, querying a public REST endpoint, and choosing not to submit a transaction — all trivially achievable by any unprivileged user.
- For high-value randomness consumers (NFT mints, on-chain games, lotteries) the fee cost per retry is negligible compared to the gain.

---

### Recommendation

1. **Disable `request` and `reveal`** entirely by adding a revert, forcing all callers to migrate to `requestV2` / `revealWithCallback`. The callback path is already immune because `revealWithCallback` is permissionless and the provider finalizes the request automatically.
2. If the non-callback path must remain, introduce a **commit-then-lock** mechanism: after the provider's contribution becomes available, start a short finalization window during which *anyone* (not just the requester) can call `reveal`, after which the request is auto-cancelled and the fee is not refunded.
3. Document clearly in the interface that `request` + `reveal` provides **no randomness fairness guarantee** and should not be used for any application where the user has an incentive to manipulate the outcome.

---

### Proof of Concept

```solidity
// Attacker script (pseudocode)
IEntropy entropy = IEntropy(ENTROPY_ADDRESS);
address provider  = entropy.getDefaultProvider();
uint128 fee       = entropy.getFee(provider);

while (true) {
    // 1. Choose a fresh user secret
    bytes32 userSecret     = keccak256(abi.encodePacked(block.timestamp, nonce++));
    bytes32 userCommitment = entropy.constructUserCommitment(userSecret);

    // 2. Commit (pay fee)
    uint64 seq = entropy.request{value: fee}(provider, userCommitment, false);

    // 3. Fetch provider contribution from Fortuna REST API off-chain
    //    GET https://fortuna.dourolabs.app/v1/chains/<chain>/revelations/<seq>
    bytes32 providerContrib = fetchFromFortuna(seq);

    // 4. Compute outcome off-chain
    bytes32 result = entropy.combineRandomValues(userSecret, providerContrib, bytes32(0));

    // 5. Only reveal if outcome is favorable (e.g., result % 100 < 10 for a 10% win)
    if (uint256(result) % 100 < 10) {
        entropy.reveal(provider, seq, userSecret, providerContrib);
        break; // Got a favorable result
    }
    // Otherwise: abandon request, loop back, pay another fee
}
```

The attacker pays one fee per iteration. The expected number of iterations to achieve a 1-in-N outcome is N, costing N × fee. For any application where the prize value exceeds N × fee, the attack is profitable.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L366-372)
```text
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
            gasLimit
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L414-421)
```text
            // The `blockhash` function will return zero if the req.blockNumber is equal to the current
            // block number, or if it is not within the 256 most recent blocks. This allows the user to
            // select between two random numbers by executing the reveal function in the same block as the
            // request, or after 256 blocks. This gives each user two chances to get a favorable result on
            // each request.
            // Revert this transaction for when the blockHash is 0;
            if (_blockHash == bytes32(uint256(0)))
                revert EntropyErrors.BlockhashUnavailable();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L493-515)
```text
    //
    // This function must be called by the same `msg.sender` that originally requested the random number. This check
    // prevents denial-of-service attacks where another actor front-runs the requester's reveal transaction.
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L950-958)
```text
    function combineRandomValues(
        bytes32 userRandomness,
        bytes32 providerRandomness,
        bytes32 blockHash
    ) public pure override returns (bytes32 combinedRandomness) {
        combinedRandomness = keccak256(
            abi.encodePacked(userRandomness, providerRandomness, blockHash)
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L87-102)
```text
    // Fulfill a request for a random number. This method validates the provided userRandomness
    // and provider's revelation against the corresponding commitment in the in-flight request. If both values are validated
    // and the requestor address is a contract address, this function calls the requester's entropyCallback method with the
    // sequence number, provider address and the random number as arguments. Else if the requestor is an EOA, it won't call it.
    //
    // Note that this function can only be called once per in-flight request. Calling this function deletes the stored
    // request information (so that the contract doesn't use a linear amount of storage in the number of requests).
    // If you need to use the returned random number more than once, you are responsible for storing it.
    //
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userRandomNumber,
        bytes32 providerRevelation
    ) external;
```
