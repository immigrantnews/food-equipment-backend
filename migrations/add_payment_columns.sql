-- IndMart freemium / Tinkoff payments
-- Платные подписчики: is_paid + момент оплаты.
-- ВАЖНО: is_paid тоже добавляется здесь — в исходной схеме его не было,
-- а webhook и get_matching_subscribers() на него опираются.

ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS is_paid BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP WITH TIME ZONE;
