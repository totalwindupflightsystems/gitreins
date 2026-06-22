// C/C++ LSP Test Fixture
// LSP: clangd
// Expected diagnostic: cannot initialize a parameter of type 'int' with an lvalue of type 'const char[4]'

#include <stdio.h>

char* get_user(int user_id) {
    static char buffer[64];
    snprintf(buffer, sizeof(buffer), "User: %d", user_id);
    return buffer;
}

int main() {
    // ERROR: passing const char* where int is expected
    char* result = get_user("abc");
    printf("%s\n", result);
    return 0;
}
