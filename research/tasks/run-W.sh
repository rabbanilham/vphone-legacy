#!/bin/bash
T=/Users/bagas/Downloads/vphone-legacy/research/tasks
cd /Users/bagas/Downloads/vphone-wt-wifi
agy --model gemini-3.8-flash-high --dangerously-skip-permissions -p="$(cat $T/W-wifi-native.prompt)"
