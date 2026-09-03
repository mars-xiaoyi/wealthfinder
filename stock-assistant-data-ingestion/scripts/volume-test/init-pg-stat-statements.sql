-- Mounted into postgres's /docker-entrypoint-initdb.d/ so it runs once, on
-- first container init, after shared_preload_libraries=pg_stat_statements
-- (set via the postgres `command:` in docker-compose.yml) takes effect.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
