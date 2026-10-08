"""Literal non-IN SQL snapshots captured at e6e819b, before R2 compiler edits."""

from typing import Any

from ontary.store._filter import RowFilter, ScopeTerm, WhereTerm

# name, row filter, SQLite (SQL, params), PostgreSQL (SQL, params).
GOLDEN_FILTERS: tuple[tuple[str, RowFilter, tuple[str, list[Any]], tuple[str, list[Any]]], ...] = (
    (
        'str-eq',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='eq', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-ne',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='ne', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <> o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN TRUE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <> o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-contains',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='str',
                                   op='contains',
                                   operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT json_type(payload, {p}) AS "
         't, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-gt',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='gt', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY > o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" > o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-gte',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='gte', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY >= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-lt',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='lt', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY < o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" < o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-lte',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='lte', operand="é%_\\'text"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', "é%_\\'text"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <= o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', "é%_\\'text"]),
    ),
    (
        'str-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='str',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='str',
                                                      op='gte',
                                                      operand="é%_\\'text"),
                                            WhereTerm(field='value',
                                                      prop_type='str',
                                                      op='lte',
                                                      operand="é%_\\'text"))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY >= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <= o COLLATE BINARY "
         'END FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', "é%_\\'text", '$."value"', '$."value"', "é%_\\'text"]),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         '\'null\' THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" '
         'END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, '
         "'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'string') "
         'THEN TRUE ELSE v COLLATE "C" <= o COLLATE "C" END FROM (SELECT jsonb_typeof(payload::jsonb '
         '-> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', "é%_\\'text", 'value', 'value', "é%_\\'text"]),
    ),
    (
        'str-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='eq', operand=None),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t = 'text')) "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'string')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'str-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='ne', operand=None),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'int-eq',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='eq', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v = o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric = o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-ne',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='ne', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <> o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN TRUE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric <> o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-contains',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='contains', operand=42),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-gt',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='gt', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v > o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric > o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-gte',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='gte', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric >= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-lt',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='lt', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v < o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric < o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-lte',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='lte', operand=42),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 42]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric <= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 42]),
    ),
    (
        'int-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='int',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='int',
                                                      op='gte',
                                                      operand=42),
                                            WhereTerm(field='value',
                                                      prop_type='int',
                                                      op='lte',
                                                      operand=42))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' "
         'AND (v > 9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM '
         '(SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', 42, '$."value"', '$."value"', 42]),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric >= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         "AS o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         "(SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE "
         'CASE WHEN abs(v::numeric) > 9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE '
         'v::numeric <= o END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, '
         'payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', 42, 'value', 'value', 42]),
    ),
    (
        'int-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='eq', operand=None),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t IN "
         "('integer', 'real'))) FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) "
         'AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'number')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'int-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='int', op='ne', operand=None),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'float-eq',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='eq',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v = o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric = o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-ne',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='ne',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <> o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN TRUE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric <> o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-contains',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='contains',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-gt',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='gt',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v > o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric > o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-gte',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='gte',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric >= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-lt',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='lt',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v < o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric < o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-lte',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='lte',
                                   operand=1.0000000000000002),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.0000000000000002]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric <= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.0000000000000002']),
    ),
    (
        'float-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='float',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='float',
                                                      op='gte',
                                                      operand=1.0000000000000002),
                                            WhereTerm(field='value',
                                                      prop_type='float',
                                                      op='lte',
                                                      operand=1.0000000000000002))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' "
         'AND (v > 9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM '
         '(SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', 1.0000000000000002, '$."value"', '$."value"', 1.0000000000000002]),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric >= o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         "AS o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         "(SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE "
         'CASE WHEN abs(v::numeric) > 9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE '
         'v::numeric <= o END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, '
         'payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', '1.0000000000000002', 'value', 'value', '1.0000000000000002']),
    ),
    (
        'float-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='float', op='eq', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t IN "
         "('integer', 'real'))) FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) "
         'AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'number')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'float-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='float', op='ne', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'bool-eq',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='eq', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v = o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '= o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-ne',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='ne', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <> o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN TRUE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '<> o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-contains',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='contains', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-gt',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='gt', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v > o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '> o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-gte',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='gte', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '>= o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-lt',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='lt', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v < o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '< o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-lte',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='lte', operand=True),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '<= o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 1]),
    ),
    (
        'bool-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='bool',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='bool',
                                                      op='gte',
                                                      operand=True),
                                            WhereTerm(field='value',
                                                      prop_type='bool',
                                                      op='lte',
                                                      operand=True))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' "
         'AND (v > 9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM '
         '(SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', 1, '$."value"', '$."value"', 1]),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE "
         '0 END) >= o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> '
         '{p} AS v, {p}::numeric AS o) AS filter_value) ELSE TRUE END AND CASE WHEN '
         "pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN "
         "FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) <= o "
         'END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', 1, 'value', 'value', 1]),
    ),
    (
        'bool-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='eq', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t IN ('true', "
         "'false'))) FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} "
         'AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'boolean')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'bool-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='bool', op='ne', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'date-eq',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='eq', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-ne',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='ne', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <> o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN TRUE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <> o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-contains',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='date',
                                   op='contains',
                                   operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT json_type(payload, {p}) AS "
         't, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-gt',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='gt', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY > o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN v !~ "
         '\'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" > o COLLATE "C" END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-gte',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='gte', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY >= o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN v !~ "
         '\'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-lt',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='lt', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY < o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN v !~ "
         '\'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" < o COLLATE "C" END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-lte',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='lte', operand='2026-02-15'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY <= o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN v !~ "
         '\'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" <= o COLLATE "C" END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15']),
    ),
    (
        'date-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='date',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='date',
                                                      op='gte',
                                                      operand='2026-02-15'),
                                            WhereTerm(field='value',
                                                      prop_type='date',
                                                      op='lte',
                                                      operand='2026-02-15'))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY >= o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS '
         "NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'text') THEN TRUE ELSE CASE WHEN v NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' THEN TRUE ELSE v COLLATE BINARY <= o END END "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', '2026-02-15', '$."value"', '$."value"', '2026-02-15']),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN v !~ "
         '\'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         "o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         "(SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE "
         'CASE WHEN v !~ \'^[0-9]{4}-[0-9]{2}-[0-9]{2}$\' THEN TRUE ELSE v COLLATE "C" <= o COLLATE '
         '"C" END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS '
         'v, {p}::text AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', '2026-02-15', 'value', 'value', '2026-02-15']),
    ),
    (
        'date-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='eq', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t = 'text')) "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'string')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'date-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='date', op='ne', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'datetime-eq',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='eq',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-ne',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='ne',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <> o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN TRUE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <> o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-contains',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='contains',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT json_type(payload, {p}) AS "
         't, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-gt',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='gt',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN ontary_instant(v) IS NULL OR ontary_instant(o) IS "
         'NULL THEN TRUE WHEN substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) THEN '
         'FALSE ELSE ontary_instant(v) > ontary_instant(o) END END FROM (SELECT json_type(payload, '
         '{p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN NOT (v ~ {p} AND o ~ {p}) THEN "
         "TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END) AND pg_input_is_valid(o, "
         "CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp "
         "without time zone' END)) THEN TRUE WHEN (v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         'v::timestamptz > o::timestamptz ELSE v::timestamp > o::timestamp END END FROM (SELECT '
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-gte',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='gte',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN ontary_instant(v) IS NULL OR ontary_instant(o) IS "
         'NULL THEN TRUE WHEN substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) THEN '
         'FALSE ELSE ontary_instant(v) >= ontary_instant(o) END END FROM (SELECT json_type(payload, '
         '{p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN NOT (v ~ {p} AND o ~ {p}) THEN "
         "TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END) AND pg_input_is_valid(o, "
         "CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp "
         "without time zone' END)) THEN TRUE WHEN (v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         'v::timestamptz >= o::timestamptz ELSE v::timestamp >= o::timestamp END END FROM (SELECT '
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-lt',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='lt',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN ontary_instant(v) IS NULL OR ontary_instant(o) IS "
         'NULL THEN TRUE WHEN substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) THEN '
         'FALSE ELSE ontary_instant(v) < ontary_instant(o) END END FROM (SELECT json_type(payload, '
         '{p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN NOT (v ~ {p} AND o ~ {p}) THEN "
         "TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END) AND pg_input_is_valid(o, "
         "CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp "
         "without time zone' END)) THEN TRUE WHEN (v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         'v::timestamptz < o::timestamptz ELSE v::timestamp < o::timestamp END END FROM (SELECT '
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-lte',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='lte',
                                   operand='2026-02-15T09:00:00+09:00'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE CASE WHEN ontary_instant(v) IS NULL OR ontary_instant(o) IS "
         'NULL THEN TRUE WHEN substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) THEN '
         'FALSE ELSE ontary_instant(v) <= ontary_instant(o) END END FROM (SELECT json_type(payload, '
         '{p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', '2026-02-15T09:00:00+09:00']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN NOT (v ~ {p} AND o ~ {p}) THEN "
         "TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END) AND pg_input_is_valid(o, "
         "CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp "
         "without time zone' END)) THEN TRUE WHEN (v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         'v::timestamptz <= o::timestamptz ELSE v::timestamp <= o::timestamp END END FROM (SELECT '
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='datetime',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='datetime',
                                                      op='gte',
                                                      operand='2026-02-15T09:00:00+09:00'),
                                            WhereTerm(field='value',
                                                      prop_type='datetime',
                                                      op='lte',
                                                      operand='2026-02-15T09:00:00+09:00'))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t = 'text') THEN TRUE ELSE CASE WHEN ontary_instant(v) IS NULL OR "
         'ontary_instant(o) IS NULL THEN TRUE WHEN substr(ontary_instant(v), 1, 1) <> '
         'substr(ontary_instant(o), 1, 1) THEN FALSE ELSE ontary_instant(v) >= ontary_instant(o) END '
         'END FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS '
         "NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'text') THEN TRUE ELSE CASE WHEN "
         'ontary_instant(v) IS NULL OR ontary_instant(o) IS NULL THEN TRUE WHEN '
         'substr(ontary_instant(v), 1, 1) <> substr(ontary_instant(o), 1, 1) THEN FALSE ELSE '
         'ontary_instant(v) <= ontary_instant(o) END END FROM (SELECT json_type(payload, {p}) AS t, '
         'json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END)',
         ['$."value"',
          '$."value"',
          '2026-02-15T09:00:00+09:00',
          '$."value"',
          '$."value"',
          '2026-02-15T09:00:00+09:00']),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE WHEN NOT (v ~ {p} AND o ~ {p}) "
         "THEN TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END) AND pg_input_is_valid(o, "
         "CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp "
         "without time zone' END)) THEN TRUE WHEN (v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         'v::timestamptz >= o::timestamptz ELSE v::timestamp >= o::timestamp END END FROM (SELECT '
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         "filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT "
         "CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE CASE "
         'WHEN NOT (v ~ {p} AND o ~ {p}) THEN TRUE WHEN NOT (pg_input_is_valid(v, CASE WHEN v ~ '
         "'(Z|[+-][0-9]{2}:[0-9]{2})$' THEN 'timestamp with time zone' ELSE 'timestamp without time "
         "zone' END) AND pg_input_is_valid(o, CASE WHEN o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$' THEN "
         "'timestamp with time zone' ELSE 'timestamp without time zone' END)) THEN TRUE WHEN (v ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$') <> (o ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') THEN FALSE WHEN v ~ "
         "'(Z|[+-][0-9]{2}:[0-9]{2})$' THEN v::timestamptz <= o::timestamptz ELSE v::timestamp <= "
         'o::timestamp END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb '
         '->> {p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END)',
         ['^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          '^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(\\.\\d{1,6})?(Z|[+-]\\d{2}:\\d{2})?$',
          'value',
          'value',
          '2026-02-15T09:00:00+09:00']),
    ),
    (
        'datetime-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='datetime', op='eq', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (NOT (t = 'text')) "
         'FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR (NOT "
         "(t = 'string')) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> "
         '{p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'datetime-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='datetime', op='ne', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'json-eq',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='eq', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-ne',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='ne', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <> o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN TRUE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <> o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-contains',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='contains', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE instr(v, o) > 0 END FROM (SELECT json_type(payload, {p}) AS "
         't, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'string') THEN TRUE ELSE strpos(v, o) > 0 END FROM (SELECT "
         'jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS '
         'filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-gt',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='gt', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY > o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" > o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-gte',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='gte', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY >= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-lt',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='lt', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY < o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" < o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-lte',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='lte', operand='open'),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 'open']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" <= o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 'open']),
    ),
    (
        'json-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='json',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='json',
                                                      op='gte',
                                                      operand='open'),
                                            WhereTerm(field='value',
                                                      prop_type='json',
                                                      op='lte',
                                                      operand='open'))),),
                  scope=None),
        ("(CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY >= o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY <= o COLLATE BINARY "
         'END FROM (SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', 'open', '$."value"', '$."value"', 'open']),
        ("(CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         '\'null\' THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" >= o COLLATE "C" '
         'END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, '
         "'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'string') "
         'THEN TRUE ELSE v COLLATE "C" <= o COLLATE "C" END FROM (SELECT jsonb_typeof(payload::jsonb '
         '-> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', 'open', 'value', 'value', 'open']),
    ),
    (
        'json-eq-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='eq', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT (t IS NULL OR t = 'null') OR (FALSE) FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT (t IS NULL OR t = 'null') OR "
         '(FALSE) FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'json-ne-null',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='ne', operand=None),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT NOT (t IS NULL OR t = 'null') FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', None]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT NOT (t IS NULL OR t = 'null') "
         'FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::text AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', None]),
    ),
    (
        'scope-self-False',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('self', None),), scope_id='équipe', climb_possible=False)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN TRUE THEN id COLLATE BINARY = {p} ELSE FALSE '
         'END ELSE TRUE END',
         ['équipe']),
        ('CASE WHEN pg_input_is_valid(payload, \'jsonb\') THEN CASE WHEN TRUE THEN id COLLATE "C" = '
         '{p} ELSE FALSE END ELSE TRUE END',
         ['équipe']),
    ),
    (
        'scope-self-True',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('self', None),), scope_id='équipe', climb_possible=True)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN TRUE THEN id COLLATE BINARY = {p} ELSE TRUE END '
         'ELSE TRUE END',
         ['équipe']),
        ('CASE WHEN pg_input_is_valid(payload, \'jsonb\') THEN CASE WHEN TRUE THEN id COLLATE "C" = '
         '{p} ELSE TRUE END ELSE TRUE END',
         ['équipe']),
    ),
    (
        'scope-prop-False',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('prop', 'team_id'),), scope_id='équipe', climb_possible=False)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS NOT NULL AND '
         "json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> 'text' THEN TRUE "
         'WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE json_extract(payload, '
         '{p}) COLLATE BINARY = {p} END ELSE FALSE END ELSE TRUE END',
         ['$."team_id"', '$."team_id"', '$."team_id"', '$."team_id"', '$."team_id"', 'équipe']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN CASE WHEN jsonb_typeof(payload::jsonb -> "
         "{p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> {p}) <> 'null' THEN CASE WHEN "
         "jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE (payload::jsonb ->> {p}) "
         'COLLATE "C" = {p} END ELSE FALSE END ELSE TRUE END',
         ['team_id', 'team_id', 'team_id', 'team_id', 'équipe']),
    ),
    (
        'scope-prop-True',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('prop', 'team_id'),), scope_id='équipe', climb_possible=True)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS NOT NULL AND '
         "json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> 'text' THEN TRUE "
         'WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE json_extract(payload, '
         '{p}) COLLATE BINARY = {p} END ELSE TRUE END ELSE TRUE END',
         ['$."team_id"', '$."team_id"', '$."team_id"', '$."team_id"', '$."team_id"', 'équipe']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN CASE WHEN jsonb_typeof(payload::jsonb -> "
         "{p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> {p}) <> 'null' THEN CASE WHEN "
         "jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE (payload::jsonb ->> {p}) "
         'COLLATE "C" = {p} END ELSE TRUE END ELSE TRUE END',
         ['team_id', 'team_id', 'team_id', 'team_id', 'équipe']),
    ),
    (
        'scope-prop-self-False',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('prop', 'team_id'), ('self', None)),
                                  scope_id='équipe',
                                  climb_possible=False)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS NOT NULL AND '
         "json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> 'text' THEN TRUE "
         'WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE json_extract(payload, '
         '{p}) COLLATE BINARY = {p} END WHEN TRUE THEN id COLLATE BINARY = {p} ELSE FALSE END ELSE '
         'TRUE END',
         ['$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          'équipe',
          'équipe']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN CASE WHEN jsonb_typeof(payload::jsonb -> "
         "{p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> {p}) <> 'null' THEN CASE WHEN "
         "jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE (payload::jsonb ->> {p}) "
         'COLLATE "C" = {p} END WHEN TRUE THEN id COLLATE "C" = {p} ELSE FALSE END ELSE TRUE END',
         ['team_id', 'team_id', 'team_id', 'team_id', 'équipe', 'équipe']),
    ),
    (
        'scope-prop-self-True',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('prop', 'team_id'), ('self', None)),
                                  scope_id='équipe',
                                  climb_possible=True)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS NOT NULL AND '
         "json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> 'text' THEN TRUE "
         'WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE json_extract(payload, '
         '{p}) COLLATE BINARY = {p} END WHEN TRUE THEN id COLLATE BINARY = {p} ELSE TRUE END ELSE TRUE '
         'END',
         ['$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          'équipe',
          'équipe']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN CASE WHEN jsonb_typeof(payload::jsonb -> "
         "{p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> {p}) <> 'null' THEN CASE WHEN "
         "jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE (payload::jsonb ->> {p}) "
         'COLLATE "C" = {p} END WHEN TRUE THEN id COLLATE "C" = {p} ELSE TRUE END ELSE TRUE END',
         ['team_id', 'team_id', 'team_id', 'team_id', 'équipe', 'équipe']),
    ),
    (
        'scope-empty-False',
        RowFilter(where=(), scope=ScopeTerm(rules=(), scope_id='équipe', climb_possible=False)),
        ('FALSE', []),
        ('FALSE', []),
    ),
    (
        'scope-empty-True',
        RowFilter(where=(), scope=ScopeTerm(rules=(), scope_id='équipe', climb_possible=True)),
        ('TRUE', []),
        ('TRUE', []),
    ),
    (
        'empty',
        RowFilter(where=(), scope=None),
        ('TRUE', []),
        ('TRUE', []),
    ),
    (
        'empty-and',
        RowFilter(where=(WhereTerm(field='value', prop_type='str', op='and', operand=()),), scope=None),
        ('TRUE', []),
        ('TRUE', []),
    ),
    (
        'json-number',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='eq', operand=1.5),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v = o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 1.5]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric = o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', '1.5']),
    ),
    (
        'json-int',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='ne', operand=7),), scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN TRUE WHEN "
         "NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <> o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 7]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN TRUE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric <> o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         'AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 7]),
    ),
    (
        'json-bool',
        RowFilter(where=(WhereTerm(field='value', prop_type='json', op='eq', operand=False),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t IN ('true', 'false')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v = o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."value"', '$."value"', 0]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         "THEN FALSE WHEN NOT (t = 'boolean') THEN TRUE ELSE (CASE WHEN v = 'true' THEN 1 ELSE 0 END) "
         '= o END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, '
         '{p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['value', 'value', 0]),
    ),
    (
        'multiple-where',
        RowFilter(where=(WhereTerm(field='status', prop_type='str', op='eq', operand='open'),
                         WhereTerm(field='score', prop_type='int', op='gte', operand=-7)),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' "
         'AND (v > 9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v >= o END END FROM '
         '(SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END',
         ['$."status"', '$."status"', 'open', '$."score"', '$."score"', -7]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         "o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         "(SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE "
         'CASE WHEN abs(v::numeric) > 9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE '
         'v::numeric >= o END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, '
         'payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS filter_value) ELSE TRUE END',
         ['status', 'status', 'open', 'score', 'score', -7]),
    ),
    (
        'where-scope',
        RowFilter(where=(WhereTerm(field='status', prop_type='str', op='eq', operand='open'),),
                  scope=ScopeTerm(rules=(('prop', 'team_id'), ('self', None)),
                                  scope_id='team',
                                  climb_possible=False)),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END AND CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS '
         "NOT NULL AND json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> "
         "'text' THEN TRUE WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE "
         'json_extract(payload, {p}) COLLATE BINARY = {p} END WHEN TRUE THEN id COLLATE BINARY = {p} '
         'ELSE FALSE END ELSE TRUE END',
         ['$."status"',
          '$."status"',
          'open',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          '$."team_id"',
          'team',
          'team']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         "o) AS filter_value) ELSE TRUE END AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         'CASE WHEN jsonb_typeof(payload::jsonb -> {p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> '
         "{p}) <> 'null' THEN CASE WHEN jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE "
         '(payload::jsonb ->> {p}) COLLATE "C" = {p} END WHEN TRUE THEN id COLLATE "C" = {p} ELSE '
         'FALSE END ELSE TRUE END',
         ['status', 'status', 'open', 'team_id', 'team_id', 'team_id', 'team_id', 'team', 'team']),
    ),
    (
        'nested-and',
        RowFilter(where=(WhereTerm(field='value',
                                   prop_type='int',
                                   op='and',
                                   operand=(WhereTerm(field='value',
                                                      prop_type='int',
                                                      op='and',
                                                      operand=(WhereTerm(field='value',
                                                                         prop_type='int',
                                                                         op='gt',
                                                                         operand=0),)),
                                            WhereTerm(field='value',
                                                      prop_type='int',
                                                      op='lte',
                                                      operand=3))),),
                  scope=None),
        ("((CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE "
         "WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' AND (v > "
         '9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v > o END END FROM (SELECT '
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END) AND CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = '
         "'null' THEN FALSE WHEN NOT (t IN ('integer', 'real')) THEN TRUE ELSE CASE WHEN t = 'integer' "
         'AND (v > 9007199254740992 OR v < -9007199254740992) THEN TRUE ELSE v <= o END END FROM '
         '(SELECT json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS '
         'filter_value) ELSE TRUE END)',
         ['$."value"', '$."value"', 0, '$."value"', '$."value"', 3]),
        ("((CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = "
         "'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE CASE WHEN abs(v::numeric) > "
         '9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE v::numeric > o END END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::numeric '
         "AS o) AS filter_value) ELSE TRUE END) AND CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN "
         "(SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN NOT (t = 'number') THEN TRUE ELSE "
         'CASE WHEN abs(v::numeric) > 9007199254740992 OR abs(o) > 9007199254740992 THEN TRUE ELSE '
         'v::numeric <= o END END FROM (SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, '
         'payload::jsonb ->> {p} AS v, {p}::numeric AS o) AS filter_value) ELSE TRUE END)',
         ['value', 'value', 0, 'value', 'value', 3]),
    ),
    (
        'quoted-field',
        RowFilter(where=(WhereTerm(field='private"field',
                                   prop_type='str',
                                   op='eq',
                                   operand="x'); DROP TABLE objects; --"),),
                  scope=None),
        ("CASE WHEN json_valid(payload) THEN (SELECT CASE WHEN t IS NULL OR t = 'null' THEN FALSE WHEN "
         "NOT (t = 'text') THEN TRUE ELSE v COLLATE BINARY = o COLLATE BINARY END FROM (SELECT "
         'json_type(payload, {p}) AS t, json_extract(payload, {p}) AS v, {p} AS o) AS filter_value) '
         'ELSE TRUE END',
         ['$."private\\"field"', '$."private\\"field"', "x'); DROP TABLE objects; --"]),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN (SELECT CASE WHEN t IS NULL OR t = 'null' "
         'THEN FALSE WHEN NOT (t = \'string\') THEN TRUE ELSE v COLLATE "C" = o COLLATE "C" END FROM '
         '(SELECT jsonb_typeof(payload::jsonb -> {p}) AS t, payload::jsonb ->> {p} AS v, {p}::text AS '
         'o) AS filter_value) ELSE TRUE END',
         ['private"field', 'private"field', "x'); DROP TABLE objects; --"]),
    ),
    (
        'scope-unicode-property',
        RowFilter(where=(),
                  scope=ScopeTerm(rules=(('prop', 'équipe'),), scope_id='café', climb_possible=True)),
        ('CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload, {p}) IS NOT NULL AND '
         "json_type(payload, {p}) <> 'null' THEN CASE WHEN json_type(payload, {p}) <> 'text' THEN TRUE "
         'WHEN instr(json_extract(payload, {p}), char(0)) > 0 THEN TRUE ELSE json_extract(payload, '
         '{p}) COLLATE BINARY = {p} END ELSE TRUE END ELSE TRUE END',
         ['$."équipe"', '$."équipe"', '$."équipe"', '$."équipe"', '$."équipe"', 'café']),
        ("CASE WHEN pg_input_is_valid(payload, 'jsonb') THEN CASE WHEN jsonb_typeof(payload::jsonb -> "
         "{p}) IS NOT NULL AND jsonb_typeof(payload::jsonb -> {p}) <> 'null' THEN CASE WHEN "
         "jsonb_typeof(payload::jsonb -> {p}) <> 'string' THEN TRUE ELSE (payload::jsonb ->> {p}) "
         'COLLATE "C" = {p} END ELSE TRUE END ELSE TRUE END',
         ['équipe', 'équipe', 'équipe', 'équipe', 'café']),
    ),
)
