-- Runs once, on first initialization of an empty data volume.
-- Creates the test database alongside the main one and installs pgvector in
-- both, so `make test` has somewhere to run instead of silently skipping.
--
-- If you already have a volume and this did not run, either
--   docker compose down -v && docker compose up -d
-- or create it by hand:
--   createdb -h localhost -U jobrunner jobrunner_test
-- and mark it, which the suite requires before it drops anything:
--   psql -h localhost -U jobrunner -d jobrunner_test \
--     -c "COMMENT ON DATABASE jobrunner_test IS 'jobrunner:test'"

CREATE DATABASE jobrunner_test;
-- tests/conftest.py drops and recreates the schema only of a database
-- carrying this mark. The suite never writes it.
COMMENT ON DATABASE jobrunner_test IS 'jobrunner:test';

\connect jobrunner
CREATE EXTENSION IF NOT EXISTS vector;

\connect jobrunner_test
CREATE EXTENSION IF NOT EXISTS vector;
