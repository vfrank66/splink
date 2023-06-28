from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pandas as pd

from .blocking import _sql_gen_where_condition, block_using_rules_sql
from .misc import calculate_cartesian, calculate_reduction_ratio

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker


def number_of_comparisons_generated_by_blocking_rule_sql(
    linker: Linker,
    blocking_rule,
) -> str:
    settings_obj = linker._settings_obj

    where_condition = _sql_gen_where_condition(
        settings_obj._link_type, settings_obj._unique_id_input_columns
    )

    sql = f"""
    select count(*) as count_of_pairwise_comparisons_generated

    from __splink__df_concat as l
    inner join __splink__df_concat as r
    on
    {blocking_rule}
    {where_condition}
    """

    return sql


def cumulative_comparisons_generated_by_blocking_rules(
    linker: Linker, blocking_rules, output_chart=True, return_dataframe=False
):
    # Deepcopy our original linker so we can safely adjust our settings.
    # This is particularly important to ensure we don't overwrite our
    # original blocking rules.
    linker = deepcopy(linker)

    settings_obj = linker._settings_obj
    linker._settings_obj_ = settings_obj
    linker._analyse_blocking_mode = True

    if blocking_rules:
        brs_as_objs = settings_obj._brs_as_objs(blocking_rules)
        linker._settings_obj_._blocking_rules_to_generate_predictions = brs_as_objs

    # Turn tf off.  No need to apply term frequencies to perform these calcs
    settings_obj._retain_matching_columns = False
    settings_obj._retain_intermediate_calculation_columns = False
    for cc in settings_obj.comparisons:
        for cl in cc.comparison_levels:
            cl._level_dict["tf_adjustment_column"] = None

    concat = linker._initialise_df_concat(materialise=True)

    # Calculate the Cartesian Product
    if output_chart:
        # We only need the cartesian product if we want to output the chart view

        if settings_obj._link_type == "dedupe_only":
            group_by_statement = ""
        else:
            group_by_statement = "group by source_dataset"

        sql = f"""
            select count(*) as count
            from {concat.physical_name}
            {group_by_statement}
        """
        linker._enqueue_sql(sql, "__splink__cartesian_product")
        cartesian_count = linker._execute_sql_pipeline([concat])
        row_count_df = cartesian_count.as_record_dict()
        cartesian_count.drop_table_from_database_and_remove_from_cache()

        cartesian = calculate_cartesian(row_count_df, settings_obj._link_type)

    # Calculate the total number of rows generated by each blocking rule
    sql = block_using_rules_sql(linker)
    linker._enqueue_sql(sql, "__splink__df_blocked_data")

    brs_as_objs = linker._settings_obj_._blocking_rules_to_generate_predictions

    sql = """
        select
        count(*) as row_count,
        match_key
        from __splink__df_blocked_data
        group by match_key
        order by cast(match_key as int) asc
    """
    linker._enqueue_sql(sql, "__splink__df_count_cumulative_blocks")
    cumulative_blocking_rule_count = linker._execute_sql_pipeline([concat])
    br_n = cumulative_blocking_rule_count.as_pandas_dataframe()
    cumulative_blocking_rule_count.drop_table_from_database_and_remove_from_cache()
    br_count, br_keys = list(br_n.row_count), list(br_n["match_key"].astype("int"))

    if len(br_count) != len(brs_as_objs):
        missing_br = [x for x in range(len(brs_as_objs)) if x not in br_keys]
        for n in missing_br:
            br_count.insert(n, 0)

    br_comparisons = []
    cumulative_sum = 0
    # Wrap everything into an output dictionary
    for row, br in zip(br_count, brs_as_objs):
        out_dict = {
            "row_count": row,
            "rule": br.blocking_rule,
        }
        if output_chart:
            cumulative_sum += row
            # Increase round threshold to capture more info on larger datasets
            rr = round(calculate_reduction_ratio(cumulative_sum, cartesian), 6)

            rr_text = (
                "The rolling reduction ratio with your given blocking rule(s) "
                f"is {rr}. This represents the reduction in the total number "
                "of comparisons due to your rule(s)."
            )

            additional_vals = {
                "cumulative_rows": cumulative_sum,
                "cartesian": int(cartesian),
                "reduction_ratio": rr_text,
                "start": cumulative_sum - row,
            }
            out_dict = {**out_dict, **additional_vals}

        br_comparisons.append(out_dict.copy())

    linker._analyse_blocking_mode = False

    if return_dataframe:
        return pd.DataFrame(br_comparisons)
    else:
        return br_comparisons
