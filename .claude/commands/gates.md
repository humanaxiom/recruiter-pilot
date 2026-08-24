# /gates

Run the full non-negotiable gate suite and report.

## Steps

1. Confirm current branch is not `main` (`git branch --show-current`)
2. Run `make gates` from repo root
3. Present a results table: gate name · status · duration · first failure line
4. If ANY gate is red:
   - Do NOT stop or summarize as "mostly passing"
   - Enter the review-iterate loop: fix only what failed, re-run
   - After 5 iterations still red → present full failure report + your analysis to the human
5. If all green: report "✅ ALL GATES GREEN" with coverage percentage
