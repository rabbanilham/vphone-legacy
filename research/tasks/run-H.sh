#!/bin/bash
T=/Users/bagas/Downloads/vphone-legacy/research/tasks
cd /Users/bagas/Downloads/vphone-wt-home-live
codex exec -m gpt-6.1-sol -c model_reasoning_effort="medium" --dangerously-bypass-approvals-and-sandbox -C "$PWD" "$(cat $T/H-home-live.prompt)" < /dev/null 2>&1 | tee $T/H-home-live.log
