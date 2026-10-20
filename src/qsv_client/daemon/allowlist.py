"""What a paired Data Schematic may run.

Browser requests never carry a path: the daemon appends the registered dataset as the last
argument. Each command has an explicit set of flags; anything else (``-o``, ``--output``,
attached short-flag values, extra positionals, an argument that starts with ``-`` where a
value is expected) is refused. Commands that read other files, reach the network or run code
(``sqlp``, ``luau``, ``py``, ``fetch``, ``describegpt``, ``to``, ``join``...) are not listed.
"""

from __future__ import annotations

from dataclasses import dataclass


class NotAllowed(ValueError):
    pass


NO_SIDE_FILES: dict[str, tuple[str, ...]] = {
    # stats caches its results in <stem>.stats.csv* beside the input unless told not to
    "stats": ("--cache-threshold", "0"),
}
QUERY_ENV = {"QSV_STATSCACHE_MODE": "none"}
"""Env for browser queries: "smart" commands such as frequency otherwise run stats and leave
its cache files beside the input."""
CSV_OUTPUT = frozenset(
    {"stats", "frequency", "slice", "search", "select", "sort", "dedup", "sample"}
)
"""Query commands whose stdout is CSV, unless a flag in ``NON_CSV_FLAGS`` is given."""
NON_CSV_FLAGS = frozenset({"--json", "--toon"})


@dataclass(frozen=True)
class CommandRule:
    switches: frozenset[str] = frozenset()
    """Flags without a value."""
    options: frozenset[str] = frozenset()
    """Flags that take one value."""
    positionals: int = 0
    """Positional arguments before the input (e.g. search's <regex>)."""
    limits: tuple[tuple[str, int], ...] = ()
    """Options whose value must be an integer from 0 to a maximum."""


def _rule(
    switches: str = "", options: str = "", positionals: int = 0, limits: str = ""
) -> CommandRule:
    """``limits`` is "flag=max ...": e.g. "-p=100 --pad=100"."""
    bounds = tuple((f, int(m)) for f, m in (s.split("=") for s in limits.split()))
    return CommandRule(frozenset(switches.split()), frozenset(options.split()), positionals, bounds)


BROWSER_COMMANDS: dict[str, CommandRule] = {
    "count": _rule("-H --human-readable --width --width-no-delims --json"),
    "headers": _rule("-j --just-names -J --just-count --trim"),
    "stats": _rule(
        "-E --everything --typesonly --infer-boolean --mode --cardinality --median --mad "
        "--quartiles --percentiles --nulls --infer-dates --prefer-dmy --vis-whitespace",
        "-s --select --round --dates-whitelist --percentile-list",
    ),
    "frequency": _rule(
        "-a --asc --no-nulls -i --ignore-case --no-trim --vis-whitespace --json --toon",
        "-s --select -l --limit -u --unq-limit --lmt-threshold --pct-dec-places --other-text",
    ),
    "slice": _rule("--json --invert", "-s --start -e --end -l --len -i --index"),
    "search": _rule(
        "-i --ignore-case --literal --exact -v --invert-match -u --unicode -c --count "
        "--json --not-one -Q --quick",
        "-s --select --preview-match",
        positionals=1,
    ),
    "select": _rule("-R --random --sort", "--seed", positionals=1),
    "sort": _rule(
        "-N --numeric -R --reverse -i --ignore-case -u --unique --natural --random",
        "-s --select --seed",
    ),
    "dedup": _rule("-N --numeric -i --ignore-case --sorted", "-s --select"),
    "sample": _rule("--bernoulli --systematic", "--seed", positionals=1),
    # width and padding multiply the output size, and the output cap only applies afterwards
    "table": _rule(
        "",
        "-w --width -p --pad -a --align -c --condense",
        limits="-w=100 --width=100 -p=100 --pad=100",
    ),
}


def build_query(command: str, args: list[object], available: frozenset[str]) -> list[str]:
    """Validate a browser's ad-hoc query; return the argv to run (without the input path)."""
    rule = BROWSER_COMMANDS.get(command)
    if rule is None or (available and command not in available):
        raise NotAllowed(f"command {command!r} is not available to Data Schematics")
    out: list[str] = []
    positionals = 0
    it = iter(args)
    for raw in it:
        if not isinstance(raw, str | int | float) or isinstance(raw, bool):
            raise NotAllowed("arguments must be strings or numbers")
        arg = str(raw)
        if arg.startswith("-") and not _is_number(arg):
            name, eq, value = arg.partition("=")
            if name in rule.switches and not eq:
                out.append(name)
            elif name in rule.options:
                if not eq:
                    nxt = next(it, None)
                    if nxt is None or isinstance(nxt, bool):
                        raise NotAllowed(f"{name} needs a value")
                    value = str(nxt)
                if value.startswith("-") and not _is_number(value):
                    raise NotAllowed(f"value for {name} may not start with '-'")
                limit = dict(rule.limits).get(name)
                if limit is not None and not (
                    value.isascii() and value.isdigit() and int(value) <= limit
                ):
                    raise NotAllowed(f"{name} must be a whole number from 0 to {limit}")
                out += [name, value]
            else:
                raise NotAllowed(f"flag {name!r} is not allowed for {command}")
        else:
            positionals += 1
            if positionals > rule.positionals:
                raise NotAllowed(f"{command} takes no file or extra argument here")
            out.append(arg)
    if positionals < rule.positionals:
        raise NotAllowed(f"{command} needs {rule.positionals} argument(s)")
    return [command, *out, *NO_SIDE_FILES.get(command, ())]


def _is_number(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


# the characters Rust's regex::escape escapes (Python's re.escape differs, e.g. for spaces)
_RUST_META = frozenset("\\.+*?()|[]{}^$#&-~")


def exact_match_regex(value: str) -> str:
    """A Rust regex matching exactly ``value``; safe as a positional even if it starts with -."""
    return "^" + "".join("\\" + c if c in _RUST_META else c for c in value) + "$"


def contains_regex(value: str) -> str:
    """A Rust regex matching ``value`` anywhere; the group keeps a leading - from reading as a
    flag."""
    return "(?:" + "".join("\\" + c if c in _RUST_META else c for c in value) + ")"
