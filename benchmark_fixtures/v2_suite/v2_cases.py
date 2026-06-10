"""Small intentionally broken functions for v2 benchmark tasks."""


def normalize_username(value):
    return value


def clamp_score(value):
    return value


def parse_retry_after(value):
    return int(value)


def merge_labels(base, extra):
    return extra


def is_truthy_flag(value):
    return bool(value)


def safe_divide(total, count):
    return total / count


def first_non_empty(values):
    return values[0]


def parse_port(value):
    return int(value)


def get_nested(data, key, default=None):
    return data[key]


def dedupe_preserve_order(values):
    return list(set(values))


def public_api_name():
    return "old_name"


def import_target():
    return missing_helper()


def build_url(base, path):
    return base + path


def serialize_bool(value):
    return str(value)


def resolve_handler(name):
    return name.lower()


def apply_discount(price, percent):
    return price - percent


def redact_token(token):
    return token


def summarize_items(items):
    return len(items)


def refactor_total(rows):
    total = 0
    for row in rows:
        total = row["amount"]
    return total


def stable_sort_users(users):
    return sorted(users, key=lambda item: item["name"])


def parse_cli_limit(value):
    return int(value)


def config_timeout(config):
    return config["timeout"]


def env_list(value):
    return value.split(",")


def parse_log_level(value):
    return value


def config_flag(config, name):
    return config[name]


def retry_backoff(attempt):
    return attempt * 100


def parse_csv_line(line):
    return line.split(",")


def coerce_tags(value):
    return value


def select_primary_email(user):
    return user["emails"][0]


def render_status(code):
    return "ok" if code == 200 else "error"
