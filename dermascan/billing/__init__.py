"""
Author: Dermascan Team
Date: 2026-08-20
License: MIT

Billing: credit accounts, the append-only ledger, and Paystack top-ups.

Postgres is authoritative for money. Redis holds the live spendable balance
for the deduction hot path; see ``redis_wallet`` for that contract.
"""
