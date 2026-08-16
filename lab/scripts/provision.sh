#!/usr/bin/env bash
# Обновить демо-инстанс Open WebUI одной командой: ставит ВЕСЬ кокпит (функции, тулзы, модели,
# арену) на РАБОТАЮЩИЙ контейнер через API. НЕ пересоздаёт контейнер и НЕ трогает веса.
#
# Локально:  ./provision.sh
# На VM:     ssh <vm> 'cd ~/lab && PATH=$HOME/bin:$PATH ./scripts/provision.sh'
#            (на VM нужен ~/bin/docker -> sudo docker; scripts используют docker exec)
#
# Идемпотентно: повторный запуск обновляет уже стоящее. Единственное предусловие — контейнер
# `open-webui` жив и слаги подключены (OPENAI_API_BASE_URLS при старте контейнера).
set -uo pipefail
cd "$(dirname "$0")"

echo "########## FUNCTIONS ##########"
./install_function.sh ../functions/collapse_reasoning.py collapse_reasoning filter 1
./install_function.sh ../functions/run_metadata.py   run_metadata   filter 1
./install_function.sh ../functions/inspect_actions.py inspect_actions action 1
./install_function.sh ../functions/autocontinue.py   autocontinue   pipe   0
./install_function.sh ../functions/project_gen.py    project_gen    pipe   0
./install_function.sh ../functions/serve_status.py   serve_status   pipe   0
./install_function.sh ../functions/gpu_status.py     gpu_status     pipe   0
./install_function.sh ../functions/chat_export.py    chat_export    action 1
./install_function.sh ../functions/ab_compare.py     ab_compare     pipe   0

echo "########## TOOLS ##########"
./install_tool.sh ../tools/diagram_tool.py diagram_tool qwen38-27b-gate
./install_tool.sh ../tools/llm_debug.py      llm_debug      qwen38-27b-gate
./install_tool.sh ../tools/context_budget.py context_budget qwen38-27b-gate
./install_tool.sh ../tools/sampling_spread.py sampling_spread qwen38-27b-gate
./install_tool.sh ../tools/plot_data.py      plot_data      qwen38-27b-gate

echo "########## MODELS (пресеты чекпойнтов) ##########"
python3 ./model_registry.py registry.example.json --apply

echo "########## ARENA (для DPO-пар) ##########"
python3 ./setup_arena.py --models ab-base,ab-rl-v7 --name "Checkpoint Arena (base vs RL)" --id ckpt-arena

echo "########## PROJECTS (папки с системным промптом) ##########"
python3 ./setup_projects.py --apply

echo "########## FEATURES (память, автодополнение, компакция) ##########"
python3 ./enable_features.py --apply
echo "(если что-то включилось впервые — перезапусти контейнер: docker restart open-webui)"

echo "########## ГОТОВО. Обновлённые модели: ##########"
python3 ./model_registry.py --list 2>/dev/null | sed 's/^/  /'
echo "Открой демо в браузере и проверь селектор моделей."
