# Ruby LSP Test Fixture
# LSP: ruby-lsp (Shopify) or solargraph
# Expected diagnostic: wrong number of arguments (given 1, expected 2)

def get_user(user_id, name)
  "User: #{user_id} (#{name})"
end

# ERROR: missing required argument 'name'
result = get_user(123)
