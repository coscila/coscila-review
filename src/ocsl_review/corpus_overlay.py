# SPDX-License-Identifier: Apache-2.0
"""A delta database over an immutable parent snapshot (Python stdlib).

A branch keeps only its own rows. Each connection ATTACHes the parent database
read-only and immutable and shadows the shared tables with TEMP views: reads
see the branch's rows and every parent row the branch does not override.
INSTEAD OF triggers send writes to the branch; updating a parent row first
copies it into the branch (copy-on-write) so the branch's own triggers still
enforce append-only tables, and parent rows cannot be deleted. Analysis queue
tables stay branch-local. SQLite cannot enforce foreign keys across databases,
so branch connections disable them and TEMP triggers check every declared
reference of an inserted or updated row against the union instead;
verification repeats that check for all branch rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3


PARENT = "ocsl_parent"
ORDERED_PREFIX = "ocsl_ordered_"
# Shared corpus tables; anything else (task queues, batches, processing
# checkpoints and tables added by later schema versions) stays branch-local.
SHARED = (
    "corpora", "runs", "analyses", "works", "work_identifiers", "source_artifacts", "retrievals",
    "source_sections", "reviews", "bibliography_entries", "claim_loci", "model_receipts", "assessments",
    "assessment_evidence", "work_observations", "work_observation_evidence", "facts", "corpus_reviews",
    "corpus_documents", "corpus_imports", "retained_sources", "store_meta",
)
# Identities that must stay unique across parent and branch (a clash raises).
STRICT = ("corpora", "runs", "analyses")


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    uniques: tuple[tuple[str, ...], ...]
    defaults: dict
    rowid: bool


def _tables(connection: sqlite3.Connection, schema: str = "main") -> dict[str, Table]:
    tables = {}
    rows = connection.execute(f"SELECT name,sql FROM {schema}.sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
    for name, sql in rows:
        info = connection.execute(f"PRAGMA {schema}.table_info({quote(name)})").fetchall()
        columns = tuple(row[1] for row in info)
        key = tuple(row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5]))
        defaults = {row[1]: row[4] for row in info if row[3] and row[4] is not None}
        uniques = []
        for index in connection.execute(f"PRAGMA {schema}.index_list({quote(name)})").fetchall():
            # (seq, name, unique, origin, partial); origin u = UNIQUE constraint, c = CREATE UNIQUE INDEX
            if index[2] and index[3] in ("u", "c") and not index[4]:
                members = tuple(row[2] for row in connection.execute(
                    f"PRAGMA {schema}.index_info({quote(index[1])})").fetchall())
                if members and None not in members and members != key:
                    uniques.append(members)
        rowid = "WITHOUT ROWID" not in (sql or "").upper()
        tables[name] = Table(name, columns, key, tuple(uniques), defaults, rowid)
    return tables


def _match(columns, left: str, right: str) -> str:
    return " AND ".join(f"{left}.{quote(c)}={right}.{quote(c)}" for c in columns)


class Overlay:
    """Generates and installs the per-connection TEMP schema for one branch."""

    def __init__(self, delta: Path, parent: Path):
        self.delta = Path(delta)
        self.parent = Path(parent)
        self.parent_uri = self.parent.as_uri() + "?mode=ro&immutable=1"
        probe = sqlite3.connect(self.delta.as_uri() + "?mode=ro", uri=True)
        try:
            probe.execute(f"ATTACH DATABASE ? AS {PARENT}", (self.parent_uri,))
            self.tables = _tables(probe)
            parent = _tables(probe, PARENT)
            self.shared = []
            for name in SHARED:
                if name not in self.tables:
                    continue
                if name not in parent or parent[name].columns != self.tables[name].columns:
                    raise ValueError(f"branch schema differs from its parent snapshot: {name}")
                # An empty parent table needs no view; the branch table answers alone.
                if probe.execute(f"SELECT EXISTS(SELECT 1 FROM {PARENT}.{quote(name)})").fetchone()[0]:
                    self.shared.append(name)
            self.parent_version = probe.execute(f"PRAGMA {PARENT}.user_version").fetchone()[0]
            self.version = probe.execute("PRAGMA main.user_version").fetchone()[0]
            self.foreign_keys = {name: probe.execute(f"PRAGMA foreign_key_list({quote(name)})").fetchall()
                                 for name in self.tables}
        finally:
            probe.close()
        if self.parent_version != self.version:
            raise ValueError("branch schema version differs from its parent snapshot")
        self.script = "\n".join(self._statements())

    def _references(self, name):
        """[(target, local columns, target columns)] for each declared foreign key of a table."""
        groups: dict[int, list] = {}
        for row in self.foreign_keys.get(name, ()):
            groups.setdefault(row[0], []).append(row)
        references = []
        for members in groups.values():
            members.sort(key=lambda r: r[1])
            target = members[0][2]
            local = [r[3] for r in members]
            remote = [r[4] for r in members]
            if any(column is None for column in remote) and target in self.tables:
                remote = list(self.tables[target].key)
            references.append((target, local, remote))
        return references

    def _checks(self, name):
        """RAISE statements emulating immediate foreign keys for NEW over the union."""
        statements = []
        for target, local, remote in self._references(name):
            if target not in self.tables or len(remote) != len(local) or None in remote:
                continue
            present = " AND ".join(f"NEW.{quote(c)} IS NOT NULL" for c in local)
            matched = " AND ".join(f"r.{quote(rc)}=NEW.{quote(lc)}" for lc, rc in zip(local, remote))
            statements.append(f"SELECT RAISE(ABORT, 'FOREIGN KEY constraint failed') WHERE {present} AND NOT EXISTS "
                              f"(SELECT 1 FROM {quote(target)} AS r WHERE {matched});")
        return " ".join(statements)

    def _statements(self):
        for name, table in self.tables.items():
            if name in self.shared:
                continue
            checks = self._checks(name)
            if checks:
                # Branch-local tables and empty shared tables are written directly.
                for action in ("INSERT", "UPDATE"):
                    yield (f"CREATE TEMP TRIGGER {quote('ocsl_fk_' + action.lower() + '_' + name)} BEFORE {action} "
                           f"ON main.{quote(name)} BEGIN {checks} END;")
        for name in self.shared:
            table = self.tables[name]
            t = quote(name)
            columns = ", ".join(quote(c) for c in table.columns)
            parent_columns = ", ".join("p." + quote(c) for c in table.columns)
            if table.key:
                hidden = f" WHERE NOT EXISTS (SELECT 1 FROM main.{t} AS d WHERE {_match(table.key, 'd', 'p')})"
            else:
                hidden = ""
            yield (f"CREATE TEMP VIEW {t} AS SELECT {columns} FROM main.{t} UNION ALL "
                   f"SELECT {parent_columns} FROM {PARENT}.{t} AS p{hidden};")
            if table.rowid:
                yield (f"CREATE TEMP VIEW {quote(ORDERED_PREFIX + name)} AS "
                       f"SELECT 0 AS ocsl_source, rowid AS ocsl_rowid, {columns} FROM main.{t} UNION ALL "
                       f"SELECT 1, p.rowid, {parent_columns} FROM {PARENT}.{t} AS p{hidden};")
            values = ", ".join(f"coalesce(NEW.{quote(c)}, {table.defaults[c]})" if c in table.defaults
                               else f"NEW.{quote(c)}" for c in table.columns)
            clashes = [f"NOT EXISTS (SELECT 1 FROM {PARENT}.{t} AS p WHERE {_match(group, 'p', 'NEW')})"
                       for group in ((table.key,) if table.key else ()) + table.uniques]
            guard = " WHERE " + " AND ".join(clashes) if clashes else ""
            strict = ""
            if name in STRICT and table.key:
                strict = (f"SELECT RAISE(ABORT, 'UNIQUE constraint failed: {name} exists in the parent snapshot') "
                          f"WHERE EXISTS (SELECT 1 FROM {PARENT}.{t} AS p WHERE {_match(table.key, 'p', 'NEW')}); ")
            checks = self._checks(name)
            yield (f"CREATE TEMP TRIGGER {quote('ocsl_insert_' + name)} INSTEAD OF INSERT ON {t} BEGIN {strict}"
                   f"{checks} INSERT INTO main.{t}({columns}) SELECT {values}{guard}; END;")
            if table.key:
                assignments = ", ".join(f"{quote(c)}=NEW.{quote(c)}" for c in table.columns)
                old = " AND ".join(f"{quote(c)}=OLD.{quote(c)}" for c in table.key)
                parent_old = " AND ".join(f"p.{quote(c)}=OLD.{quote(c)}" for c in table.key)
                delta_old = " AND ".join(f"d.{quote(c)}=OLD.{quote(c)}" for c in table.key)
                yield (f"CREATE TEMP TRIGGER {quote('ocsl_update_' + name)} INSTEAD OF UPDATE ON {t} BEGIN {checks} "
                       f"INSERT INTO main.{t}({columns}) SELECT {parent_columns} FROM {PARENT}.{t} AS p "
                       f"WHERE {parent_old} AND NOT EXISTS (SELECT 1 FROM main.{t} AS d WHERE {delta_old}); "
                       f"UPDATE main.{t} SET {assignments} WHERE {old}; END;")
                yield (f"CREATE TEMP TRIGGER {quote('ocsl_delete_' + name)} INSTEAD OF DELETE ON {t} BEGIN "
                       f"SELECT RAISE(ABORT, '{name}: rows of the parent snapshot are immutable') "
                       f"WHERE EXISTS (SELECT 1 FROM {PARENT}.{t} AS p WHERE {parent_old}); "
                       f"DELETE FROM main.{t} WHERE {old}; END;")
            else:
                for action in ("UPDATE", "DELETE"):
                    yield (f"CREATE TEMP TRIGGER {quote('ocsl_' + action.lower() + '_' + name)} INSTEAD OF {action} "
                           f"ON {t} BEGIN SELECT RAISE(ABORT, '{name} has no key; branch {action.lower()}s are not "
                           f"supported'); END;")

    def attach(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"ATTACH DATABASE ? AS {PARENT}", (self.parent_uri,))
        connection.executescript(self.script)
        connection.execute("PRAGMA foreign_keys=OFF")

    def ordered(self, table: str, alias: str | None = None) -> tuple[str, str]:
        """(FROM source, ORDER BY expression) reproducing rowid order over the union."""
        prefix = f"{alias}." if alias else ""
        if table in self.shared and self.tables[table].rowid:
            return quote(ORDERED_PREFIX + table), f"{prefix}ocsl_source, {prefix}ocsl_rowid"
        return table, f"{prefix}rowid"

    def foreign_key_problems(self, connection: sqlite3.Connection, limit: int = 20) -> list[str]:
        """Branch rows whose declared references resolve neither in the branch nor in the parent."""
        problems = []
        for name in self.tables:
            for target, local, remote in self._references(name):
                if target not in self.tables or len(remote) != len(local) or None in remote:
                    problems.append(f"{name}: reference to unknown table {target}")
                    continue
                present = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in local)
                matched = " AND ".join(f"r.{quote(rc)}=c.{quote(lc)}" for lc, rc in zip(local, remote))
                count = connection.execute(
                    f"SELECT count(*) FROM main.{quote(name)} AS c WHERE {present} AND NOT EXISTS "
                    f"(SELECT 1 FROM {quote(target)} AS r WHERE {matched})").fetchone()[0]
                if count:
                    problems.append(f"{name}({', '.join(local)}) -> {target}: {count} unresolved")
                if len(problems) >= limit:
                    return problems
        return problems
