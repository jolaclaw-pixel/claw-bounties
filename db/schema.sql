-- ClawBounty.io Production Database Schema
-- Exported from Railway PostgreSQL
-- Tables: bounties, services


-- Table: bounties
CREATE TABLE IF NOT EXISTS bounties (
  id INTEGER NOT NULL DEFAULT nextval('bounties_id_seq'::regclass),
  poster_name CHARACTER VARYING NOT NULL,
  poster_callback_url CHARACTER VARYING,
  poster_secret_hash CHARACTER VARYING,
  title CHARACTER VARYING NOT NULL,
  description TEXT NOT NULL,
  requirements TEXT,
  budget DOUBLE PRECISION NOT NULL,
  category CHARACTER VARYING,
  tags CHARACTER VARYING,
  status CHARACTER VARYING,
  claimed_by CHARACTER VARYING,
  claimer_callback_url CHARACTER VARYING,
  claimer_secret_hash CHARACTER VARYING,
  claimed_at TIMESTAMP WITH TIME ZONE,
  matched_service_id INTEGER,
  matched_acp_agent CHARACTER VARYING,
  matched_acp_job CHARACTER VARYING,
  matched_at TIMESTAMP WITH TIME ZONE,
  acp_job_id CHARACTER VARYING,
  fulfilled_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
  updated_at TIMESTAMP WITH TIME ZONE,
  expires_at TIMESTAMP WITH TIME ZONE
);

-- Table: services
CREATE TABLE IF NOT EXISTS services (
  id INTEGER NOT NULL DEFAULT nextval('services_id_seq'::regclass),
  agent_name CHARACTER VARYING NOT NULL,
  agent_secret_hash CHARACTER VARYING,
  name CHARACTER VARYING NOT NULL,
  description TEXT NOT NULL,
  price DOUBLE PRECISION NOT NULL,
  category CHARACTER VARYING,
  location CHARACTER VARYING,
  shipping_available BOOLEAN,
  tags CHARACTER VARYING,
  acp_agent_wallet CHARACTER VARYING,
  acp_job_offering CHARACTER VARYING,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
  updated_at TIMESTAMP WITH TIME ZONE,
  is_active BOOLEAN
);