from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Literal, Optional

from sqlglot import parse_one
from sqlglot.expressions import Column, Expression, Join
from sqlglot.optimizer.eliminate_joins import join_condition

from .exceptions import SplinkException
from .input_column import InputColumn
from .misc import ensure_is_list
from .pipeline import CTEPipeline
from .splink_dataframe import SplinkDataFrame
from .unique_id_concat import _composite_unique_id_from_nodes_sql
from .vertically_concatenate import compute_df_concat_with_tf

logger = logging.getLogger(__name__)

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker


def blocking_rule_to_obj(br: BlockingRule | dict | str) -> BlockingRule:
    if isinstance(br, BlockingRule):
        return br
    elif isinstance(br, dict):
        blocking_rule = br.get("blocking_rule", None)
        if blocking_rule is None:
            raise ValueError("No blocking rule submitted...")
        sqlglot_dialect = br.get("sql_dialect", None)

        salting_partitions = br.get("salting_partitions", None)
        arrays_to_explode = br.get("arrays_to_explode", None)

        if arrays_to_explode is not None and salting_partitions is not None:
            raise ValueError(
                "Splink does not support blocking rules that are "
                " both salted and exploding"
            )

        if salting_partitions is not None:
            return SaltedBlockingRule(
                blocking_rule, sqlglot_dialect, salting_partitions
            )

        if arrays_to_explode is not None:
            return ExplodingBlockingRule(
                blocking_rule, sqlglot_dialect, arrays_to_explode
            )

        return BlockingRule(blocking_rule, sqlglot_dialect)

    else:
        br = BlockingRule(br)
        return br


class BlockingRule:
    def __init__(
        self,
        blocking_rule_sql: str,
        sqlglot_dialect: str = None,
    ):
        if sqlglot_dialect:
            self._sql_dialect = sqlglot_dialect

        # Temporarily just to see if tests still pass
        if not isinstance(blocking_rule_sql, str):
            raise ValueError(
                f"Blocking rule must be a string, not {type(blocking_rule_sql)}"
            )
        self.blocking_rule_sql = blocking_rule_sql
        self.preceding_rules: List[BlockingRule] = []
        self.sqlglot_dialect = sqlglot_dialect

    @property
    def sql_dialect(self):
        return None if not hasattr(self, "_sql_dialect") else self._sql_dialect

    @property
    def match_key(self):
        return len(self.preceding_rules)

    def add_preceding_rules(self, rules):
        rules = ensure_is_list(rules)
        self.preceding_rules = rules

    def exclude_pairs_generated_by_this_rule_sql(self, linker: Linker):
        """A SQL string specifying how to exclude the results
        of THIS blocking rule from subseqent blocking statements,
        so that subsequent statements do not produce duplicate pairs
        """

        # Note the coalesce function is important here - otherwise
        # you filter out any records with nulls in the previous rules
        # meaning these comparisons get lost
        return f"coalesce(({self.blocking_rule_sql}),false)"

    def exclude_pairs_generated_by_all_preceding_rules_sql(self, linker: Linker):
        """A SQL string that excludes the results of ALL previous blocking rules from
        the pairwise comparisons generated.
        """
        if not self.preceding_rules:
            return ""
        or_clauses = [
            br.exclude_pairs_generated_by_this_rule_sql(linker)
            for br in self.preceding_rules
        ]
        previous_rules = " OR ".join(or_clauses)
        return f"AND NOT ({previous_rules})"

    def create_blocked_pairs_sql(
        self,
        linker: Linker,
        *,
        input_tablename_l,
        input_tablename_r,
        where_condition,
        probability,
    ):
        columns_to_select = linker._settings_obj._columns_to_select_for_blocking
        sql_select_expr = ", ".join(columns_to_select)

        sql = f"""
            select
            {sql_select_expr}
            , '{self.match_key}' as match_key
            {probability}
            from {input_tablename_l} as l
            inner join {input_tablename_r} as r
            on
            ({self.blocking_rule_sql})
            {where_condition}
            {self.exclude_pairs_generated_by_all_preceding_rules_sql(linker)}
            """
        return sql

    @property
    def _parsed_join_condition(self) -> Join:
        br = self.blocking_rule_sql
        return parse_one("INNER JOIN r", into=Join).on(
            br, dialect=self.sqlglot_dialect
        )  # using sqlglot==11.4.1

    @property
    def _equi_join_conditions(self):
        """
        Extract the equi join conditions from the blocking rule as a tuple:
        source_keys, join_keys

        Returns:
            list of tuples like [(name, name), (substr(name,1,2), substr(name,2,3))]
        """

        def remove_table_prefix(tree: Expression) -> Expression:
            for c in tree.find_all(Column):
                del c.args["table"]
            return tree

        j: Join = self._parsed_join_condition

        source_keys, join_keys, _ = join_condition(j)

        keys_zipped = zip(source_keys, join_keys)

        rmtp = remove_table_prefix

        keys_de_prefixed: list[tuple[Expression, Expression]] = [
            (rmtp(i), rmtp(j)) for (i, j) in keys_zipped
        ]

        keys_strings: list[tuple[str, str]] = [
            (i.sql(dialect=self.sqlglot_dialect), j.sql(self.sqlglot_dialect))
            for (i, j) in keys_de_prefixed
        ]

        return keys_strings

    @property
    def _filter_conditions(self):
        # A more accurate term might be "non-equi-join conditions"
        # or "complex join conditions", but to capture the idea these are
        # filters that have to be applied post-creation of the pairwise record
        # comparison i've opted to call it a filter
        j = self._parsed_join_condition
        _, _, filter_condition = join_condition(j)
        if not filter_condition:
            return ""
        else:
            return filter_condition.sql(self.sqlglot_dialect)

    def as_dict(self):
        "The minimal representation of the blocking rule"
        output = {}

        output["blocking_rule"] = self.blocking_rule_sql
        output["sql_dialect"] = self.sql_dialect

        return output

    def _as_completed_dict(self):
        return self.blocking_rule_sql

    @property
    def descr(self):
        return "Custom" if not hasattr(self, "_description") else self._description

    def _abbreviated_sql(self, cutoff=75):
        sql = self.blocking_rule_sql
        return (sql[:cutoff] + "...") if len(sql) > cutoff else sql

    def __repr__(self):
        return f"<{self._human_readable_succinct}>"

    @property
    def _human_readable_succinct(self):
        sql = self._abbreviated_sql(75)
        return f"{self.descr} blocking rule using SQL: {sql}"


class SaltedBlockingRule(BlockingRule):
    def __init__(
        self,
        blocking_rule: str,
        sqlglot_dialect: str = None,
        salting_partitions: int = 1,
    ):
        if salting_partitions is None or salting_partitions <= 1:
            raise ValueError("Salting partitions must be specified and > 1")

        super().__init__(blocking_rule, sqlglot_dialect)
        self.salting_partitions = salting_partitions

    def as_dict(self):
        output = super().as_dict()
        output["salting_partitions"] = self.salting_partitions
        return output

    def _as_completed_dict(self):
        return self.as_dict()

    def _salting_condition(self, salt):
        return f"AND ceiling(l.__splink_salt * {self.salting_partitions}) = {salt + 1}"

    def create_blocked_pairs_sql(
        self,
        linker: Linker,
        *,
        input_tablename_l,
        input_tablename_r,
        where_condition,
        probability,
    ):
        columns_to_select = linker._settings_obj._columns_to_select_for_blocking
        sql_select_expr = ", ".join(columns_to_select)

        sqls = []
        for salt in range(self.salting_partitions):
            salt_condition = self._salting_condition(salt)
            sql = f"""
            select
            {sql_select_expr}
            , '{self.match_key}' as match_key
            {probability}
            from {input_tablename_l} as l
            inner join {input_tablename_r} as r
            on
            ({self.blocking_rule_sql} {salt_condition})
            {where_condition}
            {self.exclude_pairs_generated_by_all_preceding_rules_sql(linker)}
            """

            sqls.append(sql)
        return " UNION ALL ".join(sqls)


class ExplodingBlockingRule(BlockingRule):
    def __init__(
        self,
        blocking_rule: BlockingRule | dict | str,
        sqlglot_dialect: str = None,
        array_columns_to_explode: list = [],
    ):
        if isinstance(blocking_rule, BlockingRule):
            blocking_rule_sql = blocking_rule.blocking_rule_sql
        elif isinstance(blocking_rule, dict):
            blocking_rule_sql = blocking_rule["blocking_rule_sql"]
        else:
            blocking_rule_sql = blocking_rule
        super().__init__(blocking_rule_sql, sqlglot_dialect)
        self.array_columns_to_explode: List[str] = array_columns_to_explode
        self.exploded_id_pair_table: Optional[SplinkDataFrame] = None

    def marginal_exploded_id_pairs_table_sql(self, linker: Linker, br: BlockingRule):
        """generates a table of the marginal id pairs from the exploded blocking rule
        i.e. pairs are only created that match this blocking rule and NOT any of
        the preceding blocking rules
        """

        settings_obj = linker._settings_obj
        unique_id_col = settings_obj.column_info_settings.unique_id_column_name
        unique_id_input_columns = (
            settings_obj.column_info_settings.unique_id_input_columns
        )

        link_type = settings_obj._link_type

        if linker._two_dataset_link_only:
            link_type = "two_dataset_link_only"

        if linker._self_link_mode:
            link_type = "self_link"

        where_condition = _sql_gen_where_condition(link_type, unique_id_input_columns)

        id_expr_l = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "l")
        id_expr_r = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "r")

        if link_type == "two_dataset_link_only":
            where_condition = (
                where_condition + " and l.source_dataset < r.source_dataset"
            )

        sql = f"""
            select distinct
                {id_expr_l} as {unique_id_col}_l,
                {id_expr_r} as {unique_id_col}_r
            from __splink__df_concat_with_tf_unnested as l
            inner join __splink__df_concat_with_tf_unnested as r
            on ({br.blocking_rule_sql})
            {where_condition}
            {self.exclude_pairs_generated_by_all_preceding_rules_sql(linker)}
            """

        return sql

    def drop_materialised_id_pairs_dataframe(self):
        if self.exploded_id_pair_table is not None:
            self.exploded_id_pair_table.drop_table_from_database_and_remove_from_cache()
        self.exploded_id_pair_table = None

    def exclude_pairs_generated_by_this_rule_sql(self, linker: Linker):
        """A SQL string specifying how to exclude the results
        of THIS blocking rule from subseqent blocking statements,
        so that subsequent statements do not produce duplicate pairs
        """

        unique_id_column = (
            linker._settings_obj.column_info_settings.unique_id_column_name
        )
        unique_id_input_columns = (
            linker._settings_obj.column_info_settings.unique_id_input_columns
        )
        if (splink_df := self.exploded_id_pair_table) is None:
            raise SplinkException(
                "Must use `materialise_exploded_id_table(linker)` "
                "to set `exploded_id_pair_table` before calling "
                "exclude_pairs_generated_by_this_rule_sql()."
            )
        ids_to_compare_sql = f"select * from {splink_df.physical_name}"

        id_expr_l = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "l")
        id_expr_r = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "r")

        return f"""EXISTS (
            select 1 from ({ids_to_compare_sql}) as ids_to_compare
            where (
                {id_expr_l} = ids_to_compare.{unique_id_column}_l and
                {id_expr_r} = ids_to_compare.{unique_id_column}_r
            )
        )
        """

    def create_blocked_pairs_sql(
        self,
        linker: Linker,
        *,
        input_tablename_l,
        input_tablename_r,
        where_condition,
        probability,
    ):
        columns_to_select = linker._settings_obj._columns_to_select_for_blocking
        sql_select_expr = ", ".join(columns_to_select)

        if self.exploded_id_pair_table is None:
            raise ValueError(
                "Exploding blocking rules are not supported for the function you have"
                " called."
            )
        settings_obj = linker._settings_obj
        unique_id_col = settings_obj.column_info_settings.unique_id_column_name
        unique_id_input_columns = (
            settings_obj.column_info_settings.unique_id_input_columns
        )
        id_expr_l = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "l")
        id_expr_r = _composite_unique_id_from_nodes_sql(unique_id_input_columns, "r")

        exploded_id_pair_table = self.exploded_id_pair_table
        sql = f"""
            select
                {sql_select_expr},
                '{self.match_key}' as match_key
                {probability}
            from {exploded_id_pair_table.physical_name} as pairs
            left join {input_tablename_l} as l
                on pairs.{unique_id_col}_l={id_expr_l}
            left join {input_tablename_r} as r
                on pairs.{unique_id_col}_r={id_expr_r}
        """
        return sql

    def as_dict(self):
        output = super().as_dict()
        output["arrays_to_explode"] = self.array_columns_to_explode
        return output


def materialise_exploded_id_tables(linker: Linker):
    settings_obj = linker._settings_obj

    blocking_rules = settings_obj._blocking_rules_to_generate_predictions
    exploding_blocking_rules = [
        br for br in blocking_rules if isinstance(br, ExplodingBlockingRule)
    ]
    if len(exploding_blocking_rules) == 0:
        return []
    exploded_tables = []

    pipeline = CTEPipeline()
    nodes_with_tf = compute_df_concat_with_tf(linker, pipeline)

    input_colnames = {col.name for col in nodes_with_tf.columns}

    for br in exploding_blocking_rules:
        pipeline = CTEPipeline([nodes_with_tf])
        arrays_to_explode_quoted = [
            InputColumn(colname, sql_dialect=linker._sql_dialect).quote().name
            for colname in br.array_columns_to_explode
        ]
        expl_sql = linker._explode_arrays_sql(
            "__splink__df_concat_with_tf",
            br.array_columns_to_explode,
            list(input_colnames.difference(arrays_to_explode_quoted)),
        )

        pipeline.enqueue_sql(
            expl_sql,
            "__splink__df_concat_with_tf_unnested",
        )

        base_name = "__splink__marginal_exploded_ids_blocking_rule"
        table_name = f"{base_name}_mk_{br.match_key}"

        sql = br.marginal_exploded_id_pairs_table_sql(linker, br)

        pipeline.enqueue_sql(sql, table_name)

        marginal_ids_table = linker.db_api.sql_pipeline_to_splink_dataframe(pipeline)
        br.exploded_id_pair_table = marginal_ids_table
        exploded_tables.append(marginal_ids_table)

    return exploding_blocking_rules


def _sql_gen_where_condition(link_type, unique_id_cols):
    id_expr_l = _composite_unique_id_from_nodes_sql(unique_id_cols, "l")
    id_expr_r = _composite_unique_id_from_nodes_sql(unique_id_cols, "r")

    if link_type in ("two_dataset_link_only", "self_link"):
        where_condition = " where 1=1 "
    elif link_type in ["link_and_dedupe", "dedupe_only"]:
        where_condition = f"where {id_expr_l} < {id_expr_r}"
    elif link_type == "link_only":
        source_dataset_col = unique_id_cols[0]
        where_condition = (
            f"where {id_expr_l} < {id_expr_r} "
            f"and l.{source_dataset_col.name} != r.{source_dataset_col.name}"
        )

    return where_condition


def block_using_rules_sqls(
    linker: Linker,
    *,
    input_tablename_l: str,
    input_tablename_r: str,
    blocking_rules: List[BlockingRule],
    link_type: Literal[
        "two_dataset_link_only",
        "self_link",
        "link_only",
        "link_and_dedupe",
        "dedupe_only",
    ],
    set_match_probability_to_one: bool = False,
):
    """Use the blocking rules specified in the linker's settings object to
    generate a SQL statement that will create pairwise record comparions
    according to the blocking rule(s).

    Where there are multiple blocking rules, the SQL statement contains logic
    so that duplicate comparisons are not generated.
    """

    sqls = []

    settings_obj = linker._settings_obj

    link_type = settings_obj._link_type

    where_condition = _sql_gen_where_condition(
        link_type, settings_obj.column_info_settings.unique_id_input_columns
    )

    # Cover the case where there are no blocking rules
    # This is a bit of a hack where if you do a self-join on 'true'
    # you create a cartesian product, rather than having separate code
    # that generates a cross join for the case of no blocking rules
    if not blocking_rules:
        blocking_rules = [BlockingRule("1=1")]

    # For Blocking rules for deterministic rules, add a match probability
    # column with all probabilities set to 1.
    if set_match_probability_to_one:
        probability = ", 1.00 as match_probability"
    else:
        probability = ""

    br_sqls = []

    for br in blocking_rules:
        sql = br.create_blocked_pairs_sql(
            linker,
            input_tablename_l=input_tablename_l,
            input_tablename_r=input_tablename_r,
            where_condition=where_condition,
            probability=probability,
        )
        br_sqls.append(sql)

    sql = " UNION ALL ".join(br_sqls)

    sqls.append({"sql": sql, "output_table_name": "__splink__df_blocked"})

    return sqls
