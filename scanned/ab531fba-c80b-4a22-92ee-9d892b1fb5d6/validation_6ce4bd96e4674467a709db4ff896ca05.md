Looking at the external report's vulnerability class — **accounting mismatch where a debit (shares burned) does not match the credit (assets actually transferred)** — I need to find an analog in CKB where a capacity debit and credit diverge due to a split-source calculation error.

Let me examine the DAO withdrawal path and the `DaoCalculator` more carefully.