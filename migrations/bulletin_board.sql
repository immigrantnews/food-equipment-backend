-- Bulletin board: users + listings (Postgres, db "indmart")
-- Run as superuser (postgres) — CREATE EXTENSION pg_trgm requires it.
-- The app connects as indmart_user, so ownership is transferred at the end,
-- otherwise the app cannot read/write these tables.

-- Users table (registered via Telegram Login)
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE NOT NULL,
  telegram_username VARCHAR(255),
  first_name VARCHAR(255),
  last_name VARCHAR(255),
  photo_url TEXT,
  user_type VARCHAR(20) DEFAULT 'manufacturer' CHECK (user_type IN ('reseller', 'manufacturer')),
  phone VARCHAR(20),
  city VARCHAR(100),
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Listings table
CREATE TABLE IF NOT EXISTS listings (
  id SERIAL PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
  title VARCHAR(500) NOT NULL,
  category VARCHAR(100),
  condition VARCHAR(20) DEFAULT 'used' CHECK (condition IN ('new', 'used', 'parts')),
  price INTEGER NOT NULL,
  city VARCHAR(100),
  region VARCHAR(100),
  description TEXT,
  photos JSONB DEFAULT '[]',
  video_url TEXT,
  phone VARCHAR(20),
  telegram_username VARCHAR(255),
  status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'sold', 'archived', 'moderation')),
  views INTEGER DEFAULT 0,
  ai_verdict VARCHAR(20),
  ai_market_min INTEGER,
  ai_market_max INTEGER,
  ai_comment TEXT,
  expires_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '90 days',
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for fast search
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category);
CREATE INDEX IF NOT EXISTS idx_listings_city ON listings(city);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_created ON listings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_user ON listings(user_id);

-- Full text search index
CREATE INDEX IF NOT EXISTS idx_listings_fts ON listings
USING gin(to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(description,'')));

-- pg_trgm for fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_listings_title_trgm ON listings USING gin(title gin_trgm_ops);

-- Hand ownership to the app's role (it connects as indmart_user)
ALTER TABLE users OWNER TO indmart_user;
ALTER TABLE listings OWNER TO indmart_user;
ALTER SEQUENCE users_id_seq OWNER TO indmart_user;
ALTER SEQUENCE listings_id_seq OWNER TO indmart_user;
