-- SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
-- Copyright (c) 2026 Proofly Technology
--
-- Supabase only: the hosted deployment's equivalent of infra/postgres/init/01-pairly-roles.sql.
-- Local development, compose and CI keep the native Postgres 16 + pgvector stack and never
-- run this file. See docs/hosted-demo.md, "Database on Supabase".
--
-- Run once (it is idempotent, so running it again is safe and re-applies every attribute
-- and password) as the project's `postgres` role over the DIRECT connection or the
-- SESSION pooler (port 5432), never the transaction pooler (port 6543):
--
--   PAIRLY_OWNER_DB_PASSWORD=... PAIRLY_APP_DB_PASSWORD=... PAIRLY_AUTH_DB_PASSWORD=... \
--   psql "postgresql://postgres.<ref>:<postgres password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require" \
--     -v ON_ERROR_STOP=1 -f infra/supabase/bootstrap.sql
--
-- Optional: -v auth_schema=<name> (default pairly_auth; must match PAIRLY_AUTH_DB_SCHEMA).
--
-- Supabase's `postgres` role is not a superuser but has CREATEROLE, so it can create these
-- roles with the attributes below (all NO*, which needs no special right) and, on Postgres
-- 16 and later, administers the roles it creates (ALTER ROLE ... SET works).
--
-- What differs from the local init script, and why:
--   * Supabase owns database `postgres` and schema `public` (its own roles depend on
--     them), so their ownership is NOT moved to pairly_owner. pairly_owner gets CREATE on
--     `public` (Pairly's tables) and on the database (migration 0006 creates schema
--     lg_checkpoints). Tables are still owned by pairly_owner, which creates them.
--   * Better Auth's schema is `pairly_auth`, not `auth`: Supabase Auth already owns `auth`.
--   * Supabase's Data API roles (`anon`, `authenticated`, `service_role`) lose USAGE on
--     `public` and on the Better Auth schema, and Supabase's default privileges for objects
--     the `postgres` role creates in `public` are revoked, so no Pairly row is reachable
--     through PostgREST or GraphQL even if the Data API is switched back on. Turning the
--     Data API off in the dashboard is still step one of the checklist.
--   * The Data API roles are left alone everywhere else (Supabase's own schemas).

\set ON_ERROR_STOP on

\getenv owner_pw PAIRLY_OWNER_DB_PASSWORD
\getenv app_pw PAIRLY_APP_DB_PASSWORD
\getenv auth_pw PAIRLY_AUTH_DB_PASSWORD

\if :{?auth_schema}
\else
  \set auth_schema pairly_auth
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

-- ---------------------------------------------------------------- preconditions
SELECT length(:'owner_pw') < 16 AS owner_pw_weak,
       length(:'app_pw') < 16 AS app_pw_weak,
       length(:'auth_pw') < 16 AS auth_pw_weak,
       current_setting('server_version_num')::int < 160000 AS pg_too_old,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS is_super,
       :'auth_schema' !~ '^[a-z_][a-z0-9_]{0,62}$'
         OR :'auth_schema' IN ('auth', 'public', 'extensions', 'storage', 'realtime',
                               'graphql', 'graphql_public', 'vault', 'lg_checkpoints')
         OR :'auth_schema' LIKE 'pg\_%' AS bad_auth_schema,
       EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
               WHERE e.extname = 'vector' AND n.nspname <> 'public') AS vector_elsewhere,
       EXISTS (SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
               WHERE n.nspname = :'auth_schema' AND r.rolname <> 'pairly_auth') AS auth_schema_taken
\gset
\if :owner_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_OWNER_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif
\if :app_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_APP_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif
\if :auth_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'PAIRLY_AUTH_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif
\if :pg_too_old
  DO $$ BEGIN RAISE EXCEPTION 'Pairly needs Postgres 16 or later'; END $$;
\endif
\if :bad_auth_schema
  DO $$ BEGIN RAISE EXCEPTION 'auth_schema must be a lower-case identifier other than Supabase''s own schemas (auth is Supabase Auth''s)'; END $$;
\endif
\if :vector_elsewhere
  -- Pairly's migrations use the type unqualified with search_path = public.
  DO $$ BEGIN RAISE EXCEPTION 'extension vector is installed in another schema; Pairly needs it in public (ALTER EXTENSION vector SET SCHEMA public, if nothing else uses it)'; END $$;
\endif
\if :auth_schema_taken
  DO $$ BEGIN RAISE EXCEPTION 'the auth schema already exists and is not owned by pairly_auth'; END $$;
\endif

-- ---------------------------------------------------------------- roles
-- Same attributes as infra/postgres/init/01-pairly-roles.sql. A new role starts with
-- NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS (the defaults). A role that
-- already exists is not altered for those (a non-superuser may not even name SUPERUSER in
-- ALTER ROLE): the check below refuses to go on unless every one of them is off.
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pairly_owner') AS new_owner,
       NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pairly_app') AS new_app,
       NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pairly_auth') AS new_auth
\gset
\if :new_owner
  CREATE ROLE pairly_owner LOGIN INHERIT PASSWORD :'owner_pw';
\endif
\if :new_app
  CREATE ROLE pairly_app LOGIN NOINHERIT PASSWORD :'app_pw';
\endif
\if :new_auth
  CREATE ROLE pairly_auth LOGIN NOINHERIT PASSWORD :'auth_pw';
\endif

-- Every run: login, inheritance and the password (rotation is a re-run).
ALTER ROLE pairly_owner LOGIN INHERIT PASSWORD :'owner_pw';
ALTER ROLE pairly_app LOGIN NOINHERIT PASSWORD :'app_pw';
ALTER ROLE pairly_auth LOGIN NOINHERIT PASSWORD :'auth_pw';

SELECT count(*) <> 3 AS unsafe_roles
  FROM pg_roles
 WHERE rolname IN ('pairly_owner', 'pairly_app', 'pairly_auth')
   AND rolcanlogin
   AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
\gset
\if :unsafe_roles
  DO $$ BEGIN RAISE EXCEPTION 'a Pairly role has SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS (or cannot log in); fix it before going on'; END $$;
\endif

-- Runtime guard rails, as locally. The search paths are role settings, so they also hold
-- behind Supavisor (a startup option might not).
ALTER ROLE pairly_app SET statement_timeout = '30s';
ALTER ROLE pairly_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE pairly_app SET search_path = public;
ALTER ROLE pairly_owner SET search_path = public;
ALTER ROLE pairly_auth SET search_path = :"auth_schema";

-- None of the three may be a member of a Supabase role (or of each other).
SELECT count(*) > 0 AS has_memberships
  FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
 WHERE r.rolname IN ('pairly_owner', 'pairly_app', 'pairly_auth')
\gset
\if :has_memberships
  DO $$ BEGIN RAISE EXCEPTION 'a Pairly role is a member of another role; revoke that membership first'; END $$;
\endif

-- ---------------------------------------------------------------- database and schemas
GRANT CONNECT ON DATABASE :"DBNAME" TO pairly_owner, pairly_app, pairly_auth;
-- Migration 0006 creates schema lg_checkpoints as the owner.
GRANT CREATE ON DATABASE :"DBNAME" TO pairly_owner;

GRANT USAGE, CREATE ON SCHEMA public TO pairly_owner;
GRANT USAGE ON SCHEMA public TO pairly_app;
-- As locally, and as Supabase's own hardening guide advises: PUBLIC (every role,
-- including anon and authenticated) gets nothing on public by default.
-- https://supabase.com/docs/guides/troubleshooting/custom-role-inherits-privileges-that-were-not-explicitly-granted-ddaa1c
REVOKE ALL ON SCHEMA public FROM PUBLIC;
-- pairly_auth never touches public.
REVOKE ALL ON SCHEMA public FROM pairly_auth;

-- ---------------------------------------------------------------- extensions
-- In public, because Pairly's migrations name the type without a schema.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- ---------------------------------------------------------------- Better Auth schema
-- CREATE SCHEMA ... AUTHORIZATION, and revoking on a schema we do not own, need
-- membership of pairly_auth for a moment; it is taken back right after.
GRANT pairly_auth TO current_user;
CREATE SCHEMA IF NOT EXISTS :"auth_schema" AUTHORIZATION pairly_auth;
REVOKE ALL ON SCHEMA :"auth_schema" FROM PUBLIC;
REVOKE ALL ON SCHEMA :"auth_schema" FROM anon, authenticated, service_role;
REVOKE pairly_auth FROM current_user;

-- ---------------------------------------------------------------- Supabase Data API roles
-- Pairly never uses PostgREST or GraphQL. Without USAGE on the schema no table, view,
-- sequence or function in it can be reached by these roles, whatever their table grants.
REVOKE ALL ON SCHEMA public FROM anon, authenticated, service_role;
-- Supabase's default privileges for objects the postgres role creates in public
-- (https://supabase.com/docs/guides/database/hardening-data-api).
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON TABLES FROM anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  REVOKE ALL ON FUNCTIONS FROM anon, authenticated, service_role;

-- Second line of defence only (the first is the Data API switched off; the third is that
-- Pairly's tables are created by pairly_owner, which has no default grants to these
-- roles, and the migrations revoke table and SECURITY DEFINER function rights from
-- PUBLIC): warn
-- loudly if the schema revoke did not take.
SELECT has_schema_privilege('anon', 'public', 'USAGE')
       OR has_schema_privilege('authenticated', 'public', 'USAGE')
       OR has_schema_privilege('service_role', 'public', 'USAGE') AS data_api_can_see_public
\gset
\if :data_api_can_see_public
  DO $$ BEGIN RAISE WARNING 'anon, authenticated or service_role still has USAGE on schema public: make sure the Data API is disabled (Project Settings, Data API)'; END $$;
\endif

-- ---------------------------------------------------------------- report
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit
  FROM pg_roles WHERE rolname IN ('pairly_owner', 'pairly_app', 'pairly_auth') ORDER BY rolname;
SELECT has_schema_privilege('anon', 'public', 'USAGE') AS anon_public_usage,
       has_schema_privilege('authenticated', 'public', 'USAGE') AS authenticated_public_usage,
       has_schema_privilege('anon', :'auth_schema', 'USAGE') AS anon_auth_usage;
