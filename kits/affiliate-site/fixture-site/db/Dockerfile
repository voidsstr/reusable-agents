# The gate fixture's Postgres: the stock image plus the init script that adds
# the violations database. Copied in and made world-readable here (not
# bind-mounted), so the files' host permissions never decide whether Postgres
# can read them.
FROM postgres:16
COPY init/ /docker-entrypoint-initdb.d/
RUN chmod -R a+rX /docker-entrypoint-initdb.d
