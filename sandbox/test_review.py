"""
Test script for GR-065 CodeRabbit-style commit review.
Creates a file with intentional issues, stages it, runs review.
"""
import os
import sys
import subprocess

# Create a test file with intentional issues
test_file = "/home/kara/gitreins-poc/sandbox/test_review_sample.py"
os.makedirs(os.path.dirname(test_file), exist_ok=True)

code = '''"""A test module with intentional issues for code review."""
import sqlite3

# BUG: SQL injection — concatenating user input into query
def get_user(user_id):
    query = "SELECT * FROM users WHERE id = " + str(user_id)
    conn = sqlite3.connect(":memory:")
    return conn.execute(query).fetchone()

# ANTI-PATTERN: mutable default argument
def add_item(item, items=[]):
    items.append(item)
    return items

# BUG: bare except swallowing all errors
def parse_config(path):
    try:
        with open(path) as f:
            return f.read()
    except:
        return ""

# SECURITY: hardcoded API key pattern (test-only but flaggable)
DEFAULT_KEY = "sk-test-12345678901234567890"

# STYLE: magic number
def calculate(price):
    return price * 1.0825  # no named constant for tax rate
'''

with open(test_file, "w") as f:
    f.write(code)

# Stage the file
subprocess.run(["git", "add", test_file], cwd="/home/kara/gitreins-poc", check=True)

# Set env and run review
os.environ["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")

from engine.llm import LLMClient
from engine.commit_audit import CommitAuditor, COMMIT_REVIEW_SYSTEM_PROMPT

llm = LLMClient()
auditor = CommitAuditor(
    llm, "/home/kara/gitreins-poc",
    review_mode="review",
    review_checks={"bugs": True, "security": True, "anti_patterns": True, "style": True, "performance": False},
    review_severity="standard",
    review_suggest_fix=True,
    max_iterations=3,
)

print("=" * 60)
print("GR-065 CodeRabbit Review — Live Test")
print("=" * 60)
print(f"File: {test_file}")
print(f"Issues planted: SQL injection, mutable default, bare except, hardcoded key, magic number")
print("-" * 60)

result = auditor.review("feat: add user query and config parsing utilities")

print(f"\nResult: {'PASS' if result.valid else 'ISSUES FOUND'}")
print(f"Message valid: {result.message_valid}")
print(f"Summary: {result.summary}")
print(f"\nIssues found: {len(result.issues)}")
print("-" * 40)

for issue in result.issues:
    print(f"\n  [{issue.severity.upper()}] [{issue.category}] {issue.file}:{issue.line}")
    print(f"  Title: {issue.title}")
    if issue.description:
        print(f"  Description: {issue.description}")
    if issue.suggestion:
        print(f"  Suggestion: {issue.suggestion}")

# Cleanup
subprocess.run(["git", "reset", "HEAD", test_file], cwd="/home/kara/gitreins-poc")
os.remove(test_file)

print("\n" + "=" * 60)
print(f"Iterations used: {result.iterations_used}")
print("Review complete.")
