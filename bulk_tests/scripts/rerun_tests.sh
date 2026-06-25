#!/bin/bash

script_home="$(
  cd "$(dirname "$0")/.." || exit 1
  pwd -P
)"

cd "$script_home"

arxiv_ids=(
    # cs-*
    "1210.3846"
    "1310.6992"
    "1706.03762v7"
    "2310.17813"
    "2409.02038"
    "2501.00656"
    "2502.16982"
    "2507.20534"
    "2512.10931"
    "2603.07685"
    "2603.26164"
    "2604.10547"
    "2604.14934v2"
    "2605.08504"
    "2605.12521"
    "2605.13414"
    "2606.12360v2"
    "2606.24597v1"

    # astro-ph
    "2603.00232v2"

    # q-bio
    "2606.23871v1"
    "2606.24246v1"
    "2606.24406v1"

    # q-fin
    "2606.22162v1"
    "2606.24309v1" # fails, need to check

    # hep-ph
    "2606.24582v1"

    # hep-lat
    "2606.24222v1"

    # hep-th
    "2606.23893v1"
    "2606.24285v1"
    "2606.24272v1"

    # quant-ph
    "2506.00755v2" # fails, need to check
    "2606.24170v1"
    "2606.24238v1"
    "2606.24310v1"
)

mkdir --parents "$script_home/outputs" "$script_home/logs"

for arxiv_id in "${arxiv_ids[@]}"
do
    python "$script_home/scripts/convert_to_md.py" "$arxiv_id" > "$script_home/logs/$arxiv_id.txt"
done
