### Title
Publisher's Registered Self-Stake Account Can Also Delegate to Its Own Pool, Double-Counting Tokens in Pool Accounting - (File: `governance/pyth_staking_sdk/src/idl/integrity-pool.json`)

---

### Summary

The OIS integrity pool's `delegate` instruction contains no check that the delegating `stake_account_positions` is not the same account already registered as the publisher's self-stake account via `set_publisher_stake_account`. A malicious publisher can register their stake account as their self-stake account (counted in `selfDelState`) and simultaneously call `delegate` from the same account to their own publisher pool (counted in `delState`), causing the same tokens to be double-counted in the pool's total stake `S_p = S^p_p + S^d_p`.

---

### Finding Description

The integrity pool tracks two separate delegation states per publisher in `poolData`:

- `selfDelState[i]` — the publisher's own self-stake (set via `set_publisher_stake_account`)
- `delState[i]` — external delegators' stake (set via `delegate`)

The `delegate` instruction's account list includes `owner`, `pool_data`, `pool_config`, `publisher`, `stake_account_positions`, `stake_account_metadata`, `stake_account_custody`, `staking_program`, and `system_program`. [1](#0-0) 

Critically, the `delegate` instruction does **not** include `publisherStakeAccounts` from `poolData` and performs no check that `stake_account_positions != pool_data.publisher_stake_accounts[publisher_index]`. [2](#0-1) 

The `set_publisher_stake_account` instruction registers a stake account as the publisher's self-stake account, updating `selfDelState`. The `signer` must be the publisher's own keypair. [3](#0-2) 

Because the `delegate` instruction only requires `owner` to be the signer and owner of `stake_account_positions`, a publisher who controls both their publisher keypair and their stake account can:

1. Call `set_publisher_stake_account` → registers `SA` in `selfDelState[P]`
2. Call `delegate(amount)` targeting publisher `P` from the same `SA` → registers the same tokens in `delState[P]`

The pool's total stake for publisher P becomes `S_p = selfDelState[P].totalDelegation + delState[P].totalDelegation`, which now double-counts the publisher's actual token balance. [4](#0-3) 

---

### Impact Explanation

The `advance` instruction computes per-epoch rewards as `R_p = y * min(S_p, C_p)` where `S_p` is the sum of `selfDelState` and `delState` for publisher `p`. [5](#0-4) 

With double-counted tokens:

- **Inflated pool stake**: `S_p` is artificially inflated, potentially exceeding the cap `C_p` faster, drawing a larger share of the global reward budget.
- **Double reward claims**: When `advanceDelegationRecord` is called, the publisher collects rewards both as self-staker (via `selfDelState`) and as delegator (via `delState`) for the same underlying tokens, effectively claiming 2× rewards per token.
- **Dilution of other pools**: The global reward pool is finite. Inflated `S_p` for one publisher reduces the effective reward rate for all other publishers and delegators.
- **Slashing accounting corruption**: Slashing computes `SL_p = w * S_p`. With inflated `S_p`, the computed slash amount exceeds the actual tokens in custody, potentially causing slash instructions to fail or producing incorrect pro-rata distributions. [6](#0-5) 

---

### Likelihood Explanation

- **Entry path is permissionless at the transaction level**: Any holder of a publisher keypair can call both `set_publisher_stake_account` and `delegate` without any additional governance approval.
- **Publishers are not fully trusted**: The OIS protocol is explicitly designed to slash publishers for bad behavior, acknowledging that publishers may act adversarially.
- **No existing guard**: The `delegate` instruction's account constraints do not reference `publisherStakeAccounts` from `poolData`, so the runtime cannot enforce the invariant.
- **Currently y=0**: Rewards are currently disabled (`y=0`), so the immediate financial impact is zero. However, the accounting corruption is live and will become exploitable the moment `y` is set to a non-zero value via governance. [7](#0-6) 

---

### Recommendation

In the `delegate` instruction handler, add a constraint that rejects the transaction if `stake_account_positions == pool_data.publisher_stake_accounts[publisher_index]`. Concretely:

1. Pass `pool_data` into the `delegate` instruction (it is already present).
2. Look up the publisher's index in `pool_data.publishers`.
3. Assert `stake_account_positions.key() != pool_data.publisher_stake_accounts[publisher_index]`.

This mirrors the fix described in the referenced report: explicitly checking that the depositor address is not equal to the self-referential account at the entry point of the delegation instruction. [8](#0-7) 

---

### Proof of Concept

1. Publisher `P` controls keypair `K_P` and stake account `SA` (owned by `K_P`).
2. `P` calls `set_publisher_stake_account(signer=K_P, publisher=K_P, new_stake_account=SA)` → `pool_data.publisher_stake_accounts[i] = SA`, `selfDelState[i].totalDelegation += amount`.
3. `P` calls `delegate(owner=K_P, publisher=K_P, stake_account_positions=SA, amount=X)` → `delState[i].totalDelegation += X`.
4. `pool_data` now records `selfDelState[i].totalDelegation = X` and `delState[i].totalDelegation = X`, but `SA`'s custody holds only `X` tokens.
5. `S_p = 2X` despite only `X` real tokens backing the pool.
6. When governance sets `y > 0` and `advance` is called, `R_p = y * min(2X, C_p)` — up to 2× the legitimate reward.
7. `P` calls `advanceDelegationRecord` for `SA` against publisher `P`, collecting rewards for both the self-stake and delegator positions from the same `X` tokens. [9](#0-8) [10](#0-9)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L209-290)
```json
    {
      "accounts": [
        {
          "name": "payer",
          "signer": true,
          "writable": true
        },
        {
          "name": "stake_account_positions"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "pool_reward_custody",
          "pda": {
            "program": {
              "kind": "const",
              "value": [
                140, 151, 37, 143, 78, 36, 137, 241, 187, 61, 16, 41, 20, 142,
                13, 131, 11, 90, 19, 153, 218, 255, 16, 132, 4, 142, 123, 216,
                219, 233, 248, 89
              ]
            },
            "seeds": [
              {
                "kind": "account",
                "path": "pool_config"
              },
              {
                "kind": "const",
                "value": [
                  6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206,
                  235, 121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140,
                  245, 133, 126, 255, 0, 169
                ]
              },
              {
                "account": "PoolConfig",
                "kind": "account",
                "path": "pool_config.pyth_token_mint"
              }
            ]
          },
          "writable": true
        },
        {
          "name": "stake_account_custody",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [99, 117, 115, 116, 111, 100, 121]
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
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
```

**File:** governance/pyth_staking_sdk/src/idl/integrity-pool.json (L721-761)
```json
    {
      "accounts": [
        {
          "name": "signer",
          "signer": true
        },
        {
          "docs": [
            "CHECK : The publisher will be checked against data in the pool_data"
          ],
          "name": "publisher"
        },
        {
          "name": "pool_data",
          "relations": ["pool_config"],
          "writable": true
        },
        {
          "name": "pool_config",
          "pda": {
            "seeds": [
              {
                "kind": "const",
                "value": [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103]
              }
            ]
          }
        },
        {
          "name": "new_stake_account_positions_option",
          "optional": true
        },
        {
          "name": "current_stake_account_positions_option",
          "optional": true
        }
      ],
      "args": [],
      "discriminator": [99, 46, 72, 132, 100, 235, 211, 117],
      "name": "set_publisher_stake_account"
    },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L425-456)
```typescript
    },
    {
      name: "delegate";
      discriminator: [90, 147, 75, 178, 85, 88, 4, 137];
      accounts: [
        {
          name: "owner";
          writable: true;
          signer: true;
        },
        {
          name: "poolData";
          writable: true;
          relations: ["poolConfig"];
        },
        {
          name: "poolConfig";
          pda: {
            seeds: [
              {
                kind: "const";
                value: [112, 111, 111, 108, 95, 99, 111, 110, 102, 105, 103];
              },
            ];
          };
        },
        {
          name: "publisher";
          docs: [
            "CHECK : The publisher will be checked against data in the pool_data",
          ];
        },
```

**File:** governance/pyth_staking_sdk/src/types/integrity-pool.ts (L1643-1700)
```typescript
    },
    {
      name: "poolData";
      serialization: "bytemuck";
      repr: {
        kind: "c";
      };
      type: {
        kind: "struct";
        fields: [
          {
            name: "lastUpdatedEpoch";
            type: "u64";
          },
          {
            name: "claimableRewards";
            type: "u64";
          },
          {
            name: "publishers";
            type: {
              array: ["pubkey", 1024];
            };
          },
          {
            name: "delState";
            type: {
              array: [
                {
                  defined: {
                    name: "delegationState";
                  };
                },
                1024,
              ];
            };
          },
          {
            name: "selfDelState";
            type: {
              array: [
                {
                  defined: {
                    name: "delegationState";
                  };
                },
                1024,
              ];
            };
          },
          {
            name: "publisherStakeAccounts";
            type: {
              array: ["pubkey", 1024];
            };
          },
          {
            name: "events";
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L34-48)
```text
The reward $R_p$ distributed to each pool is calculated as follows:

$$
\large{\bold{R_p} = y \cdot \min(S_p, C_p)}
$$

Where:

- $y$ is the cap to the rate of rewards for any pool (currently $y = 0$)
- $S_p$ be the stake assigned to the publisher p pool, made of self-staked amount $S^{p}_{p}$ and delegated stake $S^{d}_{p}$, or $S_{p} = S^{p}_{p} + S^{d}_{p}$.
- $C_p$ be the stake cap for the pool assigned to publisher p.

<Callout type="info">
  Since $y = 0$, no rewards are currently distributed to any pool.
</Callout>
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L71-87)
```text
## Slashing

Slashing is an important aspect of the OIS protocol to ensure the integrity of the system.

The slashed amount for each pool is calculated as follows:

$$
\large{\bold{SL_p} = w \cdot S_p = w \cdot (S^{p}_{p} + S^{d}_{p})}
$$

Where:

- $SL_p$ is the slashed amount for the publisher $p$ pool.
- $w$ is the slashing rate.
- $S_p$ is the stake assigned to the publisher $p$ pool, made of self-staked amount $S^{p}_{p}$ and delegated stake $S^{d}_{p}$, or $S_{p} = S^{p}_{p} + S^{d}_{p}$.

Here $SL_p$ is uniformly allocated to both the self-staking publisher and delegators in the pool, pro-rata to their respective stake.
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L601-626)
```typescript
  public async stakeToPublisher(
    stakeAccountPositions: PublicKey,
    publisher: PublicKey,
    amount: bigint,
  ) {
    const instructions = [];

    if (!(await this.hasJoinedDaoLlc(stakeAccountPositions))) {
      instructions.push(
        await this.getJoinDaoLlcInstruction(stakeAccountPositions),
      );
    }

    instructions.push(
      await this.integrityPoolProgram.methods
        .delegate(convertBigIntToBN(amount))
        .accounts({
          owner: this.wallet.publicKey,
          publisher,
          stakeAccountPositions,
        })
        .instruction(),
    );

    return sendTransaction(instructions, this.connection, this.wallet);
  }
```

**File:** governance/pyth_staking_sdk/src/pyth-staking-client.ts (L827-844)
```typescript
  async setPublisherStakeAccount(
    publisher: PublicKey,
    stakeAccountPositions: PublicKey,
    newStakeAccountPositions: PublicKey | undefined,
  ) {
    const instruction = await this.integrityPoolProgram.methods
      .setPublisherStakeAccount()
      .accounts({
        currentStakeAccountPositionsOption: stakeAccountPositions,
        // eslint-disable-next-line unicorn/no-null
        newStakeAccountPositionsOption: newStakeAccountPositions ?? null,
        publisher,
      })
      .instruction();

    await sendTransaction([instruction], this.connection, this.wallet);
    return;
  }
```
