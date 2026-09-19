-- Extensions the application relies on.
-- pg_trgm backs the fuzzy matching used when normalising airport and hotel
-- names against the catalogue; unaccent lets a search for "Berlin" find
-- "Berlín".
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
