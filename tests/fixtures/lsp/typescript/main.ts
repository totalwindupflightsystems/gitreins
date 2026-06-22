// TypeScript LSP Test Fixture
// LSP: ts_ls / typescript-language-server
// Expected diagnostic: Argument of type 'string' is not assignable to parameter of type 'number'

function getUser(userId: number): string {
    return `User: ${userId}`;
}

// ERROR: passing string where number is expected
const result = getUser("abc");
