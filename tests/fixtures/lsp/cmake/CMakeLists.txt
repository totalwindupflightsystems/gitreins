# CMake LSP Test Fixture
# LSP: neocmakelsp (Rust) or cmake-language-server
# Expected diagnostic: add_executable called with wrong number of arguments

cmake_minimum_required(VERSION 3.16)
project(LSPTest)

# ERROR: add_executable requires at least 2 arguments — missing source files
add_executable(my_app)
