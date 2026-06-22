// Rust LSP Test Fixture
// LSP: rust-analyzer
// Expected diagnostic: mismatched types — expected u32, found &str

fn get_user(user_id: u32) -> String {
    format!("User: {}", user_id)
}

fn main() {
    // ERROR: passing &str where u32 is expected
    let result = get_user("abc");
    println!("{}", result);
}
