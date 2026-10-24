#!/bin/bash
T=/Users/bagas/Downloads/vphone-legacy/research/tasks
cd /Users/bagas/Downloads/vphone-wt-home-live
codex exec resume -m gpt-6.1-sol -c model_reasoning_effort="medium" --dangerously-bypass-approvals-and-sandbox 01a1678f-dcdf-7202-beb9-d8420b67bbe3 "$(cat $T/H-resume.txt)" < /dev/null 2>&1 | tee -a $T/H-home-live.log
