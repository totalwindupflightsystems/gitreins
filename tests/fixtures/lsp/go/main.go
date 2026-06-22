// Go LSP Test Fixture
// LSP: gopls
// Expected diagnostic: cannot use "abc" (untyped string constant) as int value

package main

import "fmt"

func getUser(userID int) string {
	return fmt.Sprintf("User: %d", userID)
}

func main() {
	// ERROR: passing string where int is expected
	result := getUser("abc")
	fmt.Println(result)
}
