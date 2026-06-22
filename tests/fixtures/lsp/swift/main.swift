// Swift LSP Test Fixture
// LSP: sourcekit-lsp
// Expected diagnostic: cannot convert value of type 'String' to expected argument type 'Int'

func getUser(userId: Int) -> String {
    return "User: \(userId)"
}

// ERROR: passing String where Int is expected
let result = getUser(userId: "abc")
print(result)
