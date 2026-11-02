Commit and content guard, mirrored from the guard.py that the maintainers' local commit hooks run. Keep the copies identical.
CI runs the copy from the base revision of the checked range (the commit's own copy only when the base has none):
python3 guard.py ci --range BASE..HEAD
The process-note tests (test_process_notes.py, mirrored as well) run with python3 -m unittest discover -s scripts/guard
