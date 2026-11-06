"""Formal AtlasOS Agent Benchmark task definitions (dataset version 1.0.0).

Every coding task carries visible fixture files and a hidden oracle test suite. The hidden files
never enter the sandbox. Tool tasks additionally carry an MCP discovery contract; multi-worker
tasks carry an explicit two-node DAG.
"""

from __future__ import annotations

from experiments.evaluation.models import BenchmarkTask, WorkerStep


def _single(
    *,
    sample_id: str,
    category: str,
    mode: str,
    prompt: str,
    visible_files: dict[str, str],
    hidden_files: dict[str, str],
    allowed_paths: tuple[str, ...],
    forbidden_paths: tuple[str, ...] = ("tests/**", "TASK.md"),
    required_artifacts: tuple[str, ...] = (),
    tool_expectations: dict[str, object] | None = None,
    mcp_server: str | None = None,
    max_turns: int = 14,
    tags: tuple[str, ...] = (),
    target: str = "hidden oracle passes",
) -> BenchmarkTask:
    return BenchmarkTask(
        sample_id=sample_id,
        category=category,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        prompt=prompt,
        target=target,
        visible_files=visible_files,
        hidden_files=hidden_files,
        workers=(WorkerStep(task_id="work", title=f"{sample_id}-work", prompt=prompt),),
        allowed_paths=allowed_paths,
        forbidden_paths=forbidden_paths,
        required_artifacts=required_artifacts,
        tool_expectations=tool_expectations or {},
        mcp_server=mcp_server,
        max_turns=max_turns,
        tags=tags,
    )


def _public_test(body: str) -> str:
    return "from __future__ import annotations\n\n" + body


def _bugfix_slugify() -> BenchmarkTask:
    visible = {
        "slugify.py": (
            "from __future__ import annotations\n\n"
            "import re\n\n\n"
            "def slugify(text: str) -> str:\n"
            '    """Convert text to a lowercase hyphen-separated slug."""\n\n'
            '    return re.sub(r"[^a-z0-9]", "-", text.lower()).strip("-")\n'
        ),
        "tests/test_public.py": _public_test(
            "from slugify import slugify\n\n\n"
            "def test_simple_words() -> None:\n"
            '    assert slugify("Hello World") == "hello-world"\n'
        ),
    }
    hidden = {
        "tests/test_hidden_slugify.py": _public_test(
            "from slugify import slugify\n\n\n"
            "def test_collapses_character_runs() -> None:\n"
            '    assert slugify("Hello   World") == "hello-world"\n'
            '    assert slugify("A -- B") == "a-b"\n'
            '    assert slugify("  spaced  ") == "spaced"\n\n\n'
            "def test_keeps_digits() -> None:\n"
            '    assert slugify("Release 2.0") == "release-2-0"\n'
        ),
    }
    return _single(
        sample_id="bugfix-001-slugify",
        category="bugfix",
        mode="coding",
        prompt=(
            "TASK.md is intentionally absent. The `slugify` function in slugify.py collapses a "
            "single non-alphanumeric character but leaves runs of separators as multiple hyphens. "
            "Fix it so every run of non-alphanumeric characters becomes exactly one hyphen, with "
            "no leading or trailing hyphen. Keep digits. Do not modify tests/. Run the public "
            "tests after your change."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("slugify.py",),
        tags=("bugfix", "string"),
    )


def _bugfix_median() -> BenchmarkTask:
    visible = {
        "stats_tools.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Sequence\n\n\n"
            "def median(values: Sequence[float]) -> float:\n"
            '    """Return the arithmetic median of a non-empty sequence."""\n\n'
            "    if not values:\n"
            '        raise ValueError("median of empty sequence")\n'
            "    ordered = sorted(values)\n"
            "    return float(ordered[len(ordered) // 2])\n"
        ),
        "tests/test_public.py": _public_test(
            "from stats_tools import median\n\n\n"
            "def test_odd_length() -> None:\n"
            "    assert median([3, 1, 2]) == 2.0\n"
        ),
    }
    hidden = {
        "tests/test_hidden_median.py": _public_test(
            "import pytest\n\nfrom stats_tools import median\n\n\n"
            "def test_even_length_averages_middle() -> None:\n"
            "    assert median([1, 2, 3, 4]) == 2.5\n"
            "    assert median([10, 2, 8, 4]) == 6.0\n\n\n"
            "def test_single_and_negative() -> None:\n"
            "    assert median([7]) == 7.0\n"
            "    assert median([-5, -1, -3, -2]) == -2.5\n\n\n"
            "def test_empty_rejected() -> None:\n"
            "    with pytest.raises(ValueError):\n"
            "        median([])\n"
        ),
    }
    return _single(
        sample_id="bugfix-002-median",
        category="bugfix",
        mode="coding",
        prompt=(
            "The `median` function in stats_tools.py returns the upper middle item for even-sized "
            "inputs. It must return the arithmetic mean of the two middle values for even length, "
            "while keeping the odd-length and empty-input behavior. Do not modify tests/. Run the "
            "public tests after your change."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("stats_tools.py",),
        tags=("bugfix", "math"),
    )


def _bugfix_csv() -> BenchmarkTask:
    visible = {
        "csv_parser.py": (
            "from __future__ import annotations\n\n\n"
            "def parse_csv_line(line: str) -> list[str]:\n"
            '    """Parse one CSV record into fields."""\n\n'
            '    return line.rstrip("\\n").split(",")\n'
        ),
        "tests/test_public.py": _public_test(
            "from csv_parser import parse_csv_line\n\n\n"
            "def test_plain_fields() -> None:\n"
            '    assert parse_csv_line("a,b,c\\n") == ["a", "b", "c"]\n'
        ),
    }
    hidden = {
        "tests/test_hidden_csv.py": _public_test(
            "from csv_parser import parse_csv_line\n\n\n"
            "def test_quoted_comma() -> None:\n"
            '    assert parse_csv_line(\'a,"b,c",d\\n\') == ["a", "b,c", "d"]\n\n\n'
            "def test_escaped_quote() -> None:\n"
            '    assert parse_csv_line(\'"a""b",c\\n\') == [\'a"b\', "c"]\n\n\n'
            "def test_empty_fields() -> None:\n"
            '    assert parse_csv_line(",,\\n") == ["", "", ""]\n'
        ),
    }
    return _single(
        sample_id="bugfix-003-csv-quotes",
        category="bugfix",
        mode="coding",
        prompt=(
            "csv_parser.py parses records with a naive comma split. Fix `parse_csv_line` so quoted "
            "fields may contain commas, doubled quotes inside a quoted field represent one literal "
            "quote, and empty fields are preserved. The input includes a trailing newline. Do not "
            "modify tests/. Run the public tests after your change."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("csv_parser.py",),
        tags=("bugfix", "parsing"),
    )


def _feature_todo() -> BenchmarkTask:
    visible = {
        "todo/__init__.py": ('from todo.store import TaskStore\n\n__all__ = ["TaskStore"]\n'),
        "todo/models.py": (
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class Task:\n"
            "    task_id: int\n"
            "    title: str\n"
            "    completed: bool = False\n"
        ),
        "todo/store.py": (
            "from __future__ import annotations\n\n"
            "from todo.models import Task\n\n\n"
            "class TaskStore:\n"
            '    """In-memory ordered task store."""\n\n'
            "    def __init__(self) -> None:\n"
            "        self._tasks: list[Task] = []\n"
            "        self._next_id = 1\n\n"
            "    def add(self, title: str) -> Task:\n"
            '        raise NotImplementedError("add")\n\n'
            "    def complete(self, task_id: int) -> Task:\n"
            '        raise NotImplementedError("complete")\n\n'
            "    def pending(self) -> list[Task]:\n"
            '        raise NotImplementedError("pending")\n\n'
            "    def all(self) -> list[Task]:\n"
            '        raise NotImplementedError("all")\n'
        ),
        "tests/test_public.py": _public_test(
            "from todo import TaskStore\n\n\n"
            "def test_add_roundtrip() -> None:\n"
            "    store = TaskStore()\n"
            '    task = store.add("write report")\n'
            "    assert task.task_id == 1\n"
            '    assert store.all()[0].title == "write report"\n'
        ),
    }
    hidden = {
        "tests/test_hidden_todo.py": _public_test(
            "import pytest\n\nfrom todo import TaskStore\nfrom todo.models import Task\n\n\n"
            "def test_add_assigns_increasing_ids_and_preserves_order() -> None:\n"
            "    store = TaskStore()\n"
            '    first = store.add("one")\n'
            '    second = store.add("two")\n'
            "    assert (first.task_id, second.task_id) == (1, 2)\n"
            '    assert [task.title for task in store.all()] == ["one", "two"]\n'
            "    assert all(task.completed is False for task in store.all())\n\n\n"
            "def test_complete_and_pending() -> None:\n"
            "    store = TaskStore()\n"
            '    first = store.add("one")\n'
            '    store.add("two")\n'
            "    done = store.complete(first.task_id)\n"
            "    assert isinstance(done, Task)\n"
            "    assert done.completed is True\n"
            '    assert [task.title for task in store.pending()] == ["two"]\n\n\n'
            "def test_complete_unknown_id_raises_keyerror() -> None:\n"
            "    store = TaskStore()\n"
            "    with pytest.raises(KeyError):\n"
            "        store.complete(999)\n"
        ),
    }
    return _single(
        sample_id="feature-001-todo-store",
        category="feature",
        mode="coding",
        prompt=(
            "Implement the in-memory TaskStore in todo/store.py. `add` assigns increasing integer "
            "ids and appends tasks in insertion order. `complete` marks the matching task as "
            "completed and returns it, raising KeyError for an unknown id. `pending` returns tasks "
            "that are not completed, preserving insertion order. `all` returns every task in "
            "insertion order. Do not modify todo/models.py or tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("todo/store.py",),
        tags=("feature", "multi-file"),
    )


def _tool_fare() -> BenchmarkTask:
    visible = {
        "README.md": (
            "# Fare lookup task\n\n"
            "Use the MCP tool catalog; do not guess the fare. Write the exact tool answer into "
            "answer.txt.\n"
        ),
    }
    hidden = {
        "tests/test_hidden_fare.py": _public_test(
            "from pathlib import Path\n\n\n"
            "def test_answer_file() -> None:\n"
            '    assert Path("answer.txt").read_text(encoding="utf-8").strip() == "fare_usd=412"\n'
        ),
    }
    return _single(
        sample_id="tool-001-fare",
        category="tool_discovery",
        mode="tool_discovery",
        prompt=(
            "A read-only MCP tool can look up benchmark fares. Discover it through the tool "
            "catalog (search first), call the fare tool for Reykjavik in economy cabin, and write "
            "exactly the returned `fare_usd=<number>` token to /workspace/project/answer.txt. "
            "Do not write anything else in the file. Then verify the file content."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("answer.txt",),
        required_artifacts=("answer.txt",),
        tool_expectations={
            "require_tool_search": True,
            "required_dispatch_tool_ids": ["mcp_evaltools_lookup_fare"],
            "output_contains": "fare_usd=412",
        },
        mcp_server="evaltools",
        tags=("tool", "discovery"),
    )


def _bugfix_retry() -> BenchmarkTask:
    visible = {
        "retry.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Callable, Sequence\n\n\n"
            "def retry(operation: Callable[[], int], delays: Sequence[float]) -> int:\n"
            '    """Retry operation once per delay; return the first successful result."""\n\n'
            "    last_error: Exception | None = None\n"
            "    for delay in delays:\n"
            "        del delay\n"
            "        try:\n"
            "            return operation()\n"
            "        except Exception as error:\n"
            "            last_error = error\n"
            "    raise RuntimeError('retry attempts exhausted') from last_error\n"
        ),
        "tests/test_public.py": _public_test(
            "from retry import retry\n\n\n"
            "def test_succeeds_immediately() -> None:\n"
            "    assert retry(lambda: 7, [0.1, 0.2]) == 7\n"
        ),
    }
    hidden = {
        "tests/test_hidden_retry.py": _public_test(
            "import pytest\n\nfrom retry import retry\n\n\n"
            "def test_retries_exactly_len_delays_times() -> None:\n"
            "    attempts = []\n\n"
            "    def operation() -> int:\n"
            "        attempts.append(1)\n"
            '        raise ValueError("no")\n\n'
            "    with pytest.raises(RuntimeError):\n"
            "        retry(operation, [0.1, 0.2, 0.3])\n"
            "    assert len(attempts) == 3\n\n\n"
            "def test_stops_after_first_success() -> None:\n"
            "    attempts = []\n\n"
            "    def operation() -> int:\n"
            "        attempts.append(1)\n"
            "        if len(attempts) == 2:\n"
            "            return 9\n"
            '        raise ValueError("no")\n\n'
            "    assert retry(operation, [0.1, 0.2, 0.3]) == 9\n"
            "    assert len(attempts) == 2\n\n\n"
            "def test_empty_delays_rejected() -> None:\n"
            "    with pytest.raises(ValueError):\n"
            "        retry(lambda: 1, [])\n\n\n"
            "def test_last_exception_is_cause() -> None:\n"
            "    cause = ValueError('last')\n\n"
            "    def operation() -> int:\n"
            "        raise cause\n\n"
            "    with pytest.raises(RuntimeError) as raised:\n"
            "        retry(operation, [0.1, 0.2])\n"
            "    assert raised.value.__cause__ is cause\n"
        ),
    }
    return _single(
        sample_id="bugfix-004-retry",
        category="bugfix",
        mode="coding",
        prompt=(
            "retry.py currently makes one attempt per delay, which is the intended contract. The "
            "bug is that the final exception chain is lost when delays is empty and the function "
            "does not reject an empty delay list explicitly. Make `retry` raise ValueError for "
            "an empty delays sequence, and preserve the last exception as the cause for "
            "non-empty exhausted retries. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("retry.py",),
        tags=("bugfix", "error-handling"),
    )


def _bugfix_date_range() -> BenchmarkTask:
    visible = {
        "date_range.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Iterator\n"
            "from datetime import date, timedelta\n\n\n"
            "def date_range(start: date, end: date) -> Iterator[date]:\n"
            '    """Yield dates from start up to but excluding end."""\n\n'
            "    current = start\n"
            "    while current < end:\n"
            "        yield current\n"
            "        current += timedelta(days=1)\n"
        ),
        "tests/test_public.py": _public_test(
            "from datetime import date\n\nfrom date_range import date_range\n\n\n"
            "def test_inclusive_two_days() -> None:\n"
            "    assert list(date_range(date(2024, 1, 1), date(2024, 1, 3))) == [\n"
            "        date(2024, 1, 1),\n"
            "        date(2024, 1, 2),\n"
            "        date(2024, 1, 3),\n"
            "    ]\n"
        ),
    }
    hidden = {
        "tests/test_hidden_date_range.py": _public_test(
            "from datetime import date, timedelta\n\n"
            "import pytest\n\n"
            "from date_range import date_range\n\n\n"
            "def test_end_is_inclusive_by_default() -> None:\n"
            "    days = list(date_range(date(2024, 1, 1), date(2024, 1, 3)))\n"
            "    assert days == [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)]\n\n\n"
            "def test_single_day_and_reverse_range() -> None:\n"
            "    assert list(date_range(date(2024, 2, 1), date(2024, 2, 1))) == [\n"
            "        date(2024, 2, 1)\n"
            "    ]\n"
            "    assert list(date_range(date(2024, 2, 2), date(2024, 2, 1))) == []\n\n\n"
            "def test_custom_step() -> None:\n"
            "    days = list(date_range(date(2024, 1, 1), date(2024, 1, 7), step_days=2))\n"
            "    assert days == [\n"
            "        date(2024, 1, 1), date(2024, 1, 3), date(2024, 1, 5), date(2024, 1, 7)\n"
            "    ]\n"
        ),
    }
    return _single(
        sample_id="bugfix-005-date-range",
        category="bugfix",
        mode="coding",
        prompt=(
            "date_range.py omits the end date and has no step support. Change the contract to "
            "include the end date, yield a single day when start == end, yield nothing when start "
            "> end, and accept an optional `step_days` keyword (default 1) that advances the "
            "cursor by that many days. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("date_range.py",),
        tags=("bugfix", "datetime"),
    )


def _bugfix_intervals() -> BenchmarkTask:
    visible = {
        "intervals.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Iterable\n\n\n"
            "def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:\n"
            '    """Merge overlapping half-open intervals."""\n\n'
            "    ordered = sorted(intervals)\n"
            "    merged: list[tuple[int, int]] = []\n"
            "    for start, end in ordered:\n"
            "        if merged and start < merged[-1][1]:\n"
            "            previous_start, previous_end = merged[-1]\n"
            "            merged[-1] = (previous_start, max(previous_end, end))\n"
            "        else:\n"
            "            merged.append((start, end))\n"
            "    return merged\n"
        ),
        "tests/test_public.py": _public_test(
            "from intervals import merge_intervals\n\n\n"
            "def test_overlap() -> None:\n"
            "    assert merge_intervals([(1, 4), (3, 6)]) == [(1, 6)]\n"
        ),
    }
    hidden = {
        "tests/test_hidden_intervals.py": _public_test(
            "from intervals import merge_intervals\n\n\n"
            "def test_touching_closed_ranges_merge() -> None:\n"
            "    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]\n\n\n"
            "def test_nested_and_unsorted() -> None:\n"
            "    assert merge_intervals([(5, 7), (1, 10), (2, 3)]) == [(1, 10)]\n\n\n"
            "def test_disjoint_are_preserved() -> None:\n"
            "    assert merge_intervals([(1, 2), (4, 5), (2, 3)]) == [(1, 3), (4, 5)]\n"
        ),
    }
    return _single(
        sample_id="bugfix-006-intervals",
        category="bugfix",
        mode="coding",
        prompt=(
            "merge_intervals treats intervals as half-open and does not merge touching ranges. "
            "The required contract is inclusive integer intervals: (1,3) and (3,5) merge to "
            "(1,5), nested ranges are absorbed, and disjoint ranges are preserved in ascending "
            "order. Empty input returns an empty list. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("intervals.py",),
        tags=("bugfix", "algorithms"),
    )


def _bugfix_toposort() -> BenchmarkTask:
    visible = {
        "toposort.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Mapping\n\n\n"
            "def topological_order(graph: Mapping[str, list[str]]) -> list[str]:\n"
            '    """Return a deterministic topological order of graph nodes."""\n\n'
            "    indegree = {node: 0 for node in graph}\n"
            "    for node, neighbours in graph.items():\n"
            "        for neighbour in neighbours:\n"
            "            indegree.setdefault(neighbour, 0)\n"
            "            indegree[neighbour] += 1\n"
            "    ready = sorted(node for node, count in indegree.items() if count == 0)\n"
            "    order: list[str] = []\n"
            "    while ready:\n"
            "        node = ready.pop(0)\n"
            "        order.append(node)\n"
            "        for neighbour in sorted(graph.get(node, [])):\n"
            "            indegree[neighbour] -= 1\n"
            "            if indegree[neighbour] == 0:\n"
            "                ready.append(neighbour)\n"
            "        ready.sort()\n"
            "    return order\n"
        ),
        "tests/test_public.py": _public_test(
            "from toposort import topological_order\n\n\n"
            "def test_chain() -> None:\n"
            '    assert topological_order({"a": ["b"], "b": ["c"], "c": []}) == ["a", "b", "c"]\n'
        ),
    }
    hidden = {
        "tests/test_hidden_toposort.py": _public_test(
            "import pytest\n\nfrom toposort import topological_order\n\n\n"
            "def test_deterministic_tie_break() -> None:\n"
            "    order = topological_order({'b': [], 'a': [], 'c': ['a', 'b']})\n"
            "    assert order == ['a', 'b', 'c']\n\n\n"
            "def test_cycle_is_rejected() -> None:\n"
            "    with pytest.raises(ValueError):\n"
            "        topological_order({'a': ['b'], 'b': ['a']})\n\n\n"
            "def test_nodes_only_in_edges_are_included() -> None:\n"
            "    order = topological_order({'a': ['b']})\n"
            "    assert order == ['a', 'b']\n"
        ),
    }
    return _single(
        sample_id="bugfix-007-toposort",
        category="bugfix",
        mode="coding",
        prompt=(
            "topological_order silently returns a partial order when the graph contains a cycle. "
            "Make it raise ValueError if any node remains after processing, keep deterministic "
            "lexicographic tie-breaking, and include nodes that only appear as edge targets. Do "
            "not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("toposort.py",),
        tags=("bugfix", "algorithms"),
    )


def _bugfix_roman() -> BenchmarkTask:
    visible = {
        "roman.py": (
            "from __future__ import annotations\n\n"
            "VALUES = {\n"
            "    'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000,\n"
            "}\n\n\n"
            "def roman_to_int(value: str) -> int:\n"
            '    """Convert an uppercase Roman numeral to an integer."""\n\n'
            "    total = 0\n"
            "    for character in value:\n"
            "        total += VALUES[character]\n"
            "    return total\n"
        ),
        "tests/test_public.py": _public_test(
            "from roman import roman_to_int\n\n\n"
            "def test_additive_numeral() -> None:\n"
            '    assert roman_to_int("XVI") == 16\n'
        ),
    }
    hidden = {
        "tests/test_hidden_roman.py": _public_test(
            "import pytest\n\nfrom roman import roman_to_int\n\n\n"
            "def test_subtractive_pairs() -> None:\n"
            "    assert roman_to_int('IV') == 4\n"
            "    assert roman_to_int('MCMXCIV') == 1994\n"
            "    assert roman_to_int('XLII') == 42\n\n\n"
            "def test_empty_and_invalid() -> None:\n"
            "    assert roman_to_int('') == 0\n"
            "    with pytest.raises(ValueError):\n"
            "        roman_to_int('ABC')\n"
        ),
    }
    return _single(
        sample_id="bugfix-008-roman",
        category="bugfix",
        mode="coding",
        prompt=(
            "roman_to_int sums every symbol and ignores subtractive notation. Fix it so IV=4, "
            "MCMXCIV=1994, and XLII=42. An empty string returns 0. Unknown characters raise "
            "ValueError. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("roman.py",),
        tags=("bugfix", "parsing"),
    )


def _bugfix_pagination() -> BenchmarkTask:
    visible = {
        "pagination.py": (
            "from __future__ import annotations\n\n\n"
            "def page_count(total_items: int, page_size: int) -> int:\n"
            '    """Return the number of pages needed for total_items."""\n\n'
            "    if page_size <= 0:\n"
            '        raise ValueError("page_size must be positive")\n'
            "    return total_items // page_size\n"
        ),
        "tests/test_public.py": _public_test(
            "from pagination import page_count\n\n\n"
            "def test_exact_multiple() -> None:\n"
            "    assert page_count(10, 5) == 2\n"
        ),
    }
    hidden = {
        "tests/test_hidden_pagination.py": _public_test(
            "import pytest\n\nfrom pagination import page_count\n\n\n"
            "def test_partial_last_page() -> None:\n"
            "    assert page_count(11, 5) == 3\n"
            "    assert page_count(1, 10) == 1\n\n\n"
            "def test_empty_and_invalid() -> None:\n"
            "    assert page_count(0, 10) == 0\n"
            "    with pytest.raises(ValueError):\n"
            "        page_count(5, 0)\n"
        ),
    }
    return _single(
        sample_id="bugfix-009-pagination",
        category="bugfix",
        mode="coding",
        prompt=(
            "page_count truncates the last partial page. Fix it to use ceiling division while "
            "preserving page_count(0, n) == 0 and the ValueError for non-positive page_size. Do "
            "not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("pagination.py",),
        tags=("bugfix", "arithmetic"),
    )


def _bugfix_merge() -> BenchmarkTask:
    visible = {
        "merge.py": (
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n\n"
            "def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:\n"
            '    """Recursively merge override into a copy of base."""\n\n'
            "    result = dict(base)\n"
            "    for key, value in override.items():\n"
            "        if isinstance(value, dict) and isinstance(result.get(key), dict):\n"
            "            result[key] = deep_merge(result[key], value)\n"
            "        else:\n"
            "            result[key] = value\n"
            "    return result\n"
        ),
        "tests/test_public.py": _public_test(
            "from merge import deep_merge\n\n\n"
            "def test_nested_override() -> None:\n"
            "    assert deep_merge({'a': {'x': 1}}, {'a': {'y': 2}}) == {'a': {'x': 1, 'y': 2}}\n"
        ),
    }
    hidden = {
        "tests/test_hidden_merge.py": _public_test(
            "from merge import deep_merge\n\n\n"
            "def test_lists_are_replaced_not_mutated() -> None:\n"
            "    base = {'a': [1, 2]}\n"
            "    override = {'a': [3]}\n"
            "    merged = deep_merge(base, override)\n"
            "    assert merged == {'a': [3]}\n"
            "    assert base == {'a': [1, 2]}\n"
            "    assert override == {'a': [3]}\n\n\n"
            "def test_none_deletes_nested_key() -> None:\n"
            "    merged = deep_merge({'a': {'x': 1, 'y': 2}}, {'a': {'x': None}})\n"
            "    assert merged == {'a': {'y': 2}}\n\n\n"
            "def test_nested_dicts_are_copied() -> None:\n"
            "    base = {'a': {'x': 1}}\n"
            "    merged = deep_merge(base, {'a': {'y': 2}})\n"
            "    merged['a']['x'] = 99\n"
            "    assert base == {'a': {'x': 1}}\n"
        ),
    }
    return _single(
        sample_id="bugfix-010-deep-merge",
        category="bugfix",
        mode="coding",
        prompt=(
            "deep_merge currently overwrites lists correctly, but it treats a None override value "
            "as a normal value and it can share nested dictionaries with the input. Required: "
            "lists are replaced (not concatenated), None deletes the key from a nested dict, "
            "nested dicts in the result are independent copies, and neither input is mutated. Do "
            "not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("merge.py",),
        tags=("bugfix", "data-structures"),
    )


def _feature_inventory() -> BenchmarkTask:
    visible = {
        "inventory/__init__.py": (
            'from inventory.service import Inventory\n\n__all__ = ["Inventory"]\n'
        ),
        "inventory/models.py": (
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass\n\n\n"
            "@dataclass\n"
            "class StockItem:\n"
            "    sku: str\n"
            "    quantity: int\n"
        ),
        "inventory/service.py": (
            "from __future__ import annotations\n\n"
            "from inventory.models import StockItem\n\n\n"
            "class Inventory:\n"
            '    """In-memory stock service."""\n\n'
            "    def __init__(self) -> None:\n"
            "        self._items: dict[str, StockItem] = {}\n\n"
            "    def add(self, sku: str, quantity: int) -> int:\n"
            '        raise NotImplementedError("add")\n\n'
            "    def reserve(self, sku: str, quantity: int) -> int:\n"
            '        raise NotImplementedError("reserve")\n\n'
            "    def available(self, sku: str) -> int:\n"
            '        raise NotImplementedError("available")\n'
        ),
        "tests/test_public.py": _public_test(
            "from inventory import Inventory\n\n\n"
            "def test_add_returns_total() -> None:\n"
            "    inventory = Inventory()\n"
            "    assert inventory.add('sku-1', 4) == 4\n"
            "    assert inventory.available('sku-1') == 4\n"
        ),
    }
    hidden = {
        "tests/test_hidden_inventory.py": _public_test(
            "import pytest\n\nfrom inventory import Inventory\n\n\n"
            "def test_add_accumulates() -> None:\n"
            "    inventory = Inventory()\n"
            "    assert inventory.add('a', 2) == 2\n"
            "    assert inventory.add('a', 3) == 5\n\n\n"
            "def test_reserve_reduces_and_rejects() -> None:\n"
            "    inventory = Inventory()\n"
            "    inventory.add('a', 5)\n"
            "    assert inventory.reserve('a', 3) == 2\n"
            "    with pytest.raises(ValueError):\n"
            "        inventory.reserve('a', 3)\n"
            "    with pytest.raises(KeyError):\n"
            "        inventory.reserve('missing', 1)\n"
            "    assert inventory.available('a') == 2\n\n\n"
            "def test_negative_quantity_rejected() -> None:\n"
            "    inventory = Inventory()\n"
            "    with pytest.raises(ValueError):\n"
            "        inventory.add('a', -1)\n"
        ),
    }
    return _single(
        sample_id="feature-002-inventory",
        category="feature",
        mode="coding",
        prompt=(
            "Implement Inventory in inventory/service.py. `add` rejects negative quantities and "
            "returns the new total. `reserve` reduces stock and returns the remaining amount, "
            "raises KeyError for an unknown sku and ValueError for a negative or insufficient "
            "reservation. `available` returns 0 for an unknown sku. Do not modify models.py or "
            "tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("inventory/service.py",),
        tags=("feature", "multi-file"),
    )


def _feature_diff() -> BenchmarkTask:
    visible = {
        "textdiff.py": (
            "from __future__ import annotations\n\n"
            "from collections.abc import Sequence\n\n\n"
            "def unified_diff_lines(\n"
            "    old: Sequence[str],\n"
            "    new: Sequence[str],\n"
            "    *,\n"
            "    context: int = 1,\n"
            ") -> list[str]:\n"
            '    """Return a minimal unified-style line diff without trailing newline '
            'markers."""\n\n'
            '    raise NotImplementedError("unified_diff_lines")\n'
        ),
        "tests/test_public.py": _public_test(
            "from textdiff import unified_diff_lines\n\n\n"
            "def test_identical() -> None:\n"
            "    assert unified_diff_lines(['a', 'b'], ['a', 'b']) == []\n"
        ),
    }
    hidden = {
        "tests/test_hidden_diff.py": _public_test(
            "from textdiff import unified_diff_lines\n\n\n"
            "def test_replacement() -> None:\n"
            "    assert unified_diff_lines(['a', 'b', 'c'], ['a', 'x', 'c']) == [\n"
            "        ' a', '-b', '+x', ' c'\n"
            "    ]\n\n\n"
            "def test_insert_and_delete() -> None:\n"
            "    assert unified_diff_lines(['a', 'c'], ['a', 'b', 'c']) == [' a', '+b', ' c']\n"
            "    assert unified_diff_lines(['a', 'b', 'c'], ['a', 'c']) == [' a', '-b', ' c']\n\n\n"
            "def test_context_zero() -> None:\n"
            "    assert unified_diff_lines(['a', 'b'], ['a', 'c'], context=0) == ['-b', '+c']\n"
        ),
    }
    return _single(
        sample_id="feature-003-line-diff",
        category="feature",
        mode="coding",
        prompt=(
            "Implement unified_diff_lines in textdiff.py. It returns a list of prefixed lines: "
            "unchanged context lines start with a space, removals with '-', additions with '+'. "
            "Include at most `context` unchanged lines around each change, preserve order, and "
            "return [] for identical inputs. Implement a bounded dynamic-programming LCS; do not "
            "call an external diff command. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("textdiff.py",),
        tags=("feature", "algorithms"),
    )


def _feature_validator() -> BenchmarkTask:
    visible = {
        "validator/__init__.py": (
            'from validator.engine import validate\n\n__all__ = ["validate"]\n'
        ),
        "validator/rules.py": (
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n\n"
            "TYPE_CHECKS = {\n"
            "    'str': str,\n"
            "    'int': int,\n"
            "    'bool': bool,\n"
            "    'float': (int, float),\n"
            "}\n\n\n"
            "def check_type(value: Any, type_name: str) -> bool:\n"
            "    expected = TYPE_CHECKS.get(type_name)\n"
            "    if expected is None:\n"
            "        raise ValueError(f'unknown schema type: {type_name}')\n"
            "    return isinstance(value, expected)\n"
        ),
        "validator/engine.py": (
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n"
            "from validator.rules import check_type\n\n\n"
            "def validate(\n"
            "    payload: dict[str, Any], schema: dict[str, dict[str, Any]]\n"
            ") -> list[str]:\n"
            '    """Return a sorted list of human-readable validation errors."""\n\n'
            '    raise NotImplementedError("validate")\n'
        ),
        "tests/test_public.py": _public_test(
            "from validator import validate\n\n\n"
            "def test_empty_schema_is_valid() -> None:\n"
            "    assert validate({'x': 1}, {}) == []\n"
        ),
    }
    hidden = {
        "tests/test_hidden_validator.py": _public_test(
            "import pytest\n\nfrom validator import validate\n\n\n"
            "def test_required_and_type() -> None:\n"
            "    schema = {'name': {'type': 'str', 'required': True}, 'age': {'type': 'int'}}\n"
            "    assert validate({'name': 'Ada', 'age': 36}, schema) == []\n"
            "    errors = validate({'age': 'old'}, schema)\n"
            "    assert 'name is required' in errors\n"
            "    assert 'age must be int' in errors\n\n\n"
            "def test_bounds_and_unknown_type() -> None:\n"
            "    schema = {'score': {'type': 'int', 'min': 1, 'max': 5}}\n"
            "    assert validate({'score': 0}, schema) == ['score must be >= 1']\n"
            "    assert validate({'score': 9}, schema) == ['score must be <= 5']\n"
            "    with pytest.raises(ValueError):\n"
            "        validate({'x': 1}, {'x': {'type': 'uuid'}})\n"
        ),
    }
    return _single(
        sample_id="feature-004-validator",
        category="feature",
        mode="coding",
        prompt=(
            "Implement validate() in validator/engine.py using validator/rules.py. Supported "
            "keys per field: `type` (str, int, bool, float), `required` (bool, default False), "
            "`min`, and `max`. Return sorted error strings: '<field> is required', "
            "'<field> must be <type>', '<field> must be >= <min>', '<field> must be <= <max>'. "
            "Only check bounds when the value has the right type. Unknown type names raise "
            "ValueError. Do not modify tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("validator/engine.py",),
        tags=("feature", "multi-file"),
    )


def _feature_bank() -> BenchmarkTask:
    visible = {
        "bank/__init__.py": 'from bank.service import Bank\n\n__all__ = ["Bank"]\n',
        "bank/account.py": (
            "from __future__ import annotations\n\n"
            "from dataclasses import dataclass\n"
            "from decimal import Decimal\n\n\n"
            "@dataclass\n"
            "class Account:\n"
            "    account_id: str\n"
            "    balance: Decimal = Decimal('0')\n"
        ),
        "bank/service.py": (
            "from __future__ import annotations\n\n"
            "from decimal import Decimal\n\n"
            "from bank.account import Account\n\n\n"
            "class Bank:\n"
            '    """Minimal in-memory bank with exact Decimal balances."""\n\n'
            "    def __init__(self) -> None:\n"
            "        self._accounts: dict[str, Account] = {}\n\n"
            "    def open_account(self, account_id: str) -> Account:\n"
            '        raise NotImplementedError("open_account")\n\n'
            "    def deposit(self, account_id: str, amount: Decimal) -> Decimal:\n"
            '        raise NotImplementedError("deposit")\n\n'
            "    def withdraw(self, account_id: str, amount: Decimal) -> Decimal:\n"
            '        raise NotImplementedError("withdraw")\n\n'
            "    def transfer(\n"
            "        self, source: str, target: str, amount: Decimal\n"
            "    ) -> tuple[Decimal, Decimal]:\n"
            '        raise NotImplementedError("transfer")\n'
        ),
        "tests/test_public.py": _public_test(
            "from decimal import Decimal\n\nfrom bank import Bank\n\n\n"
            "def test_open_and_deposit() -> None:\n"
            "    bank = Bank()\n"
            "    bank.open_account('a')\n"
            "    assert bank.deposit('a', Decimal('10.50')) == Decimal('10.50')\n"
        ),
    }
    hidden = {
        "tests/test_hidden_bank.py": _public_test(
            "from decimal import Decimal\n\nimport pytest\n\nfrom bank import Bank\n\n\n"
            "def test_deposit_withdraw_and_transfer() -> None:\n"
            "    bank = Bank()\n"
            "    bank.open_account('a')\n"
            "    bank.open_account('b')\n"
            "    bank.deposit('a', Decimal('100.00'))\n"
            "    assert bank.withdraw('a', Decimal('25.25')) == Decimal('74.75')\n"
            "    assert bank.transfer('a', 'b', Decimal('50.00')) == (\n"
            "        Decimal('24.75'), Decimal('50.00')\n"
            "    )\n\n\n"
            "def test_errors() -> None:\n"
            "    bank = Bank()\n"
            "    bank.open_account('a')\n"
            "    with pytest.raises(ValueError):\n"
            "        bank.open_account('a')\n"
            "    with pytest.raises(ValueError):\n"
            "        bank.deposit('a', Decimal('0'))\n"
            "    with pytest.raises(KeyError):\n"
            "        bank.deposit('missing', Decimal('1'))\n"
            "    with pytest.raises(ValueError):\n"
            "        bank.withdraw('a', Decimal('1'))\n"
        ),
    }
    return _single(
        sample_id="feature-005-bank",
        category="feature",
        mode="coding",
        prompt=(
            "Implement Bank in bank/service.py with exact Decimal arithmetic. open_account rejects "
            "duplicate ids with ValueError. deposit rejects non-positive amounts and unknown "
            "accounts (ValueError/KeyError as appropriate) and returns the new balance. withdraw "
            "rejects insufficient funds and returns the new balance. transfer moves funds "
            "atomically and returns (source_balance, target_balance), rejecting unknown accounts "
            "and insufficient funds. Do not modify account.py or tests/. Run the public tests."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("bank/service.py",),
        tags=("feature", "multi-file"),
    )


def _tool_answer_task(
    *,
    sample_id: str,
    prompt: str,
    expected_answer: str,
    tool_id: str,
    output_contains: str,
    tags: tuple[str, ...],
) -> BenchmarkTask:
    visible = {
        "README.md": (
            "# Tool answer task\n\n"
            "Discover the MCP tool through the catalog, call it with the requested arguments, and "
            "write only the required token into answer.txt.\n"
        ),
    }
    hidden = {
        "tests/test_hidden_answer.py": _public_test(
            "from pathlib import Path\n\n\n"
            "def test_answer_file() -> None:\n"
            "    assert Path('answer.txt').read_text(encoding='utf-8').strip() == "
            + repr(expected_answer)
            + "\n"
        ),
    }
    return _single(
        sample_id=sample_id,
        category="tool_discovery",
        mode="tool_discovery",
        prompt=prompt,
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("answer.txt",),
        required_artifacts=("answer.txt",),
        tool_expectations={
            "require_tool_search": True,
            "required_dispatch_tool_ids": [tool_id],
            "output_contains": output_contains,
        },
        mcp_server="evaltools",
        tags=tags,
    )


def _tool_clearance() -> BenchmarkTask:
    return _tool_answer_task(
        sample_id="tool-002-clearance",
        prompt=(
            "Discover the read-only employee-clearance MCP tool through the catalog. Look up "
            "employee E-207 and write exactly the `clearance=<level>` token returned by the tool "
            "into /workspace/project/answer.txt. Do not guess."
        ),
        expected_answer="clearance=L3",
        tool_id="mcp_evaltools_employee_clearance",
        output_contains="clearance=L3",
        tags=("tool", "discovery"),
    )


def _tool_currency() -> BenchmarkTask:
    return _tool_answer_task(
        sample_id="tool-003-currency",
        prompt=(
            "Discover the read-only currency MCP tool. Convert 250 USD to EUR and write exactly "
            "the returned `converted=<amount>` token into /workspace/project/answer.txt."
        ),
        expected_answer="converted=230.0",
        tool_id="mcp_evaltools_convert_currency",
        output_contains="converted=230.0",
        tags=("tool", "discovery"),
    )


def _tool_shipping() -> BenchmarkTask:
    return _tool_answer_task(
        sample_id="tool-004-shipping",
        prompt=(
            "Discover the read-only shipping MCP tool. Quote a 2.5 kg shipment to the EU zone and "
            "write exactly the returned `quote_cents=<integer>` token into "
            "/workspace/project/answer.txt."
        ),
        expected_answer="quote_cents=1212",
        tool_id="mcp_evaltools_shipping_quote",
        output_contains="quote_cents=1212",
        tags=("tool", "discovery"),
    )


def _tool_policy() -> BenchmarkTask:
    return _tool_answer_task(
        sample_id="tool-005-retention",
        prompt=(
            "Discover the read-only retention-policy MCP tool. Look up `retention-audit` and "
            "write exactly the returned `value=<value>` token into "
            "/workspace/project/answer.txt."
        ),
        expected_answer="value=14 months",
        tool_id="mcp_evaltools_policy_lookup",
        output_contains="value=14 months",
        tags=("tool", "discovery"),
    )


def _multiworker_handoff() -> BenchmarkTask:
    visible = {
        "README.md": (
            "# Two-worker handoff\n\nWorker alpha produces part_a.py; worker beta consumes it.\n"
        ),
    }
    hidden = {
        "tests/test_hidden_handoff.py": _public_test(
            "from part_a import normalize_name\nfrom part_b import greeting\n\n\n"
            "def test_normalize_name() -> None:\n"
            "    assert normalize_name('  Ada  Lovelace ') == 'ada-lovelace'\n"
            "    assert normalize_name('Grace_Hopper') == 'grace-hopper'\n\n\n"
            "def test_greeting_uses_worker_artifact() -> None:\n"
            "    assert greeting('  Ada  Lovelace ') == 'hello ada-lovelace'\n"
        ),
    }
    alpha_prompt = (
        "Create /workspace/project/part_a.py with `normalize_name(value: str) -> str`. It must "
        "strip surrounding whitespace, lowercase the text, treat underscores and any run of "
        "spaces as a single hyphen, and collapse repeated hyphens. Do not create part_b.py."
    )
    beta_prompt = (
        "Worker alpha has already delivered part_a.py in /workspace/project. Read it, then create "
        "/workspace/project/part_b.py with `greeting(name: str) -> str` that imports "
        "`normalize_name` from part_a and returns `hello <normalized>`. Do not modify part_a.py "
        "or tests/."
    )
    return BenchmarkTask(
        sample_id="multiworker-001-handoff",
        category="multi_worker",
        mode="multi_worker",
        prompt="Two workers must deliver a dependency-ordered artifact handoff.",
        target="both worker artifacts pass the hidden handoff oracle",
        visible_files=visible,
        hidden_files=hidden,
        workers=(
            WorkerStep(task_id="alpha", title="multiworker-001-alpha", prompt=alpha_prompt),
            WorkerStep(
                task_id="beta",
                title="multiworker-001-beta",
                prompt=beta_prompt,
                depends_on=("alpha",),
            ),
        ),
        allowed_paths=("part_a.py", "part_b.py"),
        forbidden_paths=("tests/**", "README.md"),
        required_artifacts=("part_a.py", "part_b.py"),
        tool_expectations={"distinct_workers": 2},
        max_turns=12,
        tags=("multi_worker", "handoff"),
    )


def _multiworker_stats() -> BenchmarkTask:
    visible = {"README.md": "# Three-worker statistics report\n"}
    hidden = {
        "tests/test_hidden_stats.py": _public_test(
            "from mean import mean\nfrom median import median\nfrom report import summary\n\n\n"
            "def test_components() -> None:\n"
            "    assert mean([2, 4, 6]) == 4.0\n"
            "    assert median([1, 2, 3, 4]) == 2.5\n\n\n"
            "def test_report_combines_worker_artifacts() -> None:\n"
            "    assert summary([1, 2, 3, 4]) == 'mean=2.5;median=2.5'\n"
        ),
    }
    return BenchmarkTask(
        sample_id="multiworker-002-stats-report",
        category="multi_worker",
        mode="multi_worker",
        prompt="Three workers produce two independent statistics and one combined report.",
        target="all worker artifacts and the combined report satisfy the hidden oracle",
        visible_files=visible,
        hidden_files=hidden,
        workers=(
            WorkerStep(
                task_id="mean",
                title="multiworker-002-mean",
                prompt=(
                    "Create /workspace/project/mean.py with `mean(values)` returning the "
                    "arithmetic mean as float; empty input raises ValueError."
                ),
            ),
            WorkerStep(
                task_id="median",
                title="multiworker-002-median",
                prompt=(
                    "Create /workspace/project/median.py with `median(values)` returning the "
                    "arithmetic median as float (even length averages the middle two); empty input "
                    "raises ValueError."
                ),
            ),
            WorkerStep(
                task_id="report",
                title="multiworker-002-report",
                prompt=(
                    "Read mean.py and median.py from /workspace/project, then create report.py "
                    "with `summary(values)` returning exactly `mean=<mean>;median=<median>` using "
                    "those modules. Do not modify the other workers' files."
                ),
                depends_on=("mean", "median"),
            ),
        ),
        allowed_paths=("mean.py", "median.py", "report.py"),
        forbidden_paths=("README.md",),
        required_artifacts=("mean.py", "median.py", "report.py"),
        tool_expectations={"distinct_workers": 3},
        max_turns=14,
        tags=("multi_worker", "dag"),
    )


def _multiworker_pipeline() -> BenchmarkTask:
    visible = {"README.md": "# Two-stage parsing pipeline\n"}
    hidden = {
        "tests/test_hidden_pipeline.py": _public_test(
            "from parse import parse_pairs\nfrom aggregate import total_by_key\n\n\n"
            "def test_parse_pairs() -> None:\n"
            "    assert parse_pairs('a=1,b=2,a=3') == [('a', 1), ('b', 2), ('a', 3)]\n\n\n"
            "def test_aggregate_uses_parser() -> None:\n"
            "    assert total_by_key('a=1,b=2,a=3') == {'a': 4, 'b': 2}\n"
        ),
    }
    return BenchmarkTask(
        sample_id="multiworker-003-pipeline",
        category="multi_worker",
        mode="multi_worker",
        prompt="A parser worker hands a concrete data contract to an aggregation worker.",
        target="parser and aggregator artifacts satisfy the hidden pipeline oracle",
        visible_files=visible,
        hidden_files=hidden,
        workers=(
            WorkerStep(
                task_id="parser",
                title="multiworker-003-parser",
                prompt=(
                    "Create /workspace/project/parse.py with `parse_pairs(text)` that parses "
                    "comma-separated `key=value` pairs into a list of (key, int(value)) tuples, "
                    "preserving order. Repeated keys are valid and must each appear as a separate "
                    "tuple; do not reject them. Malformed input raises ValueError."
                ),
            ),
            WorkerStep(
                task_id="aggregator",
                title="multiworker-003-aggregator",
                prompt=(
                    "Read /workspace/project/parse.py, then create aggregate.py with "
                    "`total_by_key(text)` that calls `parse_pairs` and sums values per key. Do not "
                    "duplicate parsing or modify parse.py."
                ),
                depends_on=("parser",),
            ),
        ),
        allowed_paths=("parse.py", "aggregate.py"),
        forbidden_paths=("README.md",),
        required_artifacts=("parse.py", "aggregate.py"),
        tool_expectations={"distinct_workers": 2},
        max_turns=12,
        tags=("multi_worker", "handoff"),
    )


def _multiworker_interface() -> BenchmarkTask:
    visible = {"README.md": "# Worker interface contract\n"}
    hidden = {
        "tests/test_hidden_interface.py": _public_test(
            "from api import parse_command\nfrom client import run_command\n\n\n"
            "def test_api_contract() -> None:\n"
            "    assert parse_command('SET x 3') == ('set', 'x', 3)\n"
            "    assert parse_command('DEL x') == ('del', 'x', None)\n\n\n"
            "def test_client_uses_api() -> None:\n"
            "    state = {}\n"
            "    assert run_command(state, 'SET x 3') == 'ok'\n"
            "    assert state == {'x': 3}\n"
            "    assert run_command(state, 'DEL x') == 'ok'\n"
            "    assert state == {}\n"
        ),
    }
    return BenchmarkTask(
        sample_id="multiworker-004-interface",
        category="multi_worker",
        mode="multi_worker",
        prompt="One worker owns a parser contract; another consumes it behind a state API.",
        target="both workers honor the exact interface in the hidden oracle",
        visible_files=visible,
        hidden_files=hidden,
        workers=(
            WorkerStep(
                task_id="api",
                title="multiworker-004-api",
                prompt=(
                    "Create /workspace/project/api.py with `parse_command(text)` returning a "
                    "tuple: 'SET <key> <int>' -> ('set', key, int); 'DEL <key>' -> ('del', key, "
                    "None). Unknown commands raise ValueError."
                ),
            ),
            WorkerStep(
                task_id="client",
                title="multiworker-004-client",
                prompt=(
                    "Read /workspace/project/api.py, then create client.py with "
                    "`run_command(state, text)` that uses `parse_command`, mutates the dict for "
                    "SET "
                    "and DEL, and returns 'ok'. Do not duplicate command parsing."
                ),
                depends_on=("api",),
            ),
        ),
        allowed_paths=("api.py", "client.py"),
        forbidden_paths=("README.md",),
        required_artifacts=("api.py", "client.py"),
        tool_expectations={"distinct_workers": 2},
        max_turns=12,
        tags=("multi_worker", "interface"),
    )


def _multiworker_review() -> BenchmarkTask:
    visible = {
        "calc.py": (
            "from __future__ import annotations\n\n\n"
            "def apply_discount(price: float, percent: float) -> float:\n"
            '    """Apply a percentage discount; percent is 0..100."""\n\n'
            "    return price - percent\n"
        ),
        "README.md": "# Fix and independent verification\n",
    }
    hidden = {
        "tests/test_hidden_review.py": _public_test(
            "from calc import apply_discount\nfrom verify import check_discount\n\n\n"
            "def test_fix() -> None:\n"
            "    assert apply_discount(100.0, 25.0) == 75.0\n"
            "    assert apply_discount(80.0, 0.0) == 80.0\n\n\n"
            "def test_verifier_checks_contract() -> None:\n"
            "    assert check_discount() is True\n"
        ),
    }
    return BenchmarkTask(
        sample_id="multiworker-005-review",
        category="multi_worker",
        mode="multi_worker",
        prompt="A fixer repairs a calculation; an independent verifier encodes the contract.",
        target="fixed calculation and verifier artifact pass the hidden oracle",
        visible_files=visible,
        hidden_files=hidden,
        workers=(
            WorkerStep(
                task_id="fixer",
                title="multiworker-005-fixer",
                prompt=(
                    "Fix apply_discount in /workspace/project/calc.py so a percent discount of 25 "
                    "on 100 returns 75.0 and 0 percent leaves the price unchanged. Do not modify "
                    "README.md or hidden tests."
                ),
            ),
            WorkerStep(
                task_id="verifier",
                title="multiworker-005-verifier",
                prompt=(
                    "Read the fixed /workspace/project/calc.py, then create verify.py with "
                    "`check_discount() -> bool` that imports apply_discount and returns True only "
                    "if apply_discount(100.0, 25.0) == 75.0 and apply_discount(80.0, 0.0) == 80.0. "
                    "Do not modify calc.py."
                ),
                depends_on=("fixer",),
            ),
        ),
        allowed_paths=("calc.py", "verify.py"),
        forbidden_paths=("README.md",),
        required_artifacts=("calc.py", "verify.py"),
        tool_expectations={"distinct_workers": 2},
        max_turns=12,
        tags=("multi_worker", "review"),
    )


def _distractor_docs(count: int, topic: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for index in range(count):
        files[f"docs/archive/note-{index:02d}.md"] = (
            f"# {topic} archive note {index:02d}\n\n"
            "This document is retained for historical context. It is not the authoritative "
            "specification for the current change. Older proposals used different defaults, "
            "different numeric limits, and different ordering rules; do not apply them.\n\n"
            "## Background\n\n"
            f"The {topic} working group reviewed candidate {index} and recorded options A, B, "
            "and C. Option A favored latency, option B favored determinism, and option C was "
            "withdrawn after review. None of these options supersede the current specification.\n\n"
            "## Non-normative examples\n\n"
            "- example input: alpha\n"
            "- example output: beta\n"
            f"- archive id: {index:02d}\n\n"
            "## Status\n\n"
            "Archived. The current implementation must follow the current specification only.\n"
        )
    return files


def _long_buried_constant() -> BenchmarkTask:
    visible = {
        "docs/spec.md": (
            "# Retry configuration specification\n\n"
            "This is the authoritative specification.\n\n"
            "## Normative values\n\n"
            "- `RETRY_LIMIT = 7`\n"
            "- `BACKOFF_MS = 250`\n\n"
            "`build_config()` must return a dict with exactly the keys `retry_limit` and "
            "`backoff_ms`, using those integer values. Do not include any other keys.\n\n"
            "## Rationale\n\n"
            + ("The values were selected after a long review of provider behavior. " * 120)
        ),
        "config.py": (
            "from __future__ import annotations\n\n\n"
            "def build_config() -> dict[str, int]:\n"
            '    """Build the retry configuration from the authoritative specification."""\n\n'
            '    raise NotImplementedError("build_config")\n'
        ),
        **_distractor_docs(18, "retry"),
    }
    hidden = {
        "tests/test_hidden_constant.py": _public_test(
            "from config import build_config\n\n\n"
            "def test_authoritative_values() -> None:\n"
            "    assert build_config() == {'retry_limit': 7, 'backoff_ms': 250}\n"
        ),
    }
    return _single(
        sample_id="longcontext-001-buried-constant",
        category="long_context",
        mode="long_context",
        prompt=(
            "The repository contains many archived notes. Only docs/spec.md is authoritative for "
            "this change. Read it, find the normative retry values, and implement build_config in "
            "config.py. Do not use values from docs/archive. Do not modify tests/. Run the public "
            "tests if present."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("config.py",),
        max_turns=10,
        tags=("long_context", "information_integrity"),
    )


def _long_audit_rules() -> BenchmarkTask:
    visible = {
        "AUDIT_POLICY.md": (
            "# Payment audit policy (authoritative)\n\n"
            "Classify a payment with exactly one of `allow`, `review`, or `block`.\n\n"
            "Rules, highest priority first:\n\n"
            "1. If `flagged` is true -> `review`.\n"
            "2. If `country` is `IR` or `KP` -> `block`.\n"
            "3. If `amount_cents` > 500000 -> `block`.\n"
            "4. If `amount_cents` > 100000 -> `review`.\n"
            "5. Otherwise -> `allow`.\n\n"
            "`amount_cents` is an integer. The function is `classify(amount_cents, country, "
            "flagged)` in audit.py.\n"
        ),
        "audit.py": (
            "from __future__ import annotations\n\n\n"
            "def classify(amount_cents: int, country: str, flagged: bool) -> str:\n"
            '    """Classify a payment according to AUDIT_POLICY.md."""\n\n'
            '    raise NotImplementedError("classify")\n'
        ),
        **_distractor_docs(16, "audit"),
    }
    hidden = {
        "tests/test_hidden_audit.py": _public_test(
            "from audit import classify\n\n\n"
            "def test_priority_order() -> None:\n"
            "    assert classify(10, 'US', False) == 'allow'\n"
            "    assert classify(100001, 'US', False) == 'review'\n"
            "    assert classify(500001, 'US', False) == 'block'\n"
            "    assert classify(10, 'IR', False) == 'block'\n"
            "    assert classify(500001, 'IR', True) == 'review'\n"
        ),
    }
    return _single(
        sample_id="longcontext-002-audit-policy",
        category="long_context",
        mode="long_context",
        prompt=(
            "AUDIT_POLICY.md is the authoritative rule set. Implement classify() in audit.py with "
            "the exact priority order. Ignore the archived notes. Do not modify tests/ or the "
            "policy document."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("audit.py",),
        forbidden_paths=("tests/**", "AUDIT_POLICY.md"),
        max_turns=10,
        tags=("long_context", "rules"),
    )


def _long_many_modules() -> BenchmarkTask:
    modules: dict[str, str] = {}
    values = {f"m{index:02d}": (index * 7 + 3) for index in range(25)}
    for name, value in values.items():
        modules[f"modules/{name}.py"] = (
            f"from __future__ import annotations\n\n\ndef value() -> int:\n    return {value}\n"
        )
    expected = values["m03"] + values["m11"] + values["m22"]
    visible = {
        "docs/module_map.md": (
            "# Module map\n\n"
            "The active pipeline uses exactly these module value providers, in this order:\n\n"
            "- modules/m03.py\n- modules/m11.py\n- modules/m22.py\n\n"
            "All other modules are retained for historical pipelines and must not be used.\n"
        ),
        "modules/__init__.py": "",
        "pipeline.py": (
            "from __future__ import annotations\n\n\n"
            "def pipeline_total() -> int:\n"
            '    """Return the sum of the active module values."""\n\n'
            '    raise NotImplementedError("pipeline_total")\n'
        ),
        **_distractor_docs(14, "module"),
    }
    visible.update(modules)
    hidden = {
        "tests/test_hidden_modules.py": _public_test(
            "from pipeline import pipeline_total\n\n\n"
            "def test_active_modules_only() -> None:\n"
            f"    assert pipeline_total() == {expected}\n"
        ),
    }
    return _single(
        sample_id="longcontext-003-module-map",
        category="long_context",
        mode="long_context",
        prompt=(
            "docs/module_map.md identifies the three active value-provider modules among many "
            "historical modules. Implement pipeline_total() in pipeline.py to import and sum "
            "exactly those three values. Do not use other modules. Do not modify tests/."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("pipeline.py",),
        max_turns=10,
        tags=("long_context", "selection"),
    )


def _long_changelog() -> BenchmarkTask:
    visible = {
        "CHANGELOG.md": (
            "# Changelog\n\n"
            "## [2.4.0] - 2026-09-01\n- Added the audit filter.\n\n"
            "## [2.3.1] - 2026-08-15\n- Fixed a retry leak.\n\n"
            "## [2.0.0] - 2026-06-01\n- Changed the public config schema.\n\n"
            "## [1.9.0] - 2026-04-10\n- Legacy release.\n\n"
            "## [1.8.2] - 2026-03-02\n- Legacy release.\n\n"
            "## [0.9.0] - 2025-12-01\n- Initial preview.\n\n"
            "## Release rules\n\n"
            "`supported_versions()` returns every version >= 2.0.0 in changelog order, without "
            "the leading `v`. `latest_version()` returns the first such version. "
            "`count_legacy()` returns the number of versions < 2.0.0.\n"
        ),
        "release_notes.py": (
            "from __future__ import annotations\n\n\n"
            "def supported_versions() -> list[str]:\n"
            '    """Return supported versions in changelog order."""\n\n'
            '    raise NotImplementedError("supported_versions")\n\n\n'
            "def latest_version() -> str:\n"
            '    """Return the latest supported version."""\n\n'
            '    raise NotImplementedError("latest_version")\n\n\n'
            "def count_legacy() -> int:\n"
            '    """Return the number of legacy (<2.0.0) releases."""\n\n'
            '    raise NotImplementedError("count_legacy")\n'
        ),
        **_distractor_docs(15, "release"),
    }
    hidden = {
        "tests/test_hidden_changelog.py": _public_test(
            "from release_notes import count_legacy, latest_version, supported_versions\n\n\n"
            "def test_supported_and_latest() -> None:\n"
            "    assert supported_versions() == ['2.4.0', '2.3.1', '2.0.0']\n"
            "    assert latest_version() == '2.4.0'\n\n\n"
            "def test_legacy_count() -> None:\n"
            "    assert count_legacy() == 3\n"
        ),
    }
    return _single(
        sample_id="longcontext-004-changelog",
        category="long_context",
        mode="long_context",
        prompt=(
            "CHANGELOG.md is authoritative. Implement the three functions in release_notes.py "
            "using only entries from that changelog and the stated release rules. Do not modify "
            "the changelog or tests/."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("release_notes.py",),
        forbidden_paths=("tests/**", "CHANGELOG.md"),
        max_turns=10,
        tags=("long_context", "information_integrity"),
    )


def _long_requirements() -> BenchmarkTask:
    visible = {
        "REQUIREMENTS.md": (
            "# Planner requirements (authoritative)\n\n"
            "Implement `build_plan()` in planner.py. It returns a dict with exactly:\n\n"
            "- `batch_size`: 12\n"
            "- `retry_limit`: 4\n"
            "- `regions`: the list `['eu-west', 'us-east', 'ap-south']` in that order\n"
            "- `features`: a dict with `audit=True`, `cache=False`, `metrics=True`\n\n"
            "Additional constraints:\n"
            "1. Do not include keys other than the four above.\n"
            "2. The returned list and dict must be fresh objects on every call.\n"
            "3. `batch_size` and `retry_limit` must be integers.\n"
            + ("Background: previous proposals used batch sizes 4, 8, and 16. " * 90)
        ),
        "planner.py": (
            "from __future__ import annotations\n\n"
            "from typing import Any\n\n\n"
            "def build_plan() -> dict[str, Any]:\n"
            '    """Build the deployment plan from REQUIREMENTS.md."""\n\n'
            '    raise NotImplementedError("build_plan")\n'
        ),
        **_distractor_docs(17, "planner"),
    }
    hidden = {
        "tests/test_hidden_plan.py": _public_test(
            "from planner import build_plan\n\n\n"
            "def test_exact_plan() -> None:\n"
            "    plan = build_plan()\n"
            "    assert plan == {\n"
            "        'batch_size': 12,\n"
            "        'retry_limit': 4,\n"
            "        'regions': ['eu-west', 'us-east', 'ap-south'],\n"
            "        'features': {'audit': True, 'cache': False, 'metrics': True},\n"
            "    }\n\n\n"
            "def test_fresh_containers() -> None:\n"
            "    first = build_plan()\n"
            "    second = build_plan()\n"
            "    assert first is not second\n"
            "    assert first['regions'] is not second['regions']\n"
            "    first['regions'].append('mutation')\n"
            "    assert second['regions'] == ['eu-west', 'us-east', 'ap-south']\n"
        ),
    }
    return _single(
        sample_id="longcontext-005-requirements",
        category="long_context",
        mode="long_context",
        prompt=(
            "REQUIREMENTS.md is the authoritative specification. Implement build_plan() exactly, "
            "including fresh containers per call. Ignore archived notes and previous proposals. "
            "Do not modify tests/ or REQUIREMENTS.md."
        ),
        visible_files=visible,
        hidden_files=hidden,
        allowed_paths=("planner.py",),
        forbidden_paths=("tests/**", "REQUIREMENTS.md"),
        max_turns=10,
        tags=("long_context", "information_integrity"),
    )


TASKS: tuple[BenchmarkTask, ...] = (
    _bugfix_slugify(),
    _bugfix_median(),
    _bugfix_csv(),
    _bugfix_retry(),
    _bugfix_date_range(),
    _bugfix_intervals(),
    _bugfix_toposort(),
    _bugfix_roman(),
    _bugfix_pagination(),
    _bugfix_merge(),
    _feature_todo(),
    _feature_inventory(),
    _feature_diff(),
    _feature_validator(),
    _feature_bank(),
    _tool_fare(),
    _tool_clearance(),
    _tool_currency(),
    _tool_shipping(),
    _tool_policy(),
    _multiworker_handoff(),
    _multiworker_stats(),
    _multiworker_pipeline(),
    _multiworker_interface(),
    _multiworker_review(),
    _long_buried_constant(),
    _long_audit_rules(),
    _long_many_modules(),
    _long_changelog(),
    _long_requirements(),
)


def task_by_id(sample_id: str) -> BenchmarkTask:
    for task in TASKS:
        if task.sample_id == sample_id:
            return task
    raise KeyError(sample_id)


def validate_tasks(tasks: tuple[BenchmarkTask, ...] = TASKS) -> None:
    ids = [task.sample_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate sample_id in benchmark dataset")
    for task in tasks:
        if not task.hidden_files:
            raise ValueError(f"{task.sample_id}: every benchmark task needs a hidden oracle")
        if task.mode in ("coding", "long_context", "multi_worker") and not any(
            path.endswith(".py") for path in task.hidden_files
        ):
            raise ValueError(f"{task.sample_id}: coding task has no hidden Python oracle")


validate_tasks()
