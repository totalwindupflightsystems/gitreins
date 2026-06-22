// Kotlin LSP Test Fixture
// LSP: kotlin-lsp (JetBrains) / kotlin-language-server
// Expected diagnostic: type mismatch — required Int, found String

fun getUser(userId: Int): String {
    return "User: $userId"
}

fun main() {
    // ERROR: passing String where Int is expected
    val result = getUser("abc")
    println(result)
}
