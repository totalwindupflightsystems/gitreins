-- SQL LSP Test Fixture
-- LSP: sql-language-server
-- Expected diagnostic: syntax error — column not found or unknown identifier

-- Create a known schema so the LSP has something to validate against
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);

-- ERROR: referencing a column that doesn't exist in the schema
SELECT nonexistent_column FROM users WHERE id = 1;
