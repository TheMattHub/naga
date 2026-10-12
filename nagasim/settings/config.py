# Legacy pre-Qt block size in pixels, kept only for the v3 -> v4 coordinate
# migration in core/schema/ (the Qt canvas has its own sizes in ui/style.py).
LEGACY_BLOCK_W = 80
LEGACY_BLOCK_H = 40

#Output settings
LOG_HISTORY = 100000 # max umber of simulation steps to keep in the log history. To initialize deque, which is a more efficient list (Sequence subclass)