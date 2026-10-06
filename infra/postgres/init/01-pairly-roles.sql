-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (c) 2026 Proofly Technology
--
-- Runs once, when the Postgres volume is first initialised, as the bootstrap superuser,
-- connected to the application database ($POSTGRES_DB). The official image runs it with
-- psql and ON_ERROR_STOP=1, so any error aborts the init and the container exits.
--
-- Roles:
--   pairly_owner  owns the database, the `public` schema and every Pairly table.
--                 Only Alembic (the `migrate` job) connects as this role.
--   pairly_app    what the API and worker connect as at runtime. Not an owner, not a
--                 superuser, no BYPASSRLS, so Row Level Security policies apply to it.
--                 It gets no table rights here: every migration grants exactly what the
--                 app needs, table by table.
--   pairly_auth   Better Auth (apps/web). Owns only the `auth` schema, so it can run its
--                 own DDL there; no rights on `public`.
--
-- Passwords come from the container environment through psql's \getenv. Nothing is
-- hardcoded; a missing or empty password aborts the init (fail closed).

\set ON_ERROR_STOP on

\getenv db_name POSTGRES_DB
\getenv owner_pw PAIRLY_OWNER_DB_PASSWORD
\getenv app_pw PAIRLY_APP_DB_PASSWORD
\getenv auth_pw PAIRLY_AUTH_DB_PASSWORD

\if :{?db_name}
\else
  DO $$ BEGIN RAISE EXCEPTION 'POSTGRES_DB is not set'; END $$;
\endif
\if :{?owner_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_OWNER_DB_PASSWORD is not set'; END $$;
\endif
\if :{?app_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_APP_DB_PASSWORD is not set'; END $$;
\endif
\if :{?auth_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_AUTH_DB_PASSWORD is not set'; END $$;
\endif

SELECT length(:'owner_pw') < 16 AS owner_pw_weak,
       length(:'app_pw') < 16 AS app_pw_weak,
       length(:'auth_pw') < 16 AS auth_pw_weak \gset
\if :owner_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_OWNER_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif
\if :app_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_APP_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif
\if :auth_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_AUTH_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif

-- ---------------------------------------------------------------- roles
CREATE ROLE pairly_owner
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'owner_pw';

CREATE ROLE pairly_app
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
  PASSWORD :'app_pw';

CREATE ROLE pairly_auth
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT
  PASSWORD :'auth_pw';

-- Runtime guard rails for the application role.
ALTER ROLE pairly_app SET statement_timeout = '30s';
ALTER ROLE pairly_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE pairly_app SET search_path = public;
ALTER ROLE pairly_owner SET search_path = public;
ALTER ROLE pairly_auth SET search_path = auth;

-- ---------------------------------------------------------------- database
ALTER DATABASE :"db_name" OWNER TO pairly_owner;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO pairly_app, pairly_auth;
-- Nobody but the superuser needs the maintenance database.
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;

-- ---------------------------------------------------------------- extensions
-- Created here by the superuser so migrations never need superuser rights.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- ---------------------------------------------------------------- schemas
ALTER SCHEMA public OWNER TO pairly_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pairly_app;

-- Better Auth tables live in their own schema, owned by pairly_auth so Better Auth can run
-- its own migrations there. pairly_app gets no access to it.
CREATE SCHEMA auth AUTHORIZATION pairly_auth;
REVOKE ALL ON SCHEMA auth FROM PUBLIC;

-- No ALTER DEFAULT PRIVILEGES on purpose (least privilege): each migration grants
-- pairly_app exactly the table rights it needs.
