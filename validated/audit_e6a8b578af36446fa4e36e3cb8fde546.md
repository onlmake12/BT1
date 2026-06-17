### Title
Pending `split_request.amount` Not Deducted in `withdraw_stake` Balance Check, Allowing Source Owner to Drain Tokens Before `accept_split` Executes - (File: `governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth staking program's `withdraw_stake` instruction does not account for a pending `split_request.amount` when computing the withdrawable balance. After a user calls `request_split`, the tokens remain in the source `stake_account_custody` but are not locked. The source account owner can immediately call `withdraw_stake` for the same amount, draining the custody before the `pda_authority` calls `accept_split`. When `accept_split` is then executed, the custody is empty and the transfer to the recipient fails.

---

### Finding Description

The staking program exposes a two-step token-split flow for transferring unvested tokens:

1. **`request_split(amount, recipient)`** — called by the account owner. Records a `SplitRequest { amount, recipient }` in the `stake_account_split_request` PDA. Tokens remain in `stake_account_custody`.
2. **`accept_split(amount, recipient)`** — called by the privileged `pda_authority`. Transfers `amount` tokens from the source custody to a new stake account owned by `recipient`.

The `withdraw_stake` instruction's account list is:

```
owner, destination, stake_account_positions,
stake_account_metadata, stake_account_custody,
custody_authority, config
```

`stake_account_split_request` is **not** included. [1](#0-0) 

Therefore, the `InsufficientWithdrawableBalance` guard inside `withdraw_stake` computes available balance as:

```
custody.amount − sum(staked_positions)
```

It never subtracts `split_request.amount`. [2](#0-1) 

The `request_split` instruction itself only validates that `amount ≤ custody.amount` and that there are no active staking positions (`SplitWithStake` guard). [3](#0-2) 

After `request_split` succeeds, the same owner can call `withdraw_stake` for the same `amount` because `withdraw_stake` never reads `stake_account_split_request`. The custody is drained, and the subsequent `accept_split` call by `pda_authority` will fail (insufficient custody balance) or transfer zero tokens to the recipient.

---

### Impact Explanation

- The split mechanism — designed to transfer unvested tokens to a recipient — can be silently broken by the source account owner at any time between `request_split` and `accept_split`.
- Recipients expecting unvested token allocations receive nothing.
- The `pda_authority` (governance) cannot enforce the split because the source owner can always front-run `accept_split` with a `withdraw_stake`.
- This is a staking accounting invariant failure: the "pending transfer" amount is not protected by any on-chain lock, making the split guarantee illusory.

---

### Likelihood Explanation

Any staking account owner who has submitted a `request_split` can trigger this. The entry path is fully unprivileged (the owner signs `withdraw_stake`). No special access is required. The window exists from the moment `request_split` is confirmed until `accept_split` is executed, which can span multiple blocks or epochs.

---

### Recommendation

In `withdraw_stake`, load the `stake_account_split_request` account and subtract `split_request.amount` from the withdrawable balance before the `InsufficientWithdrawableBalance` check:

```
withdrawable = custody.amount
             − sum(staked_positions)
             − split_request.amount   // add this
```

Alternatively, lock the `split_request.amount` in a separate escrow sub-account at `request_split` time, releasing it only on `accept_split` or an explicit `cancel_split`.

---

### Proof of Concept

1. Alice has 1000 PYTH in `stake_account_custody`, no active staking positions.
2. Alice calls `request_split(amount=1000, recipient=Bob)`. `SplitRequest { amount: 1000, recipient: Bob }` is stored on-chain. Custody still holds 1000 PYTH.
3. Alice immediately calls `withdraw_stake(amount=1000)`. `withdraw_stake` reads only `stake_account_custody` (1000) and `stake_account_positions` (empty) — no split request check. Withdrawal succeeds. Custody is now 0.
4. `pda_authority` calls `accept_split(amount=1000, recipient=Bob)`. The instruction attempts to transfer 1000 PYTH from the now-empty custody. The transfer fails with insufficient balance. Bob receives nothing. [4](#0-3) [5](#0-4)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L61-103)
```json
      "msg": "Position not in use",
      "name": "PositionNotInUse"
    },
    {
      "code": 6006,
      "msg": "New position needs to have positive balance",
      "name": "CreatePositionWithZero"
    },
    {
      "code": 6007,
      "msg": "Closing a position of 0 is not allowed",
      "name": "ClosePositionWithZero"
    },
    {
      "code": 6008,
      "msg": "Invalid product/publisher pair",
      "name": "InvalidPosition"
    },
    {
      "code": 6009,
      "msg": "Amount to unlock bigger than position",
      "name": "AmountBiggerThanPosition"
    },
    {
      "code": 6010,
      "msg": "Position already unlocking",
      "name": "AlreadyUnlocking"
    },
    {
      "code": 6011,
      "msg": "Epoch duration is 0",
      "name": "ZeroEpochDuration"
    },
    {
      "code": 6012,
      "msg": "Owner needs to own destination account",
      "name": "WithdrawToUnauthorizedAccount"
    },
    {
      "code": 6013,
      "msg": "Insufficient balance to cover the withdrawal",
      "name": "InsufficientWithdrawableBalance"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L191-203)
```json
      "msg": "Can't split 0 tokens from an account",
      "name": "SplitZeroTokens"
    },
    {
      "code": 6032,
      "msg": "Can't split more tokens than are in the account",
      "name": "SplitTooManyTokens"
    },
    {
      "code": 6033,
      "msg": "Can't split a token account with staking positions. Unstake your tokens first.",
      "name": "SplitWithStake"
    },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L419-423)
```json
      "discriminator": [177, 172, 17, 93, 193, 86, 54, 222],
      "docs": [
        "* A split request can only be accepted by the `pda_authority` from\n     * the config account. If accepted, `amount` tokens are transferred to a new stake account\n     * owned by the `recipient` and the split request is reset (by setting `amount` to 0).\n     * The recipient of a transfer can't vote during the epoch of the transfer.\n     *\n     * The `pda_authority` must explicitly approve both the amount of tokens and recipient, and\n     * these parameters must match the request (in the `split_request` account)."
      ],
      "name": "accept_split"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1171-1237)
```json
        {
          "name": "stake_account_metadata",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 116, 97, 107, 101, 95, 109, 101, 116, 97, 100, 97, 116,
                  97
                ]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          }
        },
        {
          "name": "stake_account_split_request",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [
                  115, 112, 108, 105, 116, 95, 114, 101, 113, 117, 101, 115, 116
                ]
              },
              {
                "kind": "account",
                "path": "stake_account_positions"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "address": "11111111111111111111111111111111",
          "name": "system_program"
        }
      ],
      "args": [
        {
          "name": "amount",
          "type": "u64"
        },
        {
          "name": "recipient",
          "type": "pubkey"
        }
      ],
      "discriminator": [133, 146, 228, 165, 251, 207, 146, 23],
      "docs": [
        "* Any user of the staking program can request to split their account and\n     * give a part of it to another user.\n     * This is mostly useful to transfer unvested tokens. Each user can only have one active\n     * request at a time.\n     * In the first step, the user requests a split by specifying the `amount` of tokens\n     * they want to give to the other user and the `recipient`'s pubkey."
      ],
      "name": "request_split"
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L1072-1075)
```typescript
      name: "requestSplit";
      docs: [
        "* Any user of the staking program can request to split their account and\n     * give a part of it to another user.\n     * This is mostly useful to transfer unvested tokens. Each user can only have one active\n     * request at a time.\n     * In the first step, the user requests a split by specifying the `amount` of tokens\n     * they want to give to the other user and the `recipient`'s pubkey.",
      ];
```
