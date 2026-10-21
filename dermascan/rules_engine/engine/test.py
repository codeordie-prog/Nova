"""Agents: Read instructions.md at the repository root before any edits or changes; follow it strictly."""

import json

from dermascan.rules_engine.engine.rules_engine import internet_search


from dotenv import load_dotenv
import os

load_dotenv()

postgres_host = os.environ.get("POSTGRES_HOST", "localhost")
postgres_port = os.environ.get("POSTGRES_PORT", "5432")
postgres_database = os.environ.get("POSTGRES_DATABASE", "qutis_db")
postgres_user = os.environ.get("POSTGRES_USER", "postgres")
postgres_password = os.environ.get("POSTGRESS_PASSWORD", "")
postgres_table = os.environ.get("POSTGRES_TABLE", "cir_pdfs")


list_variables = [postgres_host, postgres_port, postgres_database, postgres_user, postgres_password, postgres_table]

for variable in list_variables:
    print(variable)
    print("-"*100)