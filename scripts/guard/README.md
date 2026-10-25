Mirror of coscila_develop/hooks/guard.py (the local commit hooks). Keep the copies identical.
CI runs the copy from the base revision of the checked range (the commit's own copy only when the base has none):
python3 guard.py ci --range BASE..HEAD
