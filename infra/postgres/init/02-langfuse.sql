-- SPDX-License-Identifier: AGPL-3.0-only
-- Copyright (c) 2026 Proofly Technology
--
-- Separate role and database for self-hosted LangFuse (compose profile `observability`).
-- Created on every first init, even when the profile is off, so turning the profile on
-- later needs no manual step. LangFuse runs its own Prisma migrations as `langfuse`,
-- which owns only this database and cannot connect to the application database.

\set ON_ERROR_STOP on

\getenv lf_pw LANGFUSE_DB_PASSWORD

\if :{?lf_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'LANGFUSE_DB_PASSWORD is not set'; END $$;
\endif

SELECT length(:'lf_pw') < 16 AS lf_pw_weak \gset
\if :lf_pw_weak
  DO $$ BEGIN RAISE EXCEPTION 'LANGFUSE_DB_PASSWORD is shorter than 16 characters'; END $$;
\endif

CREATE ROLE langfuse
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
  PASSWORD :'lf_pw';

CREATE DATABASE langfuse OWNER langfuse;
REVOKE ALL ON DATABASE langfuse FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE langfuse TO langfuse;
