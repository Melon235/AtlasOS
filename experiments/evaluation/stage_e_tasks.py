"""Stage E1 paired tasks: one-worker A0 vs static-DAG multi-worker A1.

Each definition owns one visible fixture and one hidden oracle. A0 and A1 are derived from the
same definition, so both configurations see the same initial files, module boundaries, allowed
paths, and final acceptance tests. They differ only in the worker graph and assignment text.
"""

from __future__ import annotations

from dataclasses import dataclass

from experiments.evaluation.models import BenchmarkTask, WorkerStep

ENV = "Work in /workspace/project. Do not modify tests/ or files outside the allowed module paths."


@dataclass(frozen=True)
class StageEDefinition:
    sample_id: str
    family: str
    overall_prompt: str
    visible_files: dict[str, str]
    hidden_files: dict[str, str]
    allowed_paths: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    workers: tuple[WorkerStep, ...]
    max_turns: int = 14


def _pytest(body: str) -> str:
    return "from __future__ import annotations\n\n" + body


def _definitions() -> tuple[StageEDefinition, ...]:
    definitions: list[StageEDefinition] = []

    # 1. Multi-module configuration.
    definitions.append(
        StageEDefinition(
            sample_id="e1-01-config",
            family="multi_module",
            overall_prompt=(
                "Implement the three configuration modules in /workspace/project. "
                "load_config(path) reads JSON into a dict; merge_defaults(defaults, overrides) "
                "recursively merges dicts while replacing lists; validate_required(config, keys) "
                "returns the missing keys sorted. " + ENV
            ),
            visible_files={
                "config/__init__.py": "",
                "config/loader.py": _pytest(
                    "def load_config(path: str) -> dict:\n"
                    '    raise NotImplementedError("load_config")\n'
                ),
                "config/merge.py": _pytest(
                    "def merge_defaults(defaults: dict, overrides: dict) -> dict:\n"
                    '    raise NotImplementedError("merge_defaults")\n'
                ),
                "config/validate.py": _pytest(
                    "def validate_required(config: dict, required: list[str]) -> list[str]:\n"
                    '    raise NotImplementedError("validate_required")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_config.py": _pytest(
                    "import json\n\n"
                    "from config.loader import load_config\n"
                    "from config.merge import merge_defaults\n"
                    "from config.validate import validate_required\n\n\n"
                    "def test_load(tmp_path) -> None:\n"
                    "    path = tmp_path / 'c.json'\n"
                    "    path.write_text(json.dumps({'a': 1}))\n"
                    "    assert load_config(str(path)) == {'a': 1}\n\n\n"
                    "def test_merge() -> None:\n"
                    "    base = {'a': {'x': 1}, 'items': [1, 2]}\n"
                    "    merged = merge_defaults(base, {'a': {'y': 2}, 'items': [3]})\n"
                    "    assert merged == {'a': {'x': 1, 'y': 2}, 'items': [3]}\n"
                    "    assert base['a'] == {'x': 1}\n\n\n"
                    "def test_validate() -> None:\n"
                    "    assert validate_required({'b': 1}, ['a', 'b', 'c']) == ['a', 'c']\n"
                )
            },
            allowed_paths=("config/loader.py", "config/merge.py", "config/validate.py"),
            required_artifacts=("config/loader.py", "config/merge.py", "config/validate.py"),
            workers=(
                WorkerStep(
                    "loader", "e1-01-loader", "Implement config/loader.py load_config. " + ENV
                ),
                WorkerStep(
                    "merge", "e1-01-merge", "Implement config/merge.py merge_defaults. " + ENV
                ),
                WorkerStep(
                    "validate",
                    "e1-01-validate",
                    "Implement config/validate.py validate_required. " + ENV,
                ),
            ),
        )
    )

    # 2. Report pipeline.
    definitions.append(
        StageEDefinition(
            sample_id="e1-02-report",
            family="multi_module",
            overall_prompt=(
                "Implement the report modules. parse_lines(text) parses lines 'name=value' into "
                "(name, int) pairs, skipping blanks and raising ValueError for malformed lines. "
                "stats(values) returns mean/max/min. render(text) returns exactly "
                "'mean=<mean>;max=<max>;min=<min>'. " + ENV
            ),
            visible_files={
                "report/__init__.py": "",
                "report/parser.py": _pytest(
                    "def parse_lines(text: str) -> list[tuple[str, int]]:\n"
                    '    raise NotImplementedError("parse_lines")\n'
                ),
                "report/stats.py": _pytest(
                    "def stats(values: list[int]) -> dict:\n"
                    '    raise NotImplementedError("stats")\n'
                ),
                "report/render.py": _pytest(
                    'def render(text: str) -> str:\n    raise NotImplementedError("render")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_report.py": _pytest(
                    "import pytest\n\n"
                    "from report.parser import parse_lines\n"
                    "from report.stats import stats\n"
                    "from report.render import render\n\n\n"
                    "def test_parse() -> None:\n"
                    "    assert parse_lines('a=1\\n\\nb=2\\n') == [('a', 1), ('b', 2)]\n"
                    "    with pytest.raises(ValueError):\n"
                    "        parse_lines('bad')\n\n\n"
                    "def test_stats_and_render() -> None:\n"
                    "    assert stats([2, 4, 6]) == {'mean': 4.0, 'max': 6, 'min': 2}\n"
                    "    assert render('a=2\\nb=4\\nc=6\\n') == 'mean=4.0;max=6;min=2'\n"
                )
            },
            allowed_paths=("report/parser.py", "report/stats.py", "report/render.py"),
            required_artifacts=("report/parser.py", "report/stats.py", "report/render.py"),
            workers=(
                WorkerStep("parser", "e1-02-parser", "Implement report/parser.py. " + ENV),
                WorkerStep("stats", "e1-02-stats", "Implement report/stats.py. " + ENV),
                WorkerStep(
                    "render",
                    "e1-02-render",
                    "Implement report/render.py using parser and stats. " + ENV,
                ),
            ),
        )
    )

    # 3. RBAC.
    definitions.append(
        StageEDefinition(
            sample_id="e1-03-rbac",
            family="multi_module",
            overall_prompt=(
                "Implement rbac/roles.py, rbac/policy.py, and rbac/service.py. The roles are "
                "admin (read, write, delete), editor (read, write), viewer (read). can(role, "
                "action) returns a bool; authorize raises PermissionError for denied actions. "
                + ENV
            ),
            visible_files={
                "rbac/__init__.py": "",
                "rbac/roles.py": _pytest("ROLE_PERMISSIONS: dict[str, set[str]] = {}\n"),
                "rbac/policy.py": _pytest(
                    "def can(role: str, action: str) -> bool:\n"
                    '    raise NotImplementedError("can")\n'
                ),
                "rbac/service.py": _pytest(
                    "def authorize(role: str, action: str) -> None:\n"
                    '    raise NotImplementedError("authorize")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_rbac.py": _pytest(
                    "import pytest\n\n"
                    "from rbac.policy import can\n"
                    "from rbac.roles import ROLE_PERMISSIONS\n"
                    "from rbac.service import authorize\n\n\n"
                    "def test_roles() -> None:\n"
                    "    assert ROLE_PERMISSIONS['admin'] == {'read', 'write', 'delete'}\n"
                    "    assert ROLE_PERMISSIONS['editor'] == {'read', 'write'}\n"
                    "    assert ROLE_PERMISSIONS['viewer'] == {'read'}\n\n\n"
                    "def test_policy_and_service() -> None:\n"
                    "    assert can('editor', 'write') is True\n"
                    "    assert can('viewer', 'write') is False\n"
                    "    authorize('admin', 'delete')\n"
                    "    with pytest.raises(PermissionError):\n"
                    "        authorize('viewer', 'write')\n"
                )
            },
            allowed_paths=("rbac/roles.py", "rbac/policy.py", "rbac/service.py"),
            required_artifacts=("rbac/roles.py", "rbac/policy.py", "rbac/service.py"),
            workers=(
                WorkerStep(
                    "roles", "e1-03-roles", "Implement rbac/roles.py ROLE_PERMISSIONS. " + ENV
                ),
                WorkerStep("policy", "e1-03-policy", "Implement rbac/policy.py can. " + ENV),
                WorkerStep(
                    "service", "e1-03-service", "Implement rbac/service.py authorize. " + ENV
                ),
            ),
        )
    )

    # 4. CLI toolkit.
    definitions.append(
        StageEDefinition(
            sample_id="e1-04-cli",
            family="multi_module",
            overall_prompt=(
                "Implement the CLI modules. parse_args(argv) accepts --name VALUE and --count N "
                "(default 1) and returns {'name': str, 'count': int}. run(command, name, count) "
                "supports 'greet' returning a list of 'hello <name>' repeated count times, and "
                "raises ValueError for unknown commands or count <= 0. main(argv) prints each "
                "line and returns 0. " + ENV
            ),
            visible_files={
                "cli/__init__.py": "",
                "cli/args.py": _pytest(
                    "def parse_args(argv: list[str]) -> dict:\n"
                    '    raise NotImplementedError("parse_args")\n'
                ),
                "cli/commands.py": _pytest(
                    "def run(command: str, name: str, count: int) -> list[str]:\n"
                    '    raise NotImplementedError("run")\n'
                ),
                "cli/main.py": _pytest(
                    'def main(argv: list[str]) -> int:\n    raise NotImplementedError("main")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_cli.py": _pytest(
                    "import pytest\n\n"
                    "from cli.args import parse_args\n"
                    "from cli.commands import run\n\n\n"
                    "def test_parse_and_run() -> None:\n"
                    "    expected = {'name': 'Ada', 'count': 2}\n"
                    "    assert parse_args(['--name', 'Ada', '--count', '2']) == expected\n"
                    "    assert run('greet', 'Ada', 2) == ['hello Ada', 'hello Ada']\n"
                    "    with pytest.raises(ValueError):\n"
                    "        run('unknown', 'Ada', 1)\n"
                    "    with pytest.raises(ValueError):\n"
                    "        run('greet', 'Ada', 0)\n"
                )
            },
            allowed_paths=("cli/args.py", "cli/commands.py", "cli/main.py"),
            required_artifacts=("cli/args.py", "cli/commands.py", "cli/main.py"),
            workers=(
                WorkerStep("args", "e1-04-args", "Implement cli/args.py. " + ENV),
                WorkerStep("commands", "e1-04-commands", "Implement cli/commands.py. " + ENV),
                WorkerStep(
                    "main", "e1-04-main", "Implement cli/main.py using args and commands. " + ENV
                ),
            ),
        )
    )

    # 5. Parser + consumer integration.
    definitions.append(
        StageEDefinition(
            sample_id="e1-05-pairs",
            family="integration",
            overall_prompt=(
                "Implement pairs.py parse_items(text) for semicolon-separated 'key:value' pairs "
                "returning ordered (key, int) tuples, and summary.py total_by_key(text) summing "
                "values per key. " + ENV
            ),
            visible_files={
                "pairs.py": _pytest(
                    "def parse_items(text: str) -> list[tuple[str, int]]:\n"
                    '    raise NotImplementedError("parse_items")\n'
                ),
                "summary.py": _pytest(
                    "def total_by_key(text: str) -> dict[str, int]:\n"
                    '    raise NotImplementedError("total_by_key")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_pairs.py": _pytest(
                    "from pairs import parse_items\n"
                    "from summary import total_by_key\n\n\n"
                    "def test_parse() -> None:\n"
                    "    assert parse_items('a:1;b:2;a:3') == [('a', 1), ('b', 2), ('a', 3)]\n\n\n"
                    "def test_total() -> None:\n"
                    "    assert total_by_key('a:1;b:2;a:3') == {'a': 4, 'b': 2}\n"
                )
            },
            allowed_paths=("pairs.py", "summary.py"),
            required_artifacts=("pairs.py", "summary.py"),
            workers=(
                WorkerStep("parser", "e1-05-parser", "Implement pairs.py. " + ENV),
                WorkerStep(
                    "summary",
                    "e1-05-summary",
                    "Read pairs.py then implement summary.py using parse_items. " + ENV,
                    depends_on=("parser",),
                ),
            ),
        )
    )

    # 6. API + client integration.
    definitions.append(
        StageEDefinition(
            sample_id="e1-06-kv",
            family="integration",
            overall_prompt=(
                "Implement kv_api.py apply(state, command) for 'SET k v' and 'DEL k', and "
                "kv_client.py run_commands(commands) applying them in order to a dict and "
                "returning it. Unknown commands raise ValueError. " + ENV
            ),
            visible_files={
                "kv_api.py": _pytest(
                    "def apply(state: dict[str, int], command: str) -> None:\n"
                    '    raise NotImplementedError("apply")\n'
                ),
                "kv_client.py": _pytest(
                    "def run_commands(commands: list[str]) -> dict[str, int]:\n"
                    '    raise NotImplementedError("run_commands")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_kv.py": _pytest(
                    "import pytest\n\n"
                    "from kv_api import apply\n"
                    "from kv_client import run_commands\n\n\n"
                    "def test_api() -> None:\n"
                    "    state: dict[str, int] = {}\n"
                    "    apply(state, 'SET a 3')\n"
                    "    assert state == {'a': 3}\n"
                    "    apply(state, 'DEL a')\n"
                    "    assert state == {}\n"
                    "    with pytest.raises(ValueError):\n"
                    "        apply(state, 'BAD')\n\n\n"
                    "def test_client() -> None:\n"
                    "    assert run_commands(['SET a 1', 'SET b 2', 'DEL a']) == {'b': 2}\n"
                )
            },
            allowed_paths=("kv_api.py", "kv_client.py"),
            required_artifacts=("kv_api.py", "kv_client.py"),
            workers=(
                WorkerStep("api", "e1-06-api", "Implement kv_api.py. " + ENV),
                WorkerStep(
                    "client",
                    "e1-06-client",
                    "Read kv_api.py then implement kv_client.py using apply. " + ENV,
                    depends_on=("api",),
                ),
            ),
        )
    )

    # 7. Formatter + exporter.
    definitions.append(
        StageEDefinition(
            sample_id="e1-07-export",
            family="integration",
            overall_prompt=(
                "Implement formatter.py format_row(row, widths) producing space-padded columns "
                "joined by ' | ', and exporter.py export_rows(rows, widths) returning the rows "
                "joined by newline plus a trailing newline. " + ENV
            ),
            visible_files={
                "formatter.py": _pytest(
                    "def format_row(row: list[str], widths: list[int]) -> str:\n"
                    '    raise NotImplementedError("format_row")\n'
                ),
                "exporter.py": _pytest(
                    "def export_rows(rows: list[list[str]], widths: list[int]) -> str:\n"
                    '    raise NotImplementedError("export_rows")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_export.py": _pytest(
                    "from exporter import export_rows\n"
                    "from formatter import format_row\n\n\n"
                    "def test_format() -> None:\n"
                    "    assert format_row(['a', 'bb'], [2, 3]) == 'a  | bb '\n\n\n"
                    "def test_export() -> None:\n"
                    "    assert export_rows([['a', 'b']], [2, 2]) == 'a  | b \\n'\n"
                )
            },
            allowed_paths=("formatter.py", "exporter.py"),
            required_artifacts=("formatter.py", "exporter.py"),
            workers=(
                WorkerStep("formatter", "e1-07-formatter", "Implement formatter.py. " + ENV),
                WorkerStep(
                    "exporter",
                    "e1-07-exporter",
                    "Read formatter.py then implement exporter.py using format_row. " + ENV,
                    depends_on=("formatter",),
                ),
            ),
        )
    )

    # 8. Quantile / variance / summary split.
    definitions.append(
        StageEDefinition(
            sample_id="e1-08-stats",
            family="decompose_merge",
            overall_prompt=(
                "Implement quantile.py median(values), variance.py population_variance(values), "
                "and summary.py describe(values) returning 'median=<m>;variance=<v>'. Empty input "
                "raises ValueError. " + ENV
            ),
            visible_files={
                "quantile.py": _pytest(
                    "def median(values: list[float]) -> float:\n"
                    '    raise NotImplementedError("median")\n'
                ),
                "variance.py": _pytest(
                    "def population_variance(values: list[float]) -> float:\n"
                    '    raise NotImplementedError("population_variance")\n'
                ),
                "summary.py": _pytest(
                    "def describe(values: list[float]) -> str:\n"
                    '    raise NotImplementedError("describe")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_stats.py": _pytest(
                    "import pytest\n\n"
                    "from quantile import median\n"
                    "from summary import describe\n"
                    "from variance import population_variance\n\n\n"
                    "def test_components() -> None:\n"
                    "    assert median([1, 2, 3, 4]) == 2.5\n"
                    "    assert population_variance([1, 3]) == 1.0\n\n\n"
                    "def test_summary() -> None:\n"
                    "    assert describe([1, 3]) == 'median=2.0;variance=1.0'\n"
                    "    with pytest.raises(ValueError):\n"
                    "        describe([])\n"
                )
            },
            allowed_paths=("quantile.py", "variance.py", "summary.py"),
            required_artifacts=("quantile.py", "variance.py", "summary.py"),
            workers=(
                WorkerStep("median", "e1-08-median", "Implement quantile.py. " + ENV),
                WorkerStep("variance", "e1-08-variance", "Implement variance.py. " + ENV),
                WorkerStep(
                    "summary",
                    "e1-08-summary",
                    "Read quantile.py and variance.py then implement summary.py. " + ENV,
                    depends_on=("median", "variance"),
                ),
            ),
        )
    )

    # 9. ETL split.
    definitions.append(
        StageEDefinition(
            sample_id="e1-09-etl",
            family="decompose_merge",
            overall_prompt=(
                "Implement extract.py rows(text) for lines 'name,value', transform.py "
                "keep_positive(rows) and double(rows), and load.py summarize(text) returning "
                "'count=<n>;total=<sum>' for positive doubled values. " + ENV
            ),
            visible_files={
                "extract.py": _pytest(
                    "def rows(text: str) -> list[tuple[str, int]]:\n"
                    '    raise NotImplementedError("rows")\n'
                ),
                "transform.py": _pytest(
                    "def keep_positive(rows: list[tuple[str, int]]) -> list[tuple[str, int]]:\n"
                    '    raise NotImplementedError("keep_positive")\n\n\n'
                    "def double(rows: list[tuple[str, int]]) -> list[tuple[str, int]]:\n"
                    '    raise NotImplementedError("double")\n'
                ),
                "load.py": _pytest(
                    'def summarize(text: str) -> str:\n    raise NotImplementedError("summarize")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_etl.py": _pytest(
                    "from extract import rows\n"
                    "from load import summarize\n"
                    "from transform import double, keep_positive\n\n\n"
                    "def test_stages() -> None:\n"
                    "    parsed = rows('a,1\\nb,-2\\nc,3\\n')\n"
                    "    assert parsed == [('a', 1), ('b', -2), ('c', 3)]\n"
                    "    assert keep_positive(parsed) == [('a', 1), ('c', 3)]\n"
                    "    assert double([('a', 1)]) == [('a', 2)]\n\n\n"
                    "def test_summary() -> None:\n"
                    "    assert summarize('a,1\\nb,-2\\nc,3\\n') == 'count=2;total=8'\n"
                )
            },
            allowed_paths=("extract.py", "transform.py", "load.py"),
            required_artifacts=("extract.py", "transform.py", "load.py"),
            workers=(
                WorkerStep("extract", "e1-09-extract", "Implement extract.py. " + ENV),
                WorkerStep("transform", "e1-09-transform", "Implement transform.py. " + ENV),
                WorkerStep(
                    "load",
                    "e1-09-load",
                    "Read extract.py and transform.py then implement load.py. " + ENV,
                    depends_on=("extract", "transform"),
                ),
            ),
        )
    )

    # 10. Search split.
    definitions.append(
        StageEDefinition(
            sample_id="e1-10-search",
            family="decompose_merge",
            overall_prompt=(
                "Implement index.py build(docs) returning a word->sorted doc-id list map, query.py "
                "lookup(index, word), and rank.py ranked(index, word) returning matching doc ids. "
                + ENV
            ),
            visible_files={
                "index.py": _pytest(
                    "def build(docs: dict[str, str]) -> dict[str, list[str]]:\n"
                    '    raise NotImplementedError("build")\n'
                ),
                "query.py": _pytest(
                    "def lookup(index: dict[str, list[str]], word: str) -> list[str]:\n"
                    '    raise NotImplementedError("lookup")\n'
                ),
                "rank.py": _pytest(
                    "def ranked(index: dict[str, list[str]], word: str) -> list[str]:\n"
                    '    raise NotImplementedError("ranked")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_search.py": _pytest(
                    "from index import build\n"
                    "from query import lookup\n"
                    "from rank import ranked\n\n\n"
                    "def test_search() -> None:\n"
                    "    index = build({'b': 'alpha beta', 'a': 'alpha'})\n"
                    "    assert index == {'alpha': ['a', 'b'], 'beta': ['b']}\n"
                    "    assert lookup(index, 'alpha') == ['a', 'b']\n"
                    "    assert ranked(index, 'missing') == []\n"
                )
            },
            allowed_paths=("index.py", "query.py", "rank.py"),
            required_artifacts=("index.py", "query.py", "rank.py"),
            workers=(
                WorkerStep("index", "e1-10-index", "Implement index.py. " + ENV),
                WorkerStep("query", "e1-10-query", "Implement query.py. " + ENV),
                WorkerStep(
                    "rank",
                    "e1-10-rank",
                    "Read index.py and query.py then implement rank.py. " + ENV,
                    depends_on=("index", "query"),
                ),
            ),
        )
    )

    # 11. Missing input / fallback.
    definitions.append(
        StageEDefinition(
            sample_id="e1-11-fallback",
            family="dependency_error",
            overall_prompt=(
                "Implement metadata.py write_metadata(path, data) and read_metadata(path) "
                "returning {} when the file is missing, and consumer.py describe_metadata(path) "
                "returning "
                "'missing' for absent metadata or 'present:<name>' for {'name': ...}. " + ENV
            ),
            visible_files={
                "metadata.py": _pytest(
                    "def write_metadata(path: str, data: dict) -> None:\n"
                    '    raise NotImplementedError("write_metadata")\n\n\n'
                    "def read_metadata(path: str) -> dict:\n"
                    '    raise NotImplementedError("read_metadata")\n'
                ),
                "consumer.py": _pytest(
                    "def describe_metadata(path: str) -> str:\n"
                    '    raise NotImplementedError("describe_metadata")\n'
                ),
            },
            hidden_files={
                "tests/test_hidden_fallback.py": _pytest(
                    "from consumer import describe_metadata\n"
                    "from metadata import read_metadata, write_metadata\n\n\n"
                    "def test_missing(tmp_path) -> None:\n"
                    "    path = str(tmp_path / 'meta.json')\n"
                    "    assert read_metadata(path) == {}\n"
                    "    assert describe_metadata(path) == 'missing'\n\n\n"
                    "def test_present(tmp_path) -> None:\n"
                    "    path = str(tmp_path / 'meta.json')\n"
                    "    write_metadata(path, {'name': 'ops'})\n"
                    "    assert describe_metadata(path) == 'present:ops'\n"
                )
            },
            allowed_paths=("metadata.py", "consumer.py"),
            required_artifacts=("metadata.py", "consumer.py"),
            workers=(
                WorkerStep("metadata", "e1-11-metadata", "Implement metadata.py. " + ENV),
                WorkerStep(
                    "consumer",
                    "e1-11-consumer",
                    "Read metadata.py then implement consumer.py. " + ENV,
                    depends_on=("metadata",),
                ),
            ),
        )
    )

    # 12. Concurrent state conflict / merge from dependency outputs.
    definitions.append(
        StageEDefinition(
            sample_id="e1-12-conflict",
            family="dependency_error",
            overall_prompt=(
                "Write /workspace/project/state.json with both keys {'a': 1, 'b': 2} and then "
                "write /workspace/project/final.json with the same object. Do not modify tests/. "
                "Use any safe method, including reconstructing from upstream worker results."
            ),
            visible_files={
                "README.md": "# Concurrent state handoff\n",
            },
            hidden_files={
                "tests/test_hidden_conflict.py": _pytest(
                    "import json\n"
                    "from pathlib import Path\n\n\n"
                    "def test_final_state() -> None:\n"
                    "    assert json.loads(Path('final.json').read_text()) == {'a': 1, 'b': 2}\n"
                    "    assert json.loads(Path('state.json').read_text()) == {'a': 1, 'b': 2}\n"
                )
            },
            allowed_paths=("state.json", "final.json"),
            required_artifacts=("state.json", "final.json"),
            workers=(
                WorkerStep(
                    "a",
                    "e1-12-a",
                    'Write /workspace/project/state.json with exactly {"a": 1} and then state '
                    'in your final answer: A_JSON={"a": 1}. Do not create final.json.',
                ),
                WorkerStep(
                    "b",
                    "e1-12-b",
                    'Write /workspace/project/state.json with exactly {"b": 2} and then state '
                    'in your final answer: B_JSON={"b": 2}. Do not create final.json.',
                ),
                WorkerStep(
                    "merge",
                    "e1-12-merge",
                    "Read state.json and the dependency worker outputs, merge the a and b keys, "
                    "and write /workspace/project/final.json and state.json with exactly "
                    '{"a": 1, "b": 2}.',
                    depends_on=("a", "b"),
                ),
            ),
        )
    )

    return tuple(definitions)


DEFINITIONS: tuple[StageEDefinition, ...] = _definitions()


def variants(definition: StageEDefinition) -> tuple[BenchmarkTask, BenchmarkTask]:
    """Return (A0 single-worker, A1 multi-worker) with identical fixtures and oracle."""

    a0 = BenchmarkTask(
        sample_id=f"{definition.sample_id}-a0",
        category="multi_worker",
        mode="coding",
        prompt=definition.overall_prompt,
        target="hidden oracle passes",
        visible_files=definition.visible_files,
        hidden_files=definition.hidden_files,
        workers=(
            WorkerStep(
                task_id="solo",
                title=f"{definition.sample_id}-solo",
                prompt=definition.overall_prompt,
            ),
        ),
        allowed_paths=definition.allowed_paths,
        forbidden_paths=("tests/**",),
        required_artifacts=definition.required_artifacts,
        max_turns=definition.max_turns,
        tags=("stage_e", definition.family, "A0"),
    )
    a1 = BenchmarkTask(
        sample_id=f"{definition.sample_id}-a1",
        category="multi_worker",
        mode="multi_worker",
        prompt=definition.overall_prompt,
        target="hidden oracle passes",
        visible_files=definition.visible_files,
        hidden_files=definition.hidden_files,
        workers=definition.workers,
        allowed_paths=definition.allowed_paths,
        forbidden_paths=("tests/**",),
        required_artifacts=definition.required_artifacts,
        tool_expectations={"distinct_workers": len(definition.workers)},
        max_turns=definition.max_turns,
        tags=("stage_e", definition.family, "A1"),
    )
    return a0, a1


def all_variant_tasks() -> tuple[BenchmarkTask, ...]:
    tasks: list[BenchmarkTask] = []
    for definition in DEFINITIONS:
        tasks.extend(variants(definition))
    return tuple(tasks)


def definition_by_id(sample_id: str) -> StageEDefinition:
    base = sample_id.rsplit("-a", 1)[0]
    for definition in DEFINITIONS:
        if definition.sample_id == base:
            return definition
    raise KeyError(sample_id)
