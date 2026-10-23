#!/bin/bash
T=/Users/bagas/Downloads/vphone-legacy/research/tasks
cd /Users/bagas/Downloads/vphone-wt-live-swipe
agy --model gemini-3.8-flash-high --dangerously-skip-permissions -p="$(cat $T/T-live-swipe.prompt)"
