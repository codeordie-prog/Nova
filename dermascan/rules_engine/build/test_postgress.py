"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly.



Test local postgress

"""

import os
import psycopg2


# Check connection

conn = psycopg2.connect(
    host="localhost",
    port="5432",
    database="postgres",
    user="postgres",
    password="Kelvinjoe692@"
)

cur = conn.cursor()
cur.execute("SELECT version();")
print(cur.fetchone())

cur.close()
conn.close()
