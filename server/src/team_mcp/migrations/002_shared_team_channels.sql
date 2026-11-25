-- The team's event feed may now contain older events its members could not see.
UPDATE settings
SET value=lower(hex(randomblob(16)))
WHERE key='event_epoch' AND EXISTS (SELECT 1 FROM channels WHERE kind='private');

UPDATE channels SET kind='team' WHERE kind='private';
DROP TABLE IF EXISTS channel_members;
