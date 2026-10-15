CREATE DATABASE ieol_test;
CREATE DATABASE ieol_demo;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ieol_app') THEN
    CREATE ROLE ieol_app LOGIN PASSWORD 'ieol-app-local-only';
  END IF;
END $$;

GRANT CONNECT ON DATABASE ieol TO ieol_app;
GRANT CONNECT ON DATABASE ieol_test TO ieol_app;
GRANT CONNECT ON DATABASE ieol_demo TO ieol_app;
GRANT ieol_app TO ieol;
