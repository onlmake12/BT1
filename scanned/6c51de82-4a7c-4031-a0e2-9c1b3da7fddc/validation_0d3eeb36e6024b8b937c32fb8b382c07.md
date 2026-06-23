Looking at the vulnerability class from the external report — **reward/capacity accounting error where a computed value is not properly bounded, causing subsequent subtraction to underflow and produce wrong results** — I need to find an analog in the CKB codebase.

Let me examine the DAO withdrawal calculation path closely.