"""Learning for the scheduler: slot preferences from user moves and check-in
outcomes, a duration correction from observed durations, and the share of
free time that goes to tasks. Fitting code is pure (`preference`,
`duration`); `data` turns the schedule log into training rows and `jobs`
runs fits and publishes parameters to `learned_models`."""
