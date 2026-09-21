# Quote gate (JEVRES-005 corpus, not-implemented state)

Rate limiting for the quote API is PLANNED: a rolling one-minute window with a
cap of five requests is the design intent, recorded here so the intent is
findable. No limiter exists yet — there is no ``allow()`` anywhere in this
tree. ``config.py`` predates the plan and holds unrelated constants.
