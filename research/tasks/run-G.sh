#!/bin/bash
T=/Users/bagas/Downloads/vphone-legacy/research/tasks
cd /Users/bagas/Downloads/vphone-wt-lock-gfx
codex exec -m gpt-6.1-sol -c model_reasoning_effort="medium" --dangerously-bypass-approvals-and-sandbox -C "$PWD" "$(cat $T/G-lock-gfx.prompt)" < /dev/null 2>&1 | tee $T/G-lock-gfx.log
