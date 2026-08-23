# Log in via psql

`gclould auth list`
`gcloud config set account support@qurtis.tech`

`Get-ChildItem "C:\Program Files" -Recurse -Filter psql.exe -ErrorAction SilentlyContinue`

` & "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h 34.79.111.107 -p 5432 -U postgres -d qutis_db`

`\x on`

`SELECT * FROM (TABLE NAME) LIMIT N`