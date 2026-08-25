import os
import sys

# Put repo root on sys.path so `import main`, `config`, `collectors.jira`, etc. resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
