-- The banner now has a user-visible click-through in confession receipts.
UPDATE packages SET enabled=1
WHERE slug='banner'
	AND NOT EXISTS (
		SELECT 1 FROM audit
		WHERE target=(SELECT CAST(id AS TEXT) FROM packages WHERE slug='banner')
			AND (action='packages_toggle' OR (action='package_set' AND detail LIKE 'enabled=%'))
	);