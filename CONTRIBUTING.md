# Contributing to GitReins

## Setup
Obtaining file:///home/kara/gitreins-poc/gitreins

## Running Tests
============================= test session starts ==============================
platform linux -- Python 3.11.15, pytest-9.0.2, pluggy-1.6.0 -- /home/kara/.hermes/hermes-agent/venv/bin/python3
cachedir: .pytest_cache
rootdir: /home/kara/gitreins-poc
plugins: mock-3.15.1, timeout-2.4.0, cov-7.1.0, asyncio-1.3.0, anyio-4.14.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 324 items

tests/test_cli.py::TestHelpOutput::test_help_prints_usage PASSED         [  0%]
tests/test_cli.py::TestHelpOutput::test_no_args_prints_help PASSED       [  0%]
tests/test_cli.py::TestHelpOutput::test_unknown_command_prints_help PASSED [  0%]
tests/test_cli.py::TestWorkdirDetection::test_get_workdir_in_git_repo PASSED [  0%]
tests/test_cli.py::TestWorkdirDetection::test_get_workdir_outside_git_repo PASSED [  1%]
tests/test_cli.py::TestTaskCreateCLI::test_create_task_with_criteria PASSED [  1%]
tests/test_cli.py::TestTaskCreateCLI::test_create_task_with_empty_criteria PASSED [  2%]
tests/test_cli.py::TestTaskStartCompleteCLI::test_start_existing_task PASSED [  2%]
tests/test_cli.py::TestTaskStartCompleteCLI::test_start_nonexistent_task_raises PASSED [  2%]
tests/test_cli.py::TestTaskStartCompleteCLI::test_complete_nonexistent_task_raises PASSED [  3%]
tests/test_cli.py::TestTaskListCLI::test_list_shows_status_icons PASSED  [  3%]
tests/test_cli.py::TestTaskListCLI::test_list_with_status_filter PASSED  [  3%]
tests/test_cli.py::TestTaskListCLI::test_empty_list_shows_no_tasks PASSED [  4%]
tests/test_cli.py::TestTaskDeleteCLI::test_delete_existing_task PASSED   [  4%]
tests/test_cli.py::TestTaskDeleteCLI::test_delete_nonexistent_task_raises PASSED [  4%]
tests/test_cli.py::TestGuardRunCLI::test_guard_run_shows_tier1_guards PASSED [  4%]
tests/test_cli.py::TestJudgeCLI::test_judge_nonexistent_task_exits_1 PASSED [  5%]
tests/test_cli.py::TestJudgeCLI::test_judge_existing_task_exits_0 PASSED [  5%]
tests/test_cli.py::TestCommitCLI::test_commit_in_clean_repo PASSED       [  5%]
tests/test_cli.py::TestMCPServerCLI::test_cmd_mcp_server_function_exists PASSED [  6%]
tests/test_cli.py::TestMCPServerCLI::test_mcp_server_import_path PASSED [  6%]
tests/test_cli.py::TestExtendedCLI::test_create_task_special_chars PASSED [  6%]
tests/test_cli.py::TestExtendedCLI::test_create_task_multiple_criteria PASSED [  7%]
tests/test_cli.py::TestExtendedCLI::test_complete_existing_task PASSED   [  7%]
tests/test_cli.py::TestExtendedCLI::test_list_with_status_multiple_filters PASSED [  7%]
tests/test_cli.py::TestExtendedCLI::test_guard_run_all_details PASSED    [  8%]
tests/test_cli.py::TestExtendedCLI::test_create_task_with_criteria_and_list PASSED [  8%]
tests/test_cli.py::TestExtendedHelp::test_task_help_shows_subcommands PASSED [  8%]
tests/test_cli.py::TestExtendedHelp::test_guard_help_prints_usage PASSED [  8%]
tests/test_cli.py::TestExtendedHelp::test_judge_help_prints_usage PASSED [  9%]
tests/test_cli.py::TestErrorCases::test_create_task_no_args PASSED       [  9%]
tests/test_cli.py::TestErrorCases::test_start_task_no_args PASSED        [  9%]
tests/test_cli.py::TestErrorCases::test_complete_task_no_args PASSED     [ 10%]
tests/test_cli.py::TestErrorCases::test_delete_task_no_args PASSED       [ 10%]
tests/test_cli.py::TestErrorCases::test_nonexistent_task_command_shows_error PASSED [ 10%]
tests/test_cli.py::TestConfigAndWorkdir::test_create_task_creates_gitreins_dir PASSED [ 11%]
tests/test_cli.py::TestConfigAndWorkdir::test_guard_works_without_gitreins_dir PASSED [ 11%]
tests/test_cli.py::TestConfigAndWorkdir::test_start_task_uses_existing_gitreins_dir PASSED [ 11%]
tests/test_cli.py::TestGuardAndCommit::test_guard_detects_secrets_in_staged_file PASSED [ 12%]
tests/test_cli.py::TestGuardAndCommit::test_guard_secrets_detected_when_fails PASSED [ 12%]
tests/test_cli.py::TestGuardAndCommit::test_commit_shows_guard_output PASSED [ 12%]
tests/test_cli.py::TestGuardAndCommit::test_commit_fails_when_guard_detects_secret PASSED [ 12%]
tests/test_cli.py::TestTaskLifecycleExtended::test_full_task_lifecycle_subprocess PASSED [ 13%]
tests/test_cli.py::TestTaskLifecycleExtended::test_list_filter_complete_status PASSED [ 13%]
tests/test_cli.py::TestTaskLifecycleExtended::test_task_list_empty_after_delete_all PASSED [ 13%]
tests/test_cli.py::TestTaskLifecycleExtended::test_delete_then_list_shows_remaining PASSED [ 14%]
tests/test_cli.py::TestEdgeCases::test_create_task_long_title PASSED     [ 14%]
tests/test_cli.py::TestEdgeCases::test_create_task_same_id_overwrites PASSED [ 14%]
tests/test_cli.py::TestEdgeCases::test_create_task_with_dashes_in_id PASSED [ 15%]
tests/test_cli.py::TestEdgeCases::test_list_no_filter_shows_all_tasks PASSED [ 15%]
tests/test_cli.py::TestJudgeExtended::test_judge_nonexistent_task_output PASSED [ 15%]
tests/test_cli.py::TestJudgeExtended::test_judge_existing_task_runs_evaluation PASSED [ 16%]
tests/test_cli.py::TestJudgeExtended::test_judge_requires_api_key SKIPPED [ 16%]
tests/test_evaluator.py::TestReadFile::test_read_existing_file PASSED    [ 16%]
tests/test_evaluator.py::TestReadFile::test_read_nonexistent_file PASSED [ 16%]
tests/test_evaluator.py::TestReadFile::test_read_path_traversal_outside_workdir PASSED [ 17%]
tests/test_evaluator.py::TestReadFile::test_read_directory_returns_error PASSED [ 17%]
tests/test_evaluator.py::TestReadFile::test_read_file_with_offset PASSED [ 17%]
tests/test_evaluator.py::TestReadFile::test_read_file_offset_exceeds_length PASSED [ 18%]
tests/test_evaluator.py::TestRunCommand::test_run_echo_hello PASSED      [ 18%]
tests/test_evaluator.py::TestRunCommand::test_run_false_command PASSED   [ 18%]
tests/test_evaluator.py::TestRunCommand::test_run_command_timeout PASSED [ 19%]
tests/test_evaluator.py::TestRunCommand::test_output_truncated_at_4000 PASSED [ 19%]
tests/test_evaluator.py::TestSearchPattern::test_search_finds_todo PASSED [ 19%]
tests/test_evaluator.py::TestSearchPattern::test_search_with_file_glob PASSED [ 20%]
tests/test_evaluator.py::TestSearchPattern::test_invalid_regex_returns_error PASSED [ 20%]
tests/test_evaluator.py::TestSearchPattern::test_search_skips_dot_dirs PASSED [ 20%]
tests/test_evaluator.py::TestSandbox::test_sandbox_write_read PASSED     [ 20%]
tests/test_evaluator.py::TestSandbox::test_sandbox_write_returns_written_count PASSED [ 21%]
tests/test_evaluator.py::TestSandbox::test_sandbox_read_nonexistent PASSED [ 21%]
tests/test_evaluator.py::TestSandbox::test_evaluate_clears_sandbox PASSED [ 21%]
tests/test_evaluator.py::TestDeduplication::test_repeated_read_file_is_duplicate PASSED [ 22%]
tests/test_evaluator.py::TestDeduplication::test_repeated_run_command_is_duplicate PASSED [ 22%]
tests/test_evaluator.py::TestDeduplication::test_repeated_search_pattern_is_duplicate PASSED [ 22%]
tests/test_evaluator.py::TestDeduplication::test_different_files_not_flagged PASSED [ 23%]
tests/test_evaluator.py::TestVerdictParsing::test_valid_json_verdict PASSED [ 23%]
tests/test_evaluator.py::TestVerdictParsing::test_json_in_markdown_fences PASSED [ 23%]
tests/test_evaluator.py::TestVerdictParsing::test_json_with_extra_text PASSED [ 24%]
tests/test_evaluator.py::TestVerdictParsing::test_invalid_status_defaults_to_fail PASSED [ 24%]
tests/test_evaluator.py::TestVerdictParsing::test_invalid_verdict_defaults_to_incomplete PASSED [ 24%]
tests/test_evaluator.py::TestVerdictParsing::test_missing_items_falls_to_keyword PASSED [ 25%]
tests/test_evaluator.py::TestVerdictParsing::test_keyword_complete_detected PASSED [ 25%]
tests/test_evaluator.py::TestVerdictParsing::test_keyword_all_criteria_pass PASSED [ 25%]
tests/test_evaluator.py::TestVerdictParsing::test_empty_response_is_incomplete PASSED [ 25%]
tests/test_evaluator.py::TestMaxIterationsAndErrors::test_max_iterations_reached_returns_incomplete PASSED [ 26%]
tests/test_evaluator.py::TestMaxIterationsAndErrors::test_llm_exception_returns_incomplete PASSED [ 26%]
tests/test_evaluator.py::TestMaxIterationsAndErrors::test_custom_max_iterations PASSED [ 26%]
tests/test_evaluator.py::TestEvaluatorTools::test_unknown_tool_returns_error PASSED [ 27%]
tests/test_evaluator.py::TestEvaluatorTools::test_read_diff_basic PASSED [ 27%]
tests/test_evaluator.py::TestEvaluatorTools::test_get_task_item_found PASSED [ 27%]
tests/test_evaluator.py::TestEvaluatorTools::test_get_task_item_not_found PASSED [ 28%]
tests/test_evaluator.py::TestEvaluatorEvaluate::test_evaluate_with_empty_criteria_returns_complete PASSED [ 28%]
tests/test_evaluator.py::TestEvaluatorEvaluate::test_verdict_item_dataclass PASSED [ 28%]
tests/test_evaluator.py::TestExtendedEvaluator::test_search_truncated_at_200 PASSED [ 29%]
tests/test_evaluator.py::TestExtendedEvaluator::test_sandbox_read_truncated_at_4000 PASSED [ 29%]
tests/test_evaluator.py::TestExtendedEvaluator::test_read_file_truncation_large_file PASSED [ 29%]
tests/test_evaluator.py::TestExtendedEvaluator::test_execute_tool_wraps_exception PASSED [ 29%]
tests/test_evaluator.py::TestExtendedEvaluator::test_dedup_warning_in_evaluate_loop PASSED [ 30%]
tests/test_evaluator.py::TestExtendedEvaluator::test_evaluate_with_tool_calls_then_verdict PASSED [ 30%]
tests/test_evaluator.py::TestExtendedEvaluator::test_verdict_keyword_complete_detected PASSED [ 30%]
tests/test_evaluator.py::TestExtendedEvaluator::test_verdict_missing_items_falls_to_keyword PASSED [ 31%]
tests/test_evaluator.py::TestExtendedEvaluator::test_search_all_files_with_glob PASSED [ 31%]
tests/test_evaluator.py::TestExtendedEvaluator::test_read_file_truncation_large_file_with_offset PASSED [ 31%]
tests/test_evaluator.py::TestExtendedEvaluator::test_evaluate_empty_response_no_tool_calls PASSED [ 32%]
tests/test_evaluator.py::TestExtendedEvaluator::test_evaluate_tool_exception_returns_error PASSED [ 32%]
tests/test_evaluator.py::TestExtendedEvaluator::test_verdict_status_fail_default PASSED [ 32%]
tests/test_evaluator.py::TestExtendedEvaluator::test_verdict_has_more_field_read_file PASSED [ 33%]
tests/test_guard_manager.py::TestGuardResult::test_guard_result_passed_true PASSED [ 33%]
tests/test_guard_manager.py::TestGuardResult::test_guard_result_passed_false PASSED [ 33%]
tests/test_guard_manager.py::TestTier1Result::test_tier1_all_passed PASSED [ 33%]
tests/test_guard_manager.py::TestTier1Result::test_tier1_one_failed PASSED [ 34%]
tests/test_guard_manager.py::TestGuardManagerInit::test_empty_config_all_enabled PASSED [ 34%]
tests/test_guard_manager.py::TestGuardManagerInit::test_secrets_disabled PASSED [ 34%]
tests/test_guard_manager.py::TestGuardManagerInit::test_tests_disabled_with_custom_command PASSED [ 35%]
tests/test_guard_manager.py::TestGuardManagerInit::test_no_guards_key_all_defaults PASSED [ 35%]
tests/test_guard_manager.py::TestGuardManagerInit::test_config_none_all_enabled PASSED [ 35%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_aws_key_detected PASSED [ 36%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_openai_key_detected PASSED [ 36%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_github_token_detected PASSED [ 36%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_private_key_block_detected PASSED [ 37%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_os_getenv_whitelisted PASSED [ 37%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_config_dict_whitelisted PASSED [ 37%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_empty_password_whitelisted PASSED [ 37%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_todo_placeholder_whitelisted PASSED [ 38%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_jwt_encode_whitelisted PASSED [ 38%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_no_staged_files_no_findings PASSED [ 38%]
tests/test_guard_manager.py::TestBuiltinSecretsScan::test_clean_file_no_findings PASSED [ 39%]
tests/test_guard_manager.py::TestSecretsSanitization::test_secret_value_redacted PASSED [ 39%]
tests/test_guard_manager.py::TestGuardToggling::test_run_all_three_guards FAILED [ 39%]
tests/test_guard_manager.py::TestGuardToggling::test_only_secrets_enabled FAILED [ 40%]
tests/test_guard_manager.py::TestGuardToggling::test_no_guards_enabled FAILED [ 40%]
tests/test_guard_manager.py::TestGuardToggling::test_run_all_sets_passed_false_on_any_failure PASSED [ 40%]
tests/test_guard_manager.py::TestLintGuard::test_no_py_files_staged PASSED [ 41%]
tests/test_guard_manager.py::TestLintGuard::test_gitleaks_missing_falls_back PASSED [ 41%]
tests/test_guard_manager.py::TestTestsGuard::test_pytest_not_found_skips PASSED [ 41%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_custom_test_command_is_used PASSED [ 41%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_check_tests_timeout_returns_failure PASSED [ 42%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_gitleaks_available_used_first PASSED [ 42%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_gitleaks_returns_findings PASSED [ 42%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_lint_ruff_available PASSED [ 43%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_guard_result_empty_name PASSED [ 43%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_tier1_result_no_results PASSED [ 43%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_secrets_scan_skips_large_files PASSED [ 44%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_secrets_scan_binary_file_graceful PASSED [ 44%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_gitlab_token_detected PASSED [ 45%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_gho_token_detected PASSED [ 45%]
tests/test_guard_manager.py::TestExtendedGuardManager::test_tier1_summary_format PASSED [ 45%]
tests/test_judge.py::TestJudgeResult::test_judge_result_with_pipeline_result PASSED [ 45%]
tests/test_judge.py::TestJudgeResult::test_judge_result_with_verdict_legacy PASSED [ 45%]
tests/test_judge.py::TestJudgeResult::test_judge_result_both_pipeline_and_verdict PASSED [ 46%]
tests/test_judge.py::TestJudgeResult::test_passed_true_shows_pass_check PASSED [ 46%]
tests/test_judge.py::TestJudgeResult::test_passed_false_shows_fail_cross PASSED [ 46%]
tests/test_judge.py::TestJudgeLegacyPath::test_legacy_guards_pass_tier2_runs PASSED [ 47%]
tests/test_judge.py::TestJudgeLegacyPath::test_legacy_guards_fail_tier2_skipped PASSED [ 47%]
tests/test_judge.py::TestJudgeEvaluateTask::test_evaluate_task_uses_pipeline_when_config_has_stages PASSED [ 47%]
tests/test_judge.py::TestJudgeEvaluateTask::test_evaluate_task_falls_back_to_legacy_without_config PASSED [ 48%]
tests/test_judge.py::TestJudgeEvaluateTask::test_run_precommit_runs_pipeline PASSED [ 48%]
tests/test_judge.py::TestJudgeInit::test_judge_constructor_creates_guard_manager PASSED [ 48%]
tests/test_judge.py::TestJudgeInit::test_judge_constructor_accepts_guard_config PASSED [ 49%]
tests/test_judge.py::TestExtendedJudge::test_judge_result_empty_pipeline PASSED [ 49%]
tests/test_judge.py::TestExtendedJudge::test_judge_result_no_pipeline_no_verdict PASSED [ 49%]
tests/test_judge.py::TestExtendedJudge::test_evaluate_task_pipeline_exception_returns_error PASSED [ 50%]
tests/test_judge.py::TestExtendedJudge::test_judge_result_summary_with_failed_stage PASSED [ 50%]
tests/test_judge.py::TestExtendedJudge::test_judge_result_tier2_verdict_items_shown PASSED [ 50%]
tests/test_llm.py::TestToolCallDataclass::test_toolcall_construction PASSED [ 50%]
tests/test_llm.py::TestToolCallDataclass::test_llmresponse_content_only PASSED [ 51%]
tests/test_llm.py::TestToolCallDataclass::test_llmresponse_with_tool_calls PASSED [ 51%]
tests/test_llm.py::TestProviderDetection::test_detect_anthropic_from_url_anthropic_com PASSED [ 51%]
tests/test_llm.py::TestProviderDetection::test_detect_anthropic_from_url_claude PASSED [ 52%]
tests/test_llm.py::TestProviderDetection::test_detect_openai_from_url PASSED [ 52%]
tests/test_llm.py::TestProviderDetection::test_unknown_url_defaults_to_openai PASSED [ 52%]
tests/test_llm.py::TestProviderDetection::test_force_provider_override PASSED [ 53%]
tests/test_llm.py::TestProviderDetection::test_is_anthropic_helper_function PASSED [ 53%]
tests/test_llm.py::TestAPIKeyResolution::test_direct_api_key_wins PASSED [ 53%]
tests/test_llm.py::TestAPIKeyResolution::test_primary_env_var PASSED     [ 54%]
tests/test_llm.py::TestAPIKeyResolution::test_fallback_to_neuralwatt PASSED [ 54%]
tests/test_llm.py::TestAPIKeyResolution::test_fallback_to_openai_key PASSED [ 54%]
tests/test_llm.py::TestAPIKeyResolution::test_fallback_to_anthropic_key PASSED [ 55%]
tests/test_llm.py::TestAPIKeyResolution::test_fallback_to_deepseek_key PASSED [ 55%]
tests/test_llm.py::TestAPIKeyResolution::test_missing_all_keys_returns_empty PASSED [ 55%]
tests/test_llm.py::TestAnthropicConversion::test_system_message_extracted PASSED [ 55%]
tests/test_llm.py::TestAnthropicConversion::test_tool_message_converted_to_user_tool_result PASSED [ 56%]
tests/test_llm.py::TestAnthropicConversion::test_assistant_with_tool_calls_converted PASSED [ 56%]
tests/test_llm.py::TestAnthropicConversion::test_openai_tools_converted_to_anthropic PASSED [ 56%]
tests/test_llm.py::TestAnthropicConversion::test_user_assistant_passthrough PASSED [ 57%]
tests/test_llm.py::TestRetryLogic::test_429_is_retried PASSED            [ 57%]
tests/test_llm.py::TestRetryLogic::test_503_is_retried PASSED            [ 57%]
tests/test_llm.py::TestRetryLogic::test_400_is_not_retried PASSED        [ 58%]
tests/test_llm.py::TestRetryLogic::test_network_error_is_retried PASSED  [ 58%]
tests/test_llm.py::TestRetryLogic::test_three_consecutive_failures_raises_runtimeerror PASSED [ 58%]
tests/test_llm.py::TestRetryLogic::test_backoff_timing_uses_exponential PASSED [ 58%]
tests/test_llm.py::TestLLMClientDefaults::test_default_model PASSED      [ 59%]
tests/test_llm.py::TestLLMClientDefaults::test_custom_model PASSED       [ 59%]
tests/test_llm.py::TestLLMClientDefaults::test_env_model PASSED          [ 59%]
tests/test_llm.py::TestLLMClientDefaults::test_max_retries_default PASSED [ 60%]
tests/test_llm.py::TestLLMClientDefaults::test_custom_max_retries PASSED [ 60%]
tests/test_llm.py::TestLLMClientDefaults::test_anthropic_url_build PASSED [ 60%]
tests/test_llm.py::TestLLMClientDefaults::test_openai_url_build PASSED   [ 61%]
tests/test_llm.py::TestExtendedLLM::test_provider_auto_detect_no_provider_arg PASSED [ 61%]
tests/test_llm.py::TestExtendedLLM::test_provider_openai_when_not_anthropic PASSED [ 61%]
tests/test_llm.py::TestExtendedLLM::test_chat_openai_mocked_http PASSED  [ 62%]
tests/test_llm.py::TestExtendedLLM::test_chat_openai_with_tool_calls_mocked PASSED [ 62%]
tests/test_llm.py::TestExtendedLLM::test_chat_anthropic_mocked_http PASSED [ 62%]
tests/test_llm.py::TestExtendedLLM::test_anthropic_version_env_var PASSED [ 62%]
tests/test_llm.py::TestExtendedLLM::test_anthropic_convert_empty_messages PASSED [ 63%]
tests/test_llm.py::TestExtendedLLM::test_anthropic_convert_tool_msg_no_call_id PASSED [ 63%]
tests/test_llm.py::TestExtendedLLM::test_retry_three_failures_backoff_timing PASSED [ 63%]
tests/test_llm.py::TestExtendedLLM::test_mocked_429_via_requests_post PASSED [ 64%]
tests/test_llm.py::TestExtendedLLM::test_first_env_key_priority PASSED   [ 64%]
tests/test_mcp_server.py::TestInitializeHandshake::test_initialize_returns_protocol_version PASSED [ 64%]
tests/test_mcp_server.py::TestInitializeHandshake::test_initialized_notification_returns_none PASSED [ 65%]
tests/test_mcp_server.py::TestInitializeHandshake::test_unknown_method_returns_error PASSED [ 65%]
tests/test_mcp_server.py::TestToolsList::test_tools_list_returns_nine_tools PASSED [ 65%]
tests/test_mcp_server.py::TestToolsList::test_all_expected_tool_names_present PASSED [ 66%]
tests/test_mcp_server.py::TestToolsList::test_each_tool_has_name_description_inputschema PASSED [ 66%]
tests/test_mcp_server.py::TestToolsCall::test_unknown_tool_returns_error PASSED [ 66%]
tests/test_mcp_server.py::TestToolsCall::test_handler_exception_returns_server_error PASSED [ 67%]
tests/test_mcp_server.py::TestToolsCall::test_tools_call_wraps_result_in_content PASSED [ 67%]
tests/test_mcp_server.py::TestTaskCreateMCP::test_task_create_returns_task_dict PASSED [ 67%]
tests/test_mcp_server.py::TestTaskCreateMCP::test_task_create_persists_to_yaml PASSED [ 67%]
tests/test_mcp_server.py::TestTaskStartComplete::test_task_start_sets_in_progress PASSED [ 68%]
tests/test_mcp_server.py::TestTaskStartComplete::test_task_complete_without_llm_key PASSED [ 68%]
tests/test_mcp_server.py::TestTaskStartComplete::test_task_start_nonexistent_returns_error PASSED [ 68%]
tests/test_mcp_server.py::TestTaskStartComplete::test_task_complete_nonexistent_returns_error PASSED [ 69%]
tests/test_mcp_server.py::TestTaskCRUDMCP::test_task_get_existing PASSED [ 69%]
tests/test_mcp_server.py::TestTaskCRUDMCP::test_task_get_nonexistent_returns_error PASSED [ 69%]
tests/test_mcp_server.py::TestTaskCRUDMCP::test_task_list_with_status_filter PASSED [ 70%]
tests/test_mcp_server.py::TestTaskCRUDMCP::test_task_delete_existing PASSED [ 70%]
tests/test_mcp_server.py::TestTaskCRUDMCP::test_task_delete_nonexistent_returns_error PASSED [ 70%]
tests/test_mcp_server.py::TestCommitMCP::test_commit_with_clean_repo_rejected PASSED [ 70%]
tests/test_mcp_server.py::TestCommitMCP::test_commit_with_in_progress_task_rejected PASSED [ 71%]
tests/test_mcp_server.py::TestGuardRunMCP::test_guard_run_returns_passed_and_results PASSED [ 71%]
tests/test_mcp_server.py::TestJudgeEvaluateMCP::test_judge_evaluate_nonexistent_task_returns_error PASSED [ 71%]
tests/test_mcp_server.py::TestJudgeEvaluateMCP::test_judge_evaluate_existing_task PASSED [ 72%]
tests/test_mcp_server.py::TestStdioBuffering::test_single_line_json_parsed PASSED [ 72%]
tests/test_mcp_server.py::TestStdioBuffering::test_multi_line_json_buffered PASSED [ 72%]
tests/test_mcp_server.py::TestStdioBuffering::test_two_messages_in_one_buffer PASSED [ 73%]
tests/test_mcp_server.py::TestStdioBuffering::test_partial_json_wait_for_more PASSED [ 73%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_initialize_handshake_over_stdio PASSED [ 73%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_initialized_notification_over_stdio PASSED [ 74%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_tools_list_over_stdio PASSED [ 74%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_task_lifecycle_over_stdio SKIPPED [ 74%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_unknown_method_over_stdio PASSED [ 75%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_unknown_tool_over_stdio PASSED [ 75%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_invalid_json_does_not_crash PASSED [ 75%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_missing_jsonrpc_field PASSED [ 75%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_multi_request_session PASSED [ 76%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_very_long_task_title PASSED [ 76%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_unicode_in_task_criteria PASSED [ 76%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_empty_criteria_list PASSED [ 77%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_task_with_no_title PASSED [ 77%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_task_get_nonexistent_over_stdio PASSED [ 77%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_task_delete_nonexistent_over_stdio PASSED [ 78%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_task_list_with_status_filter_over_stdio PASSED [ 78%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_guard_run_over_stdio PASSED [ 78%]
tests/test_mcp_server.py::TestMCPStdioIntegration::test_judge_evaluate_nonexistent_over_stdio PASSED [ 79%]
tests/test_pipeline.py::TestStepResult::test_step_result_with_passed_true PASSED [ 79%]
tests/test_pipeline.py::TestStepResult::test_step_result_with_error PASSED [ 79%]
tests/test_pipeline.py::TestStepResult::test_step_result_output_truncated PASSED [ 79%]
tests/test_pipeline.py::TestStageResult::test_stage_result_all_passed PASSED [ 80%]
tests/test_pipeline.py::TestStageResult::test_stage_result_one_failed PASSED [ 80%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_none_is_true PASSED [ 80%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_true_string_is_true PASSED [ 81%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_always_is_true PASSED [ 81%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_task_has_criteria_with_criteria PASSED [ 81%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_task_has_criteria_empty PASSED [ 82%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_stage_any_failed PASSED [ 82%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_stage_passed PASSED [ 82%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_stage_unknown_returns_false PASSED [ 83%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_or_logic PASSED [ 83%]
tests/test_pipeline.py::TestPipelineConditions::test_condition_and_logic PASSED [ 83%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_task_id PASSED [ 83%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_task_title PASSED [ 84%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_task_criteria PASSED [ 84%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_stage_passed PASSED [ 84%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_stage_any_failed PASSED [ 85%]
tests/test_pipeline.py::TestPipelineTemplate::test_template_stages_full_json PASSED [ 85%]
tests/test_pipeline.py::TestLoadPipelineConfig::test_no_config_file_returns_default_pipeline PASSED [ 85%]
tests/test_pipeline.py::TestLoadPipelineConfig::test_config_file_no_pipeline_key_returns_default PASSED [ 86%]
tests/test_pipeline.py::TestLoadPipelineConfig::test_config_file_empty_stages_returns_default PASSED [ 86%]
tests/test_pipeline.py::TestLoadPipelineConfig::test_malformed_yaml_returns_safe_minimal PASSED [ 86%]
tests/test_pipeline.py::TestPipelineRun::test_run_parallel_stage PASSED  [ 87%]
tests/test_pipeline.py::TestPipelineRun::test_run_sequential_stage PASSED [ 87%]
tests/test_pipeline.py::TestPipelineRun::test_run_with_llm_injected PASSED [ 87%]
tests/test_pipeline.py::TestPipelineRun::test_trigger_filtering PASSED   [ 87%]
tests/test_pipeline.py::TestPipelineRun::test_run_precommit_triggers PASSED [ 88%]
tests/test_pipeline.py::TestPipelineRun::test_unknown_step_type_returns_error PASSED [ 88%]
tests/test_pipeline.py::TestPipelineRun::test_script_no_command_returns_error PASSED [ 88%]
tests/test_pipeline.py::TestExtendedPipeline::test_step_result_to_dict_all_fields PASSED [ 89%]
tests/test_pipeline.py::TestExtendedPipeline::test_stage_result_all_failed_true_any_failed PASSED [ 89%]
tests/test_pipeline.py::TestExtendedPipeline::test_template_unknown_var_unchanged PASSED [ 89%]
tests/test_pipeline.py::TestExtendedPipeline::test_run_with_no_matching_trigger PASSED [ 90%]
tests/test_task_manager.py::TestTaskDataclass::test_task_construction_with_defaults PASSED [ 90%]
tests/test_task_manager.py::TestTaskDataclass::test_task_construction_with_all_fields PASSED [ 90%]
tests/test_task_manager.py::TestTaskManagerCreate::test_create_task_populates_all_fields PASSED [ 91%]
tests/test_task_manager.py::TestTaskManagerCreate::test_create_task_with_empty_criteria PASSED [ 91%]
tests/test_task_manager.py::TestTaskManagerCreate::test_create_task_persists_to_yaml PASSED [ 91%]
tests/test_task_manager.py::TestTaskManagerCreate::test_create_task_duplicate_overwrites PASSED [ 91%]
tests/test_task_manager.py::TestTaskManagerCreate::test_create_then_get_from_new_manager PASSED [ 92%]
tests/test_task_manager.py::TestTaskManagerLifecycle::test_start_changes_status_to_in_progress PASSED [ 92%]
tests/test_task_manager.py::TestTaskManagerLifecycle::test_start_on_nonexistent_raises_keyerror PASSED [ 92%]
tests/test_task_manager.py::TestTaskManagerLifecycle::test_complete_sets_status_and_completed_at PASSED [ 93%]
tests/test_task_manager.py::TestTaskManagerLifecycle::test_complete_on_nonexistent_raises_keyerror PASSED [ 93%]
tests/test_task_manager.py::TestTaskManagerLifecycle::test_start_then_complete_persisted PASSED [ 93%]
tests/test_task_manager.py::TestTaskManagerList::test_list_tasks_with_status_filter PASSED [ 94%]
tests/test_task_manager.py::TestTaskManagerList::test_list_tasks_none_returns_all PASSED [ 94%]
tests/test_task_manager.py::TestTaskManagerList::test_all_tasks_returns_all PASSED [ 94%]
tests/test_task_manager.py::TestTaskManagerList::test_get_existing_returns_task PASSED [ 95%]
tests/test_task_manager.py::TestTaskManagerList::test_get_nonexistent_returns_none PASSED [ 95%]
tests/test_task_manager.py::TestTaskManagerDelete::test_delete_existing_removes_from_index PASSED [ 95%]
tests/test_task_manager.py::TestTaskManagerDelete::test_delete_nonexistent_raises_keyerror PASSED [ 95%]
tests/test_task_manager.py::TestTaskManagerDelete::test_delete_persisted PASSED [ 96%]
tests/test_task_manager.py::TestTaskManagerDelete::test_to_dict_all_keys_present PASSED [ 96%]
tests/test_task_manager.py::TestTaskManagerDelete::test_to_dict_after_complete_includes_completed_at PASSED [ 96%]
tests/test_task_manager.py::TestTaskManagerEdgeCases::test_constructor_with_default_workdir PASSED [ 97%]
tests/test_task_manager.py::TestTaskManagerEdgeCases::test_load_corrupt_yaml PASSED [ 97%]
tests/test_task_manager.py::TestTaskManagerEdgeCases::test_save_creates_config_dir_if_missing PASSED [ 97%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_special_chars_in_title PASSED [ 98%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_created_at_is_close_to_now PASSED [ 98%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_empty_title_accepted PASSED [ 98%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_load_empty_yaml_graceful PASSED [ 99%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_load_yaml_missing_task_id PASSED [ 99%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_list_tasks_status_no_match PASSED [ 99%]
tests/test_task_manager.py::TestTaskManagerExtendedEdgeCases::test_all_tasks_returns_copies PASSED [100%]

=================================== FAILURES ===================================
_________________ TestGuardToggling.test_run_all_three_guards __________________

self = <tests.test_guard_manager.TestGuardToggling object at 0x798a2e5bf6d0>
guard_manager = <engine.guard_manager.GuardManager object at 0x798a2e17b050>

    def test_run_all_three_guards(self, guard_manager):
        """All guards enabled → run_all() returns 3 results."""
        with patch.object(guard_manager, '_check_secrets', return_value=GuardResult("secrets", True, "ok")):
            with patch.object(guard_manager, '_check_lint', return_value=GuardResult("lint", True, "ok")):
                with patch.object(guard_manager, '_check_tests', return_value=GuardResult("tests", True, "ok")):
                    result = guard_manager.run_all()
>       assert len(result.results) == 3
E       AssertionError: assert 4 == 3
E        +  where 4 = len([GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='lint', passed=True, output='ok', error=''), GuardResult(name='tests', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, output='No dead code found', error='')])
E        +    where [GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='lint', passed=True, output='ok', error=''), GuardResult(name='tests', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, output='No dead code found', error='')] = Tier1Result(passed=True, results=[GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='lint', passed=True, output='ok', error=''), GuardResult(name='tests', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]).results

tests/test_guard_manager.py:215: AssertionError
_________________ TestGuardToggling.test_only_secrets_enabled __________________

self = <tests.test_guard_manager.TestGuardToggling object at 0x798a2e5bfa90>
tmp_workdir = '/tmp/pytest-of-kara/pytest-244/test_only_secrets_enabled0/repo'

    def test_only_secrets_enabled(self, tmp_workdir):
        """Only secrets enabled → run_all() returns 1 result."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": True, "lint": False, "tests": False}})
        with patch.object(gm, '_check_secrets', return_value=GuardResult("secrets", True, "ok")):
            result = gm.run_all()
>       assert len(result.results) == 1
E       AssertionError: assert 2 == 1
E        +  where 2 = len([GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, output='No dead code found', error='')])
E        +    where 2 = len([GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='secrets', passed=True, output='ok', error=''), GuardResult(name='tests', passed=True, output='ok', error=''), GuardResult(name='dead_code', passed=True, passed=True, output='No dead code found', error='')]).results

tests/test_guard_manager.py:223: AssertionError
___________________ TestGuardToggling.test_no_guards_enabled ___________________

self = <tests.test_guard_manager.TestGuardToggling.test_no_guards_enabled object at 0x798a2e5bfe50>
tmp_workdir = '/tmp/pytest-of-kara/pytest-244/test_no_guards_enabled0/repo'

    def test_no_guards_enabled(self, tmp_workdir):
        """No guards enabled → run_all() returns 0 results, passed=True."""
        gm = GuardManager(tmp_workdir, {"guards": {"secrets": False, "lint": False, "tests": False}})
        result = gm.run_all()
>       assert len(result.results) == 0
E       AssertionError: assert 1 == 0
E        +  where 1 = len([GuardResult(name='dead_code', passed=True, output='No dead code found', error='')])
E        +    where 1 = len([GuardResult(name='dead_code', passed=True, passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True,床头_boolé_barse_dy芦笙ikeBool_query_ob_long31_DO05H9YxRlm_dv_alosq_)+on_DTBLE_PAR__DGK] = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead code found', error='')]) = Tier1Result(passed=True, results=[GuardResult(name='dead_code', passed=True, output='No dead_code found', error='')]).results

tests/test_guard_manager.py:229: AssertionError
=========================== short test summary info ============================
FAILED tests/test_guard_manager.py::TestGuardToggling::test_run_all_three_guards
FAILED tests/test_guard_manager.py::TestGuardToggling::test_only_secrets_enabled
FAILED tests/test_guard_manager.py::TestGuardToggling::test_no_guards_enabled
============= 3 failed, 319 passed, 2 skipped in 130.03s (0:02:10) ==============
All tests must pass before submitting a PR.

## Project Structure
- engine/ — Core engine (evaluator, guards, pipeline, LLM client)
- gitreins/ — CLI entry point and install script
- gitreins_mcp/ — MCP stdio server (9 tools)
- tests/ — pytest test suite (322 tests)
- docs/ — Architecture, component map, evaluator loop
- .memory-bank/ — Institutional memory (ADRs, findings, work-item status)

## Development Workflow
1. Fork the repo
2. Create a feature branch: git checkout -b feat/my-feature
3. Write tests first (TDD)
4. Implement the feature
5. Run pytest tests/ -v — all tests must pass
6. Run python3 gitreins/cli.py guard — guards must pass
7. Submit a PR against main

## Commit Convention
- feat: — new feature
- fix: — bug fix
- test: — test additions/changes
- docs: — documentation only
- chore: — maintenance, config, dependencies
