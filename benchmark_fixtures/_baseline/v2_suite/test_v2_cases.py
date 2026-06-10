from v2_cases import *


def test_normalize_username_strips_and_lowercases():
    assert normalize_username("  Ada ") == "ada"


def test_clamp_score_bounds_values():
    assert clamp_score(120) == 100
    assert clamp_score(-5) == 0


def test_parse_retry_after_handles_blank():
    assert parse_retry_after("") == 0


def test_merge_labels_keeps_base():
    assert merge_labels(["bug"], ["v2"]) == ["bug", "v2"]


def test_is_truthy_flag_parses_strings():
    assert is_truthy_flag("false") is False


def test_safe_divide_handles_zero():
    assert safe_divide(10, 0) == 0


def test_first_non_empty_skips_blanks():
    assert first_non_empty(["", None, "x"]) == "x"


def test_parse_port_uses_default():
    assert parse_port(None) == 8000


def test_get_nested_returns_default():
    assert get_nested({}, "missing", "fallback") == "fallback"


def test_dedupe_preserve_order_is_stable():
    assert dedupe_preserve_order(["b", "a", "b"]) == ["b", "a"]


def test_public_api_name_new_contract():
    assert public_api_name() == "patchflow_v2"


def test_import_target_uses_helper():
    assert import_target() == "helper-ready"


def test_build_url_normalizes_slashes():
    assert build_url("https://x.test/", "/api") == "https://x.test/api"


def test_serialize_bool_lowercase():
    assert serialize_bool(True) == "true"


def test_resolve_handler_preserves_known_alias():
    assert resolve_handler("HTTP") == "http_handler"


def test_apply_discount_uses_percent():
    assert apply_discount(200, 10) == 180


def test_redact_token_masks_middle():
    assert redact_token("abcdef") == "ab***ef"


def test_summarize_items_returns_counts():
    assert summarize_items(["a", "a", "b"]) == {"total": 3, "unique": 2}


def test_refactor_total_sums_rows():
    assert refactor_total([{"amount": 2}, {"amount": 3}]) == 5


def test_stable_sort_users_by_id():
    users = [{"id": 2, "name": "a"}, {"id": 1, "name": "z"}]
    assert stable_sort_users(users) == [{"id": 1, "name": "z"}, {"id": 2, "name": "a"}]


def test_parse_cli_limit_blank_is_none():
    assert parse_cli_limit("") is None


def test_config_timeout_default():
    assert config_timeout({}) == 30


def test_env_list_ignores_empty_items():
    assert env_list("a,,b") == ["a", "b"]


def test_parse_log_level_uppercase():
    assert parse_log_level("debug") == "DEBUG"


def test_config_flag_false_default():
    assert config_flag({}, "enabled") is False


def test_retry_backoff_exponential():
    assert retry_backoff(3) == 800


def test_parse_csv_line_strips_cells():
    assert parse_csv_line("a, b ,,c") == ["a", "b", "c"]


def test_coerce_tags_from_string():
    assert coerce_tags("a,b") == ["a", "b"]


def test_select_primary_email_skips_unverified():
    user = {"emails": [{"value": "a", "verified": False}, {"value": "b", "verified": True}]}
    assert select_primary_email(user) == "b"


def test_render_status_handles_created():
    assert render_status(201) == "created"
